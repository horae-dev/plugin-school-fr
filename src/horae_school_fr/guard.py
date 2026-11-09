# SPDX-License-Identifier: Apache-2.0
"""The lockout guard: a persistent, process-safe poll and login budget.

PRONOTE and ÉcoleDirecte are reverse-engineered APIs and repeated logins can lock a parent's
account (risk horae-dev/horae#73). Every source of this plugin must go through `Budget.begin`
before it touches the network; the guard refuses, with a fixed reason and no network call, when
the account has used its budget.

**The contract for a source**

    attempt = budget.begin(account, limits, login=session_is_gone)   # raises `Refused`
    with attempt:
        ...one network round, at most one login, no retry...
        raise AuthFailure("login_failed")        # wrong credentials, quiz failed, lockout text

- `begin` checks, in this order: an auth stop, a backoff, the allowed windows, the daily poll
  budget and `min_interval`, and (`login=True`, only when the cached session or token is gone)
  the daily login budget. It then *reserves* the poll (and the login) in the state file before
  returning, so a crash during the attempt still counts and two overlapping runs cannot both pass.
- Leaving the block records the outcome: normally = success (the failure streak resets);
  `AuthFailure` = stop completely until a human resets the account; any other exception = a
  transient failure, which starts an exponential backoff (1 h, 2 h, 4 h ... capped at 24 h).
- A `Refused` is a `PluginError` with the message `degraded: <reason>`: letting it propagate makes
  the core mark the `collect` step degraded.

**State**: a JSON file at a path the core approved for writing, written by an atomic replace
(mode 0600) under an `fcntl` lock on `<state_path>.lock`. It holds timestamps, counters and short
fixed codes only: no credential, no token, no name. A file that cannot be read, parsed or
recognised **fails closed**: every account is treated as stopped (`auth_stopped`, detail
`state_invalid`) and the file is left untouched for a human to look at.
"""

import contextlib
import fcntl
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from datetime import time as day_time
from types import TracebackType
from typing import Any, Self

from horae_sdk import Call, PluginError

Clock = Callable[[], datetime]
"""Returns the current time as an aware datetime. Tests inject a manual clock."""

VERSION = 1
MAX_BYTES = 1024 * 1024
MAX_HISTORY = 64
"""Per account and kind; the day limits are at most 12 and 6, and 48 hours are kept."""
KEEP = timedelta(hours=48)
BACKOFF_START = timedelta(hours=1)
BACKOFF_CAP = timedelta(hours=24)
LOCK_TIMEOUT_S = 10.0

BUDGET_EXHAUSTED = "budget_exhausted"
OUTSIDE_WINDOW = "outside_window"
BACKOFF_UNTIL = "backoff_until"
AUTH_STOPPED = "auth_stopped"
REASONS = (BUDGET_EXHAUSTED, OUTSIDE_WINDOW, BACKOFF_UNTIL, AUTH_STOPPED)
"""The only refusal reasons. `backoff_until` is followed by the time (`backoff_until <iso>`)."""

LABEL = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
CODE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
WINDOW = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")
DURATION = re.compile(r"^(\d{1,5})(s|m|h)$")

MAX_POLLS_RANGE = (1, 12)
MAX_LOGINS_RANGE = (1, 6)
MIN_INTERVAL_RANGE = (timedelta(minutes=1), timedelta(hours=24))
MAX_WINDOWS = 8


class Refused(PluginError):
    """The budget says no. Nothing was sent. `str(error)` is `degraded: <reason>`, plus the
    detail in parentheses when there is one."""

    def __init__(
        self,
        code: str,
        *,
        until: datetime | None = None,
        shown_until: str | None = None,
        detail: str | None = None,
    ) -> None:
        self.code = code
        """One of `REASONS`."""
        self.until = until
        """For `backoff_until` (and the next allowed poll): when, as an aware datetime."""
        self.detail = detail
        """A fixed secondary code (`state_invalid`, `state_busy`, `login_budget`...)."""
        self.reason = code if shown_until is None else f"{code} {shown_until}"
        message = f"degraded: {self.reason}"
        super().__init__(f"{message} ({detail})" if detail else message)


class AuthFailure(PluginError):
    """Raised by a source inside an attempt after an authentication failure (wrong credentials,
    a failed quiz, a lockout message). The guard stops the account until a human resets it."""

    def __init__(self, code: str = "auth_failed") -> None:
        self.code = code if CODE.fullmatch(code) else "auth_failed"
        """A short fixed code (`login_failed`, `quiz_failed`, `locked_out`...), never a message
        from the service."""
        super().__init__(f"degraded: {AUTH_STOPPED} ({self.code})")


class StateInvalid(Exception):
    """The state file cannot be trusted (unreadable, corrupt, unknown version or shape)."""


# --- limits ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """An allowed local-time window: `start` inclusive, `end` exclusive; `start > end` crosses
    midnight (`22:00-06:00`)."""

    start: day_time
    end: day_time

    def contains(self, moment: day_time) -> bool:
        if self.start < self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


def parse_window(text: object) -> Window:
    match = WINDOW.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise PluginError("config_invalid: a window is 'HH:MM-HH:MM' (24 h, local time)")
    start = day_time(int(match[1]), int(match[2]))
    end = day_time(int(match[3]), int(match[4]))
    if start == end:
        raise PluginError("config_invalid: a window must not start and end at the same time")
    return Window(start, end)


def parse_duration(text: object) -> timedelta:
    match = DURATION.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise PluginError("config_invalid: a duration is a number and s, m or h (2h, 90m)")
    unit = {"s": 1, "m": 60, "h": 3600}[match[2]]
    return timedelta(seconds=int(match[1]) * unit)


@dataclass(frozen=True)
class Limits:
    """The budget of one account."""

    max_polls_per_day: int = 4
    """Polls per local day (1 to 12). A poll is one attempt that may touch the network."""
    min_interval: timedelta = timedelta(hours=2)
    """The least time between two polls (1 minute to 24 hours)."""
    windows: tuple[Window, ...] = ()
    """Allowed local-time windows; none means any time."""
    max_logins_per_day: int = 2
    """Logins per local day (1 to 6), counted apart from polls: a login happens only when the
    cached session or token is gone."""

    def __post_init__(self) -> None:
        low, high = MAX_POLLS_RANGE
        if type(self.max_polls_per_day) is not int or not low <= self.max_polls_per_day <= high:
            raise PluginError(f"config_invalid: max_polls_per_day is {low} to {high}")
        low, high = MAX_LOGINS_RANGE
        if type(self.max_logins_per_day) is not int or not low <= self.max_logins_per_day <= high:
            raise PluginError(f"config_invalid: max_logins_per_day is {low} to {high}")
        shortest, longest = MIN_INTERVAL_RANGE
        if not shortest <= self.min_interval <= longest:
            raise PluginError("config_invalid: min_interval is 1m to 24h")
        if len(self.windows) > MAX_WINDOWS:
            raise PluginError(f"config_invalid: at most {MAX_WINDOWS} windows")

    @classmethod
    def from_params(
        cls,
        *,
        max_polls_per_day: int = 4,
        min_interval: str = "2h",
        windows: Sequence[str] | None = None,
        max_logins_per_day: int = 2,
    ) -> Self:
        """The limits from a tool's parameters (`min_interval` like `2h` or `90m`, `windows`
        like `["07:00-08:00", "17:00-20:00"]`); `PluginError("config_invalid: ...")` otherwise."""
        if windows is not None and (isinstance(windows, str) or not isinstance(windows, Sequence)):
            raise PluginError("config_invalid: windows is a list of 'HH:MM-HH:MM'")
        return cls(
            max_polls_per_day,
            parse_duration(min_interval),
            tuple(parse_window(w) for w in windows or ()),
            max_logins_per_day,
        )


def backoff_delay(failures: int) -> timedelta:
    """1 h after the first failure, doubling each time, capped at 24 h."""
    doublings: int = min(max(failures, 1) - 1, 8)
    return min(BACKOFF_CAP, BACKOFF_START * (1 << doublings))


def instance_timezone(environ: Mapping[str, str] | None = None) -> tzinfo:
    """The instance time zone, which the plugin host passes as `TZ` (`horae.yaml` `timezone`).
    Unset means UTC; an unknown name is a configuration error (the guard needs the right day)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    name = (os.environ if environ is None else environ).get("TZ") or "UTC"
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise PluginError("config_invalid: the instance time zone (TZ) is not known") from None


def utc_now() -> datetime:
    return datetime.now(UTC)


# --- state -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Account:
    polls: tuple[datetime, ...] = ()
    """When polls were reserved (UTC, the last 48 hours)."""
    logins: tuple[datetime, ...] = ()
    failures: int = 0
    """The current streak of transient failures."""
    backoff_until: datetime | None = None
    stopped_at: datetime | None = None
    stop_code: str | None = None
    """Set after an auth failure; only a human clears it."""


def _stamp(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("timestamp")
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ValueError("timestamp")
    return moment.astimezone(UTC)


def _stamps(value: object) -> tuple[datetime, ...]:
    if not isinstance(value, list) or len(value) > MAX_HISTORY:
        raise ValueError("history")
    return tuple(_stamp(item) for item in value)


def _account(entry: object) -> Account:
    if not isinstance(entry, dict) or set(entry) != {
        "polls",
        "logins",
        "failures",
        "backoff_until",
        "auth_stopped",
    }:
        raise ValueError("account")
    failures = entry["failures"]
    if type(failures) is not int or not 0 <= failures <= 10_000:
        raise ValueError("failures")
    backoff = entry["backoff_until"]
    stop = entry["auth_stopped"]
    stopped_at = stop_code = None
    if stop is not None:
        if not isinstance(stop, dict) or set(stop) != {"at", "code"}:
            raise ValueError("auth_stopped")
        code = stop["code"]
        if type(code) is not str or not CODE.fullmatch(code):
            raise ValueError("code")
        stopped_at, stop_code = _stamp(stop["at"]), code
    return Account(
        _stamps(entry["polls"]),
        _stamps(entry["logins"]),
        failures,
        None if backoff is None else _stamp(backoff),
        stopped_at,
        stop_code,
    )


def _dump_account(account: Account) -> dict[str, Any]:
    return {
        "polls": [t.isoformat() for t in account.polls],
        "logins": [t.isoformat() for t in account.logins],
        "failures": account.failures,
        "backoff_until": None
        if account.backoff_until is None
        else account.backoff_until.isoformat(),
        "auth_stopped": (
            None
            if account.stopped_at is None or account.stop_code is None
            else {"at": account.stopped_at.isoformat(), "code": account.stop_code}
        ),
    }


def load(path: str) -> dict[str, Account]:
    """`{label: Account}`. An absent file is an empty state (a first run); anything else that is
    not exactly a version-1 state raises `StateInvalid` (the caller fails closed)."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return {}
    except OSError:
        raise StateInvalid("unreadable") from None
    try:
        if len(raw) > MAX_BYTES:
            raise ValueError("too large")
        document = json.loads(raw)
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "accounts"}
            or document["version"] != VERSION
            or not isinstance(document["accounts"], dict)
        ):
            raise ValueError("shape")
        result: dict[str, Account] = {}
        for label, entry in document["accounts"].items():
            if not LABEL.fullmatch(label):
                raise ValueError("label")
            result[label] = _account(entry)
        return result
    except (ValueError, KeyError, TypeError, RecursionError):
        raise StateInvalid("invalid") from None


def save(path: str, accounts: Mapping[str, Account]) -> None:
    """Write atomically: a synthesised sibling (0600), flushed to disk, then `os.replace`."""
    document = {
        "version": VERSION,
        "accounts": {label: _dump_account(a) for label, a in sorted(accounts.items())},
    }
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=os.path.dirname(path), prefix=".school-fr-state-"
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        if temporary:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
        raise PluginError("state_unwritable: cannot write the state file") from None


# --- the decision ----------------------------------------------------------------------------


def _local_day(moment: datetime, zone: tzinfo) -> date:
    return moment.astimezone(zone).date()


def _show(moment: datetime, zone: tzinfo) -> str:
    return moment.astimezone(zone).replace(microsecond=0).isoformat()


def counts(account: Account, now: datetime, zone: tzinfo) -> tuple[int, int]:
    """Polls and logins reserved on the local day of `now`."""
    today = _local_day(now, zone)
    return (
        sum(_local_day(t, zone) == today for t in account.polls),
        sum(_local_day(t, zone) == today for t in account.logins),
    )


def next_poll_at(account: Account, limits: Limits) -> datetime | None:
    """When `min_interval` allows the next poll (`None` before the first one)."""
    return max(account.polls) + limits.min_interval if account.polls else None


def evaluate(
    account: Account, limits: Limits, now: datetime, zone: tzinfo, *, login: bool
) -> Refused | None:
    """Why the account may not poll (and log in) now, or `None`. Pure: no clock, no I/O."""
    if account.stopped_at is not None:
        return Refused(AUTH_STOPPED, detail=account.stop_code)
    if account.backoff_until is not None and now < account.backoff_until:
        return Refused(
            BACKOFF_UNTIL,
            until=account.backoff_until,
            shown_until=_show(account.backoff_until, zone),
        )
    if limits.windows:
        local = now.astimezone(zone).time()
        if not any(w.contains(local) for w in limits.windows):
            return Refused(OUTSIDE_WINDOW)
    polls, logins = counts(account, now, zone)
    if polls >= limits.max_polls_per_day:
        return Refused(BUDGET_EXHAUSTED, detail="polls_per_day")
    following = next_poll_at(account, limits)
    if following is not None and now < following:
        return Refused(BUDGET_EXHAUSTED, until=following, detail="min_interval")
    if login and logins >= limits.max_logins_per_day:
        return Refused(BUDGET_EXHAUSTED, detail="logins_per_day")
    return None


def _prune(times: tuple[datetime, ...], now: datetime) -> tuple[datetime, ...]:
    return tuple(sorted(t for t in times if t > now - KEEP))[-MAX_HISTORY:]


# --- the budget ------------------------------------------------------------------------------


class Budget:
    """The budget of every account in one state file."""

    def __init__(
        self,
        state_path: str,
        zone: tzinfo,
        clock: Clock = utc_now,
        *,
        lock_timeout: float = LOCK_TIMEOUT_S,
    ) -> None:
        self.state_path = state_path
        self.zone = zone
        self.clock = clock
        self.lock_timeout = lock_timeout

    def now(self) -> datetime:
        moment = self.clock()
        if moment.tzinfo is None:
            raise PluginError("config_invalid: the clock must be timezone-aware")
        return moment.astimezone(UTC)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """An exclusive `flock` on a sibling file (the state file itself is replaced, so its
        inode cannot carry the lock). Waits up to `lock_timeout`, then refuses."""
        try:
            descriptor = os.open(
                f"{self.state_path}.lock",
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
        except OSError:
            raise PluginError("state_unwritable: cannot open the state lock") from None
        try:
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Refused(BUDGET_EXHAUSTED, detail="state_busy") from None
                    time.sleep(0.02)
            yield
        finally:
            os.close(descriptor)  # closing releases the lock

    def _load(self) -> dict[str, Account]:
        try:
            return load(self.state_path)
        except StateInvalid:
            raise Refused(AUTH_STOPPED, detail="state_invalid") from None

    def begin(self, account: str, limits: Limits, *, login: bool) -> "Attempt":
        """Check the budget and reserve a poll (and a login when `login`), or raise `Refused`.
        Pass `login=True` only when the cached session or token is gone and the attempt will
        log in. Nothing is sent here. Use the result as a context manager, once."""
        _check_label(account)
        with self._locked():
            state = self._load()
            now = self.now()
            current = state.get(account, Account())
            refusal = evaluate(current, limits, now, self.zone, login=login)
            if refusal is not None:
                raise refusal
            state[account] = replace(
                current,
                polls=_prune((*current.polls, now), now),
                logins=_prune((*current.logins, now) if login else current.logins, now),
            )
            save(self.state_path, state)
        return Attempt(self, account)

    def _record(self, account: str, change: Callable[[Account, datetime], Account]) -> None:
        with self._locked():
            try:
                state = load(self.state_path)
            except StateInvalid:
                raise PluginError("state_invalid: the state file is not a valid state") from None
            now = self.now()
            state[account] = change(state.get(account, Account()), now)
            save(self.state_path, state)

    def peek(self, account: str, limits: Limits) -> dict[str, Any]:
        """The guard state of `account` for a diagnostic, without reserving or locking (the
        file is replaced atomically, so a read is always whole). Never raises on a bad file."""
        _check_label(account)
        now = self.now()
        try:
            state = load(self.state_path)
        except StateInvalid:
            return {"account": account, "state": "invalid", "would_refuse": AUTH_STOPPED}
        current = state.get(account, Account())
        refusal = evaluate(current, limits, now, self.zone, login=False)
        polls, logins = counts(current, now, self.zone)
        following = next_poll_at(current, limits)
        return {
            "account": account,
            "state": (
                "auth_stopped"
                if current.stopped_at is not None
                else "backoff"
                if current.backoff_until is not None and now < current.backoff_until
                else "ok"
            ),
            "would_refuse": None if refusal is None else refusal.reason,
            "polls_today": polls,
            "max_polls_per_day": limits.max_polls_per_day,
            "logins_today": logins,
            "max_logins_per_day": limits.max_logins_per_day,
            "failures": current.failures,
            "last_poll_at": _show(max(current.polls), self.zone) if current.polls else None,
            "next_poll_not_before": None if following is None else _show(following, self.zone),
            "backoff_until": (
                None if current.backoff_until is None else _show(current.backoff_until, self.zone)
            ),
            "auth_stop_code": current.stop_code,
            "auth_stopped_at": (
                None if current.stopped_at is None else _show(current.stopped_at, self.zone)
            ),
        }

    def accounts(self) -> list[str]:
        """The labels in the state file (`[]` when absent or invalid)."""
        try:
            return sorted(load(self.state_path))
        except StateInvalid:
            return []


def _check_label(account: object) -> None:
    if not isinstance(account, str) or not LABEL.fullmatch(account):
        raise PluginError("config_invalid: account is a label (A-Z a-z 0-9 . _ -, at most 32)")


class Attempt:
    """One reserved attempt. A context manager that records the outcome on exit; single use."""

    def __init__(self, budget: Budget, account: str) -> None:
        self._budget = budget
        self._account = account
        self._entered = False
        self._done = False

    def __enter__(self) -> Self:
        if self._entered:
            raise RuntimeError("an attempt is used once: call Budget.begin again (it is budgeted)")
        self._entered = True
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        if self._done:
            return
        if error is None:
            self.success()
        elif isinstance(error, AuthFailure):
            self.auth_failure(error.code)
        else:
            self.failure()

    def success(self) -> None:
        """The attempt worked: the failure streak and any elapsed backoff are cleared."""
        self._finish(lambda a, now: replace(a, failures=0, backoff_until=None))

    def failure(self) -> None:
        """A transient failure (network, API change, parse error): back off 1 h, 2 h, 4 h ...
        up to 24 h from now."""

        def change(account: Account, now: datetime) -> Account:
            failures = account.failures + 1
            return replace(account, failures=failures, backoff_until=now + backoff_delay(failures))

        self._finish(change)

    def auth_failure(self, code: str = "auth_failed") -> None:
        """Stop the account completely until a human resets it (README, "Limits")."""
        safe = code if CODE.fullmatch(code) else "auth_failed"
        self._finish(lambda a, now: replace(a, stopped_at=now, stop_code=safe))

    def _finish(self, change: Callable[[Account, datetime], Account]) -> None:
        if self._done:
            return
        self._done = True
        self._budget._record(self._account, change)


def resolve_state_path(call: Call, value: object) -> str:
    """The `state_path` parameter checked against the paths the core approved for writing."""
    if not isinstance(value, str) or not value:
        raise PluginError("config_invalid: state_path is required (an absolute path)")
    real = call.paths.allowed(value, write=True)
    if real is None or os.path.isdir(real):
        raise PluginError("path_not_approved: state_path is not under a path approved for writing")
    return real


@dataclass(frozen=True)
class Guard:
    """What a source needs, built once per call by `plugin.build`: the budget and the limits."""

    budget: Budget
    limits: Limits
    account: str

    def begin(self, *, login: bool) -> Attempt:
        return self.budget.begin(self.account, self.limits, login=login)

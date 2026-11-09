# SPDX-License-Identifier: Apache-2.0
"""The PRONOTE source `school-fr.pronote` and the `pronote-setup` helper.

**This is the only module that imports `pronotepy`** (an unofficial, reverse-engineered library;
risk horae-dev/horae#73). Everything else (the guard, the credential store, the normalizer) is
independent of it, and `tests/test_pronote.py` checks that.

**One poll** (`run`), in this order, each step before the next one can touch the network:

1. the parameters, the limits, the approved paths and the credentials (store, else the secret);
2. the host check: `call.host_allowed(url)` (see `check_host`);
3. the credential store is proven writable (a login spends the token);
4. `guard.begin(login=True)`: the budget is reserved, or `Refused` ends the call;
5. inside the attempt: **one** token login, then the new credentials are persisted **at once**,
   then the reads, then the credentials again if they changed. No step is retried.

`login=True` always: a plugin process lives for one run, so there is never a cached session,
and every poll is a login. The login budget therefore caps the polls too (`max_logins_per_day`
defaults to `max_polls_per_day`, at most 6).

**Read-only.** The client is wrapped in `ReadOnly`, a proxy that answers only the names of
`READ_ATTRIBUTES` and raises `ReadOnlyViolation` for anything else (`set_done`, `mark_as_read`,
`post`, `new_discussion`, ...). Objects it returns are proxied the same way.

**Failures.** Wrong credentials, an expired token, a second factor or a lockout message raise
`AuthFailure(code)` (the guard stops the account); a network error, a timeout or an API change
raises `SourceUnavailable` (the guard backs off). Messages are fixed codes: never a credential, a
URL, a name or a response body. pronotepy's own logger is silenced, because it logs those.

**pronotepy's retries are disabled.** Its `ParentClient.post` re-logs in (`refresh`) after an API
error, and a failed first login would otherwise be retried behind our back. `_Client.refresh`
raises instead, so an error ends the attempt. `initialise` still repeats a harmless GET up to
three times when the login page does not parse.
"""

import argparse
import getpass
import hashlib
import importlib
import json
import logging
import math
import os
import re
import sys
import unicodedata
import uuid as uuidlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final, TextIO

import pronotepy
import requests
from horae_sdk import Call, Output, PluginError
from horae_sdk.protocol import url_host

from horae_school_fr._http import TimeoutSession
from horae_school_fr.credentials import (
    DEFAULT_NAME,
    SECRET,
    Credentials,
    CredentialStore,
    from_secret,
    parse,
)
from horae_school_fr.guard import (
    LABEL,
    AuthFailure,
    Clock,
    Guard,
    Limits,
)
from horae_school_fr.normalize import (
    MAX_CHILDREN,
    Child,
    Grade,
    Homework,
    Signature,
    normalize,
)

# pronotepy logs response bodies and user names at debug and info level.
logging.getLogger("pronotepy").setLevel(logging.CRITICAL)
logging.getLogger("pronotepy").propagate = False

REF: Final = "school-fr.pronote"
INDEX_EDUCATION: Final = ".index-education.net"
HTTP_TIMEOUT_S: Final = 30.0
"""pronotepy sets no timeout. Every request of its `requests.Session` (the login and the reads
that follow) gets this one: see `_RequestsWithTimeout`."""
MAX_DAYS_AHEAD: Final = 60
INFO_DAYS: Final = 30
"""Unread informations are looked for since this many days."""
MAX_GRADES: Final = 200
MAX_LIST: Final = 16
MAX_TEXT_PARAM: Final = 64
LOCKED_OUT_CODE: Final = 25
"""PRONOTE's "exceeded max authorization requests"."""

_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]")
_HEX = re.compile(r"(?:[0-9a-fA-F]{2})+")

# --- the read-only proxy -----------------------------------------------------------------------

READ_METHODS: Final = frozenset(
    {
        "export_credentials",  # a dict of the credentials: no request
        "homework",  # PageCahierDeTexte
        "information_and_surveys",  # PageActualites (the list; never `content()`)
        "set_child",  # selects a child locally: no request
    }
)
"""The only methods the source may call."""
READ_ATTRIBUTES: Final = frozenset(
    {
        # the client
        "children",
        "periods",
        "current_period",
        # a child, a period, a subject
        "id",
        "name",
        # a period
        "grades",
        # homework
        "subject",
        "description",
        "done",
        "date",
        "files",
        # a grade
        "grade",
        "out_of",
        "comment",
        # an information
        "title",
        "category",
        "survey",
        "read",
        "start_date",
    }
)
"""The only data the source may read, on any object."""
ALLOWED: Final = READ_METHODS | READ_ATTRIBUTES
_PLAIN = (str, int, float, bool, date, datetime, type(None))


class ReadOnlyViolation(Exception):
    """Something outside the read allow-list was asked of the client."""


def _unwrap(value: object) -> object:
    return object.__getattribute__(value, "_target") if isinstance(value, ReadOnly) else value


def _wrap(value: object) -> object:
    if isinstance(value, (*_PLAIN, ReadOnly)):
        return value
    if isinstance(value, dict):
        return {k: _wrap(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(_wrap(item) for item in value)
    return ReadOnly(value)


class ReadOnly:
    """A proxy that lets through the names of `ALLOWED` and nothing else: no write method, no
    raw `post`, no assignment. Results are wrapped again (lists become tuples)."""

    __slots__ = ("_target",)

    def __init__(self, target: object) -> None:
        object.__setattr__(self, "_target", target)

    def __getattribute__(self, name: str) -> Any:
        if name not in ALLOWED:
            raise ReadOnlyViolation(f"{name!r} is not on the read allow-list")
        value = getattr(object.__getattribute__(self, "_target"), name)
        if name in READ_METHODS:

            def call(*args: object, **kwargs: object) -> object:
                return _wrap(
                    value(
                        *(_unwrap(a) for a in args),
                        **{k: _unwrap(v) for k, v in kwargs.items()},
                    )
                )

            return call
        return _wrap(value)

    def __setattr__(self, name: str, value: object) -> None:
        raise ReadOnlyViolation("the client is read-only")

    def __delattr__(self, name: str) -> None:
        raise ReadOnlyViolation("the client is read-only")

    def __repr__(self) -> str:
        return "<ReadOnly>"


# --- the client --------------------------------------------------------------------------------


class _LoginRejected(Exception):
    """PRONOTE answered the login without a session: wrong or expired credentials."""


class _SessionLost(Exception):
    """pronotepy wanted to log in again after an error; we do not."""


class _Client(pronotepy.ParentClient):
    """A parent client that never retries on its own."""

    def _login(self) -> bool:
        if not super()._login():
            # Without this, ParentClient.__init__ fails on a missing key and hides the cause.
            raise _LoginRejected
        return True

    def refresh(self) -> None:
        raise _SessionLost


class _RequestsWithTimeout:
    """What `pronotepy.pronoteAPI` sees as `requests`: the real module, except that `Session()`
    is a `TimeoutSession`. pronotepy creates one session per connection and uses it for the login
    and for every later read (and download), so none of its requests can wait forever."""

    @staticmethod
    def Session() -> TimeoutSession:
        return TimeoutSession(HTTP_TIMEOUT_S)

    def __getattr__(self, name: str) -> Any:
        return getattr(requests, name)


PRONOTE_API: Final[Any] = importlib.import_module("pronotepy.pronoteAPI")
"""The module that creates the `requests.Session` of every connection."""
PRONOTE_API.requests = _RequestsWithTimeout()


def login(credentials: Credentials) -> Any:
    """One PRONOTE token login (the real `connect`). pronotepy rotates the token in the client."""
    return _Client.token_login(
        credentials.url,
        credentials.username,
        credentials.password,
        credentials.uuid,
        client_identifier=credentials.client_identifier,
    )


def qr_login(qr_code: dict[str, str], pin: str, application_uuid: str) -> Any:
    """The one QR-code login of `pronote-setup`."""
    return _Client.qrcode_login(qr_code, pin, application_uuid)


Connect = Callable[[Credentials], Any]


# --- failures ----------------------------------------------------------------------------------

UNAVAILABLE = ("network", "service", "api_changed", "credentials_unwritable")


class SourceUnavailable(PluginError):
    """A transient failure: the guard backs off. The message is a fixed code."""

    def __init__(self, code: str) -> None:
        self.code = code if code in UNAVAILABLE else "api_changed"
        super().__init__(f"degraded: source_unavailable ({self.code})")


def _chain(error: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    pending: list[BaseException | None] = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen or len(seen) > 8:
            continue
        seen.add(id(current))
        yield current
        pending += [current.__cause__, current.__context__]


def classify(error: BaseException, *, logging_in: bool) -> PluginError:
    """`AuthFailure` for wrong credentials, an expired token, a second factor or a lockout;
    `SourceUnavailable` for everything else. While logging in, any error PRONOTE itself coded
    means the server answered and said no, which is not a reason to try again."""
    chain = list(_chain(error))
    api = [e for e in chain if isinstance(e, pronotepy.PronoteAPIError)]
    codes = {e.pronote_error_code for e in api if e.pronote_error_code is not None}
    if LOCKED_OUT_CODE in codes:
        return AuthFailure("locked_out")
    if any(isinstance(e, pronotepy.MFAError) for e in chain):
        return AuthFailure("mfa_failed")
    if any(isinstance(e, _LoginRejected | pronotepy.CryptoError) for e in chain):
        return AuthFailure("login_failed")
    if logging_in and codes:
        return AuthFailure("login_failed")
    if any(isinstance(e, OSError) for e in chain):
        return SourceUnavailable("network")
    if api:
        return SourceUnavailable("service")
    return SourceUnavailable("api_changed")


# --- parameters --------------------------------------------------------------------------------


def strings(name: str, value: object, limit: int = MAX_LIST) -> tuple[str, ...] | None:
    if value is None:
        return None
    if (
        isinstance(value, str)
        or not isinstance(value, Sequence)
        or not 1 <= len(value) <= limit
        or not all(isinstance(v, str) and 1 <= len(v) <= MAX_TEXT_PARAM for v in value)
    ):
        raise PluginError(f"config_invalid: {name} is a list of 1 to {limit} short texts")
    return tuple(value)


@dataclass(frozen=True)
class Options:
    """Which children and periods to read."""

    children: tuple[str, ...] | None = None
    """Names (first name, surname or both, any case or accent) or ids; `None`: all children."""
    periods: tuple[str, ...] | None = None
    """Names of the periods whose grades are read; `None`: the current period."""
    days_ahead: int = 14
    """Homework due from today to this many days ahead."""

    def __post_init__(self) -> None:
        if type(self.days_ahead) is not int or not 1 <= self.days_ahead <= MAX_DAYS_AHEAD:
            raise PluginError(f"config_invalid: days_ahead is 1 to {MAX_DAYS_AHEAD}")


def check_host(call: Call, url: str, extra_hosts: Sequence[str] = ()) -> None:
    """Refuse, before any login, a URL the instance did not approve.

    `call.host_allowed(url)`: `False` refuses (the host is not in `horae.lock`); `True` is
    allowed; `None` (an older core sent no list) falls back to this plugin's own rule: an
    `https` host under `index-education.net`, or one of `extra_hosts` (the `hosts` parameter,
    for a school with a custom domain)."""
    host = url_host(url)
    if host is None:
        raise PluginError("config_invalid: the PRONOTE url must be a plain https url")
    verdict = call.host_allowed(url)
    if verdict is False:
        raise PluginError("network_not_approved: the PRONOTE host is not approved in horae.lock")
    if verdict is None and not (host.endswith(INDEX_EDUCATION) or host in extra_hosts):
        raise PluginError(
            "network_not_approved: the PRONOTE host is not under index-education.net "
            "and not in the hosts parameter"
        )


def _hosts(value: object) -> tuple[str, ...]:
    hosts = strings("hosts", value, 8) or ()
    if not all(_HOST.fullmatch(h) for h in hosts):
        raise PluginError("config_invalid: hosts are lowercase DNS names")
    return hosts


def _resolve(call: Call, path: object, state_path: str) -> str:
    wanted = path if path is not None else os.path.join(os.path.dirname(state_path), DEFAULT_NAME)
    if not isinstance(wanted, str) or not wanted:
        raise PluginError("config_invalid: credentials_path is an absolute path")
    real = call.paths.allowed(wanted, write=True)
    if real is None or os.path.isdir(real) or real == state_path:
        raise PluginError("path_not_approved: credentials_path is not under a path approved")
    return real


# --- reading -----------------------------------------------------------------------------------


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _child_id(raw: str) -> str:
    """An opaque, stable id: the platform's own id is never published."""
    return "c" + hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def _wanted(name: str, child_id: str, wanted: tuple[str, ...] | None) -> bool:
    if wanted is None:
        return True
    parts = {_fold(p) for p in re.split(r"[\s-]+", name) if p}
    full = _fold(" ".join(name.split()))
    return any(_fold(w) in parts or _fold(w) == full or w == child_id for w in wanted)


def _number(text: object) -> float | None:
    try:
        value = float(str(text).replace(",", ".").strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _grade_value(text: object) -> float | str | None:
    if text is None or str(text).strip() == "":
        return None
    number = _number(text)
    return str(text) if number is None else number


def read(
    client: Any, options: Options, today: date
) -> tuple[list[Child], list[Homework], list[Grade], list[Signature]]:
    """Everything the source reads, from a `ReadOnly` client: homework from `today` to
    `days_ahead`, the grades of the chosen periods (newest `MAX_GRADES`), and the unread
    informations (attributed to the first selected child: PRONOTE addresses them to the
    parent). Raises `LookupError` when no child matches or a period is unknown."""
    chosen: list[tuple[Any, Child]] = []
    for info in client.children:
        raw_id, name = str(info.id), str(info.name)
        child_id = _child_id(raw_id)
        if _wanted(name, child_id, options.children):
            chosen.append((info, Child(child_id, name)))
    if not chosen or len(chosen) > MAX_CHILDREN:
        raise LookupError("children")

    periods = list(client.periods)
    if options.periods is None:
        selected = [client.current_period]
    else:
        wanted = {_fold(p) for p in options.periods}
        selected = [p for p in periods if _fold(str(p.name)) in wanted]
        if len(selected) < len(wanted):
            raise LookupError("periods")

    homework: list[Homework] = []
    grades: list[Grade] = []
    signatures: list[Signature] = []
    end = today + timedelta(days=options.days_ahead)
    for position, (info, child) in enumerate(chosen):
        client.set_child(info)
        for item in client.homework(today, end):
            homework.append(
                Homework(
                    child.id,
                    str(item.subject.name),
                    str(item.description),
                    item.date,
                    done=bool(item.done),
                    attachments=len(item.files),
                )
            )
        found: list[Grade] = []
        for period in selected:
            for mark in period.grades:
                found.append(
                    Grade(
                        child.id,
                        str(mark.subject.name),
                        _grade_value(mark.grade),
                        _number(mark.out_of) or 20.0,
                        mark.date,
                        str(mark.comment or ""),
                    )
                )
        found.sort(key=lambda g: g.date, reverse=True)
        grades += found[:MAX_GRADES]
        if position == 0:
            since = datetime.combine(today - timedelta(days=INFO_DAYS), datetime.min.time())
            for info_item in client.information_and_surveys(date_from=since, only_unread=True):
                signatures.append(
                    Signature(
                        child.id,
                        str(info_item.title or "(sans titre)"),
                        info_item.start_date or since,
                        "sondage" if info_item.survey else "information",
                    )
                )
    return [child for _, child in chosen], homework, grades, signatures


# --- the source --------------------------------------------------------------------------------


def run(
    call: Call,
    *,
    account: object,
    state_path: object,
    credentials_path: str | None,
    limits: Limits,
    options: Options,
    hosts: object,
    guard: Guard,
    connect: Connect,
    clock: Clock,
    environ: Mapping[str, str],
) -> Output:
    """One poll of one account (see the module docstring). Raises `Refused` (a `PluginError`
    that the core marks degraded) when the budget says no; no network call happens then."""
    label = account if isinstance(account, str) else ""
    path = _resolve(call, credentials_path, guard.budget.state_path)
    store = CredentialStore(path)
    credentials = store.load(label) or from_secret(environ.get(SECRET), label)
    if credentials is None:
        raise PluginError(
            f"credentials_missing: no credentials for this account; run pronote-setup "
            f"and put its output in the {SECRET} secret or in the credential store"
        )
    check_host(call, credentials.url, _hosts(hosts))
    store.check_writable()
    today = clock().astimezone(guard.budget.zone).date()

    attempt = guard.begin(login=True)  # one attempt; raises Refused
    missing: str | None = None
    with attempt:
        logging_in = True
        try:
            client = ReadOnly(connect(credentials))
            current = _credentials(client)
            _persist(store, label, current)
            logging_in = False
            try:
                result = read(client, options, today)
            except LookupError as error:
                missing = str(error)  # the login worked: not a failure of the account
                result = None
            after = _credentials(client)
            if after != current:
                _persist(store, label, after)
        except PluginError:
            raise
        except Exception as error:
            raise classify(error, logging_in=logging_in) from None
    if result is None:
        raise PluginError(f"config_invalid: no {missing} matches the children or periods asked")
    children, homework, grades, signatures = result
    return normalize(
        label,
        children,
        homework=homework,
        grades=grades,
        signatures=signatures,
        tz=guard.budget.zone,
    )


def _credentials(client: Any) -> Credentials:
    """The client's current credentials (`export_credentials` names the url `pronote_url`)."""
    raw = client.export_credentials()
    return parse(
        {**{k: v for k, v in raw.items() if k != "pronote_url"}, "url": raw.get("pronote_url")}
    )


def _persist(store: CredentialStore, account: str, credentials: Credentials) -> None:
    try:
        store.save(account, credentials)
    except PluginError:
        raise SourceUnavailable("credentials_unwritable") from None


# --- pronote-setup -----------------------------------------------------------------------------

SETUP_USAGE = (
    "usage: python -m horae_school_fr pronote-setup --account LABEL [--qr FILE|-] [--store PATH]"
)


def _qr_code(text: str) -> dict[str, str]:
    try:
        document = json.loads(text)
    except (ValueError, RecursionError):
        raise PluginError("qr_invalid: the QR code text is not JSON") from None
    if (
        not isinstance(document, dict)
        or not {"login", "jeton", "url"} <= set(document)
        or not all(isinstance(document[k], str) for k in ("login", "jeton", "url"))
        or not all(_HEX.fullmatch(document[k]) for k in ("login", "jeton"))
    ):
        raise PluginError("qr_invalid: the QR code needs login, jeton and url")
    return {k: document[k] for k in ("login", "jeton", "url")}


def setup(
    args: Sequence[str],
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    ask_pin: Callable[[], str] | None = None,
    connect: Callable[[dict[str, str], str, str], Any] = qr_login,
    environ: Mapping[str, str] | None = None,
) -> int:
    """`python -m horae_school_fr pronote-setup`, run **once** on the user's own machine.

    Reads the QR code JSON (a file, or `-` for stdin) and the PIN (prompted; or the
    `PRONOTE_PIN` variable), does the QR login **once** (no retry), and prints the credentials
    as `{"<account>": {url, username, password, uuid, client_identifier}}`, the value of the
    `PRONOTE_CREDENTIALS` secret. With `--store PATH` it writes them to the credential store
    instead (mode 0600) and prints nothing secret. Exit status 0, or 1 with a fixed message."""
    out, err, source = stdout or sys.stdout, stderr or sys.stderr, stdin or sys.stdin
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="pronote-setup", add_help=False)
    parser.add_argument("--account", required=True)
    parser.add_argument("--qr", default="-")
    parser.add_argument("--store")
    parser.add_argument("--host", action="append", default=[])
    try:
        parsed = parser.parse_args(list(args))
    except SystemExit:
        err.write(SETUP_USAGE + "\n")
        return 1
    try:
        if not LABEL.fullmatch(parsed.account):
            raise PluginError("config_invalid: --account is a label (A-Z a-z 0-9 . _ -)")
        if parsed.qr == "-":
            text = source.read(65536)
        else:
            with open(parsed.qr, encoding="utf-8") as handle:
                text = handle.read(65536)
        qr = _qr_code(text)
        host = url_host(qr["url"])
        if host is None or not (
            host.endswith(INDEX_EDUCATION) or host in _hosts(parsed.host or None)
        ):
            raise PluginError(
                "network_not_approved: the QR code host is not under index-education.net "
                "(a custom domain: --host NAME)"
            )
        pin = ask_pin() if ask_pin else env.get("PRONOTE_PIN") or getpass.getpass("PIN: ")
        if not re.fullmatch(r"\d{4}", pin):
            raise PluginError("qr_invalid: the PIN is the 4 digits chosen with the QR code")
        try:
            client = connect(qr, pin, str(uuidlib.uuid4()))
            credentials = _credentials(ReadOnly(client))
        except pronotepy.QRCodeDecryptError:
            raise PluginError("login_failed: wrong PIN, or the QR code expired") from None
        except Exception as error:
            failure = classify(error, logging_in=True)
            kind = failure.code if isinstance(failure, AuthFailure) else "unavailable"
            raise PluginError(f"login_failed: the QR login did not work ({kind})") from None
        if parsed.store:
            CredentialStore(parsed.store).save(parsed.account, credentials)
            err.write("Credentials written to the store (mode 0600).\n")
        else:
            json.dump({parsed.account: credentials.to_dict()}, out, indent=2)
            out.write("\n")
            err.write(f"Put the JSON above in the {SECRET} secret. Do not share it.\n")
    except (PluginError, OSError) as error:
        err.write(f"error: {error if isinstance(error, PluginError) else 'cannot read a file'}\n")
        return 1
    return 0


__all__ = [
    "ALLOWED",
    "READ_ATTRIBUTES",
    "READ_METHODS",
    "Options",
    "ReadOnly",
    "ReadOnlyViolation",
    "SourceUnavailable",
    "check_host",
    "classify",
    "login",
    "qr_login",
    "read",
    "run",
    "setup",
    "strings",
]

# SPDX-License-Identifier: Apache-2.0
"""The budget: limits, day rollover, DST, windows, backoff, auth stop, corrupt state."""

import json
import os
import stat
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest
from horae_sdk import PluginError

from conftest import PARIS, ManualClock, paris, utc
from horae_school_fr.guard import (
    Account,
    Attempt,
    AuthFailure,
    Budget,
    Limits,
    Refused,
    Window,
    backoff_delay,
    evaluate,
    instance_timezone,
    parse_duration,
    parse_window,
    utc_now,
)

NO_WINDOW = Limits()
FAST = Limits(max_polls_per_day=12, min_interval=timedelta(minutes=1))
"""For tests that fail often: the interval and the day budget are not what is tested."""


def refusal(budget: Budget, limits: Limits = NO_WINDOW, *, login: bool = False) -> Refused:
    with pytest.raises(Refused) as caught:
        budget.begin("anna", limits, login=login)
    return caught.value


def poll(budget: Budget, limits: Limits = NO_WINDOW, *, login: bool = False) -> None:
    with budget.begin("anna", limits, login=login):
        pass


def saved(state: Path) -> dict[str, dict[str, object]]:
    document = json.loads(state.read_text("utf-8"))
    accounts: dict[str, dict[str, object]] = document["accounts"]
    return accounts


# --- limits ----------------------------------------------------------------------------------


def test_defaults() -> None:
    limits = Limits()
    assert (limits.max_polls_per_day, limits.max_logins_per_day) == (4, 2)
    assert limits.min_interval == timedelta(hours=2)
    assert limits.windows == ()


@pytest.mark.parametrize("value", [0, 13, -1, True, 2.5])
def test_polls_per_day_range(value: object) -> None:
    with pytest.raises(PluginError, match="max_polls_per_day is 1 to 12"):
        Limits(max_polls_per_day=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [1, 12])
def test_polls_per_day_bounds_are_accepted(value: int) -> None:
    assert Limits(max_polls_per_day=value).max_polls_per_day == value


@pytest.mark.parametrize("value", [0, 7, True])
def test_logins_per_day_range(value: object) -> None:
    with pytest.raises(PluginError, match="max_logins_per_day is 1 to 6"):
        Limits(max_logins_per_day=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [timedelta(seconds=59), timedelta(hours=24, seconds=1)])
def test_min_interval_range(value: timedelta) -> None:
    with pytest.raises(PluginError, match="min_interval"):
        Limits(min_interval=value)


def test_durations() -> None:
    assert parse_duration("2h") == timedelta(hours=2)
    assert parse_duration("90m") == timedelta(minutes=90)
    assert parse_duration("45s") == timedelta(seconds=45)
    for bad in ["2", "2 h", "h", "-1h", "1d", "", None, 5]:
        with pytest.raises(PluginError, match="duration"):
            parse_duration(bad)


def test_windows_parse() -> None:
    window = parse_window("07:00-08:30")
    assert window == Window(time(7, 0), time(8, 30))
    assert str(window) == "07:00-08:30"
    for bad in ["7:00-8:00", "07:00", "07:00-07:00", "24:00-01:00", "07:60-08:00", None, 7]:
        with pytest.raises(PluginError, match="window"):
            parse_window(bad)


def test_from_params() -> None:
    limits = Limits.from_params(
        max_polls_per_day=6,
        min_interval="30m",
        windows=["07:00-08:00", "17:00-20:00"],
        max_logins_per_day=1,
    )
    assert limits == Limits(
        6,
        timedelta(minutes=30),
        (Window(time(7), time(8)), Window(time(17), time(20))),
        1,
    )
    with pytest.raises(PluginError, match="windows is a list"):
        Limits.from_params(windows="07:00-08:00")
    with pytest.raises(PluginError, match="at most 8 windows"):
        Limits.from_params(windows=[f"0{n}:00-0{n}:30" for n in range(8)] + ["10:00-10:30"])


def test_the_instance_time_zone_comes_from_tz() -> None:
    assert instance_timezone({"TZ": "Europe/Paris"}).key == "Europe/Paris"  # type: ignore[attr-defined]
    assert instance_timezone({}).key == "UTC"  # type: ignore[attr-defined]
    assert instance_timezone().key  # type: ignore[attr-defined]
    with pytest.raises(PluginError, match="time zone"):
        instance_timezone({"TZ": "Not/AZone"})
    with pytest.raises(PluginError, match="time zone"):
        instance_timezone({"TZ": "../../etc/passwd"})


def test_the_default_clock_is_aware_utc() -> None:
    assert utc_now().utcoffset() == timedelta(0)


def test_a_naive_clock_is_refused(state: Path) -> None:
    naive = Budget(str(state), PARIS, lambda: datetime(2026, 11, 9, 7, 0))
    with pytest.raises(PluginError, match="timezone-aware"):
        naive.begin("anna", NO_WINDOW, login=False)


def test_labels_are_checked(budget: Budget) -> None:
    for bad in ["", "a b", "../x", "x" * 33, 5]:
        with pytest.raises(PluginError, match="account is a label"):
            budget.begin(bad, NO_WINDOW, login=False)  # type: ignore[arg-type]


# --- polls, interval, day rollover -----------------------------------------------------------


def test_a_fresh_account_may_poll_and_the_poll_is_reserved(
    budget: Budget, state: Path, clock: ManualClock
) -> None:
    attempt = budget.begin("anna", NO_WINDOW, login=False)
    assert isinstance(attempt, Attempt)
    # reserved before the attempt runs: a crash in the attempt still counts
    entry = saved(state)["anna"]
    assert len(entry["polls"]) == 1  # type: ignore[arg-type]
    assert entry["logins"] == []


def test_min_interval_between_polls(budget: Budget, clock: ManualClock) -> None:
    poll(budget)
    clock.advance(hours=1, minutes=59, seconds=59)
    error = refusal(budget)
    assert (error.code, error.detail) == ("budget_exhausted", "min_interval")
    assert str(error) == "degraded: budget_exhausted (min_interval)"
    assert error.until == clock.now + timedelta(seconds=1)
    clock.advance(seconds=1)
    poll(budget)


def test_max_polls_per_day(budget: Budget, clock: ManualClock) -> None:
    limits = Limits(max_polls_per_day=3, min_interval=timedelta(minutes=1))
    for _ in range(3):
        poll(budget, limits)
        clock.advance(minutes=1)
    error = refusal(budget, limits)
    assert (error.code, error.detail) == ("budget_exhausted", "polls_per_day")


def test_the_default_gives_four_polls_a_day_two_hours_apart(
    budget: Budget, clock: ManualClock
) -> None:
    clock.set(paris(2026, 11, 9, 8, 0))
    for _ in range(4):
        poll(budget)
        clock.advance(hours=2)
    assert refusal(budget).code == "budget_exhausted"


def test_a_refusal_writes_nothing(budget: Budget, state: Path, clock: ManualClock) -> None:
    poll(budget)
    before = state.read_bytes()
    clock.advance(minutes=5)
    refusal(budget)
    assert state.read_bytes() == before


def test_a_refusal_for_a_new_account_creates_no_state(state: Path, clock: ManualClock) -> None:
    budget = Budget(str(state), PARIS, clock)
    clock.set(paris(2026, 11, 9, 12, 0))
    windows = (Window(time(7), time(8)),)
    with pytest.raises(Refused):
        budget.begin("anna", Limits(windows=windows), login=False)
    assert not state.exists()


def test_accounts_are_independent(budget: Budget, clock: ManualClock) -> None:
    poll(budget)
    with budget.begin("ben", NO_WINDOW, login=False):
        pass
    assert budget.accounts() == ["anna", "ben"]
    assert refusal(budget).code == "budget_exhausted"


def test_the_day_rolls_over_at_local_midnight(budget: Budget, clock: ManualClock) -> None:
    limits = Limits(max_polls_per_day=1, min_interval=timedelta(minutes=1))
    clock.set(paris(2026, 11, 9, 23, 50))  # 22:50 UTC, still the 9th in Paris
    poll(budget, limits)
    clock.set(paris(2026, 11, 9, 23, 59, 59))
    assert refusal(budget, limits).detail == "polls_per_day"
    clock.set(paris(2026, 11, 10, 0, 0))  # 23:00 UTC: the 10th in Paris, the 9th in UTC
    poll(budget, limits)


def test_the_day_is_the_instance_day_not_the_utc_day(budget: Budget, clock: ManualClock) -> None:
    limits = Limits(max_polls_per_day=1, min_interval=timedelta(minutes=1))
    clock.set(utc(2026, 7, 9, 21, 30))  # 23:30 on the 9th in Paris (UTC+2)
    poll(budget, limits)
    clock.set(utc(2026, 7, 9, 22, 30))  # 00:30 on the 10th in Paris, still the 9th in UTC
    poll(budget, limits)
    clock.set(utc(2026, 7, 10, 21, 59))  # 23:59 on the 10th in Paris
    assert refusal(budget, limits).detail == "polls_per_day"
    clock.set(utc(2026, 7, 10, 22, 0))  # 00:00 on the 11th
    poll(budget, limits)


def test_the_day_rolls_over_on_the_spring_forward_day(budget: Budget, clock: ManualClock) -> None:
    # 2026-03-29: 02:00 CET jumps to 03:00 CEST, a 23-hour day (23:00 UTC to 22:00 UTC).
    limits = Limits(max_polls_per_day=1, min_interval=timedelta(minutes=1))
    clock.set(utc(2026, 3, 28, 23, 0))  # 00:00 CET on the 29th
    poll(budget, limits)
    clock.set(utc(2026, 3, 29, 21, 59))  # 23:59 CEST, still the 29th
    assert refusal(budget, limits).detail == "polls_per_day"
    clock.set(utc(2026, 3, 29, 22, 0))  # 00:00 CEST on the 30th, after 23 hours
    poll(budget, limits)


def test_the_day_rolls_over_on_the_fall_back_day(budget: Budget, clock: ManualClock) -> None:
    # 2026-10-25: 03:00 CEST falls back to 02:00 CET, a 25-hour day (22:00 UTC to 23:00 UTC).
    limits = Limits(max_polls_per_day=1, min_interval=timedelta(minutes=1))
    clock.set(utc(2026, 10, 24, 22, 0))  # 00:00 CEST on the 25th
    poll(budget, limits)
    clock.set(utc(2026, 10, 25, 22, 59))  # 23:59 CET, 24h59 later and still the 25th
    assert refusal(budget, limits).detail == "polls_per_day"
    clock.set(utc(2026, 10, 25, 23, 0))  # 00:00 CET on the 26th
    poll(budget, limits)


def test_old_history_is_pruned(budget: Budget, state: Path, clock: ManualClock) -> None:
    limits = Limits(max_polls_per_day=12, min_interval=timedelta(minutes=1))
    for _ in range(12):
        poll(budget, limits)
        clock.advance(minutes=1)
    clock.advance(days=3)
    poll(budget, limits)
    assert len(saved(state)["anna"]["polls"]) == 1  # type: ignore[arg-type]


# --- windows ---------------------------------------------------------------------------------

MORNING_EVENING = Limits(
    max_polls_per_day=12,
    min_interval=timedelta(minutes=1),
    windows=(Window(time(7), time(8)), Window(time(17), time(20))),
)


@pytest.mark.parametrize(
    ("hour", "minute", "second", "allowed"),
    [
        (6, 59, 59, False),
        (7, 0, 0, True),  # the start is inclusive
        (7, 59, 59, True),
        (8, 0, 0, False),  # the end is exclusive
        (12, 0, 0, False),
        (16, 59, 59, False),
        (17, 0, 0, True),
        (19, 59, 59, True),
        (20, 0, 0, False),
        (0, 0, 0, False),
    ],
)
def test_window_edges(
    budget: Budget, clock: ManualClock, hour: int, minute: int, second: int, allowed: bool
) -> None:
    clock.set(paris(2026, 11, 9, hour, minute, second))
    if allowed:
        poll(budget, MORNING_EVENING)
    else:
        error = refusal(budget, MORNING_EVENING)
        assert (error.code, error.reason) == ("outside_window", "outside_window")
        assert str(error) == "degraded: outside_window"


def test_windows_cross_midnight() -> None:
    night = Window(time(22), time(6))
    assert night.contains(time(22, 0))
    assert night.contains(time(23, 59))
    assert night.contains(time(0, 0))
    assert night.contains(time(5, 59))
    assert not night.contains(time(6, 0))
    assert not night.contains(time(21, 59))


def test_windows_follow_the_local_wall_clock_in_summer_and_winter(
    budget: Budget, clock: ManualClock
) -> None:
    limits = Limits(windows=(Window(time(7), time(8)),))
    clock.set(utc(2026, 1, 9, 5, 0))  # 06:00 CET: outside
    assert refusal(budget, limits).code == "outside_window"
    clock.set(utc(2026, 1, 9, 6, 0))  # 07:00 CET
    poll(budget, limits)
    clock.set(utc(2026, 7, 9, 4, 59))  # 06:59 CEST: outside
    assert refusal(budget, limits).code == "outside_window"
    clock.set(utc(2026, 7, 9, 5, 0))  # 07:00 CEST
    poll(budget, limits)


def test_windows_across_the_spring_forward_gap(budget: Budget, clock: ManualClock) -> None:
    # 02:30 on 2026-03-29 does not exist in Paris; the wall clock goes 01:59 CET to 03:00 CEST.
    limits = Limits(windows=(Window(time(2), time(3)),))
    clock.set(utc(2026, 3, 29, 0, 59))  # 01:59 CET
    assert refusal(budget, limits).code == "outside_window"
    clock.set(utc(2026, 3, 29, 1, 0))  # 03:00 CEST: the 02:00-03:00 hour never happened
    assert refusal(budget, limits).code == "outside_window"
    clock.set(utc(2026, 3, 30, 0, 30))  # 02:30 CEST on a normal day
    poll(budget, limits)


def test_windows_across_the_fall_back_repeat(budget: Budget, clock: ManualClock) -> None:
    # 02:00-03:00 happens twice on 2026-10-25 (CEST, then CET); both are inside the window.
    limits = Limits(min_interval=timedelta(minutes=1), windows=(Window(time(2), time(3)),))
    clock.set(utc(2026, 10, 25, 0, 30))  # 02:30 CEST
    poll(budget, limits)
    clock.set(utc(2026, 10, 25, 1, 30))  # 02:30 CET
    poll(budget, limits)
    clock.set(utc(2026, 10, 25, 2, 0))  # 03:00 CET
    assert refusal(budget, limits).code == "outside_window"


# --- logins ----------------------------------------------------------------------------------


def test_logins_are_counted_apart_from_polls(
    budget: Budget, state: Path, clock: ManualClock
) -> None:
    limits = Limits(max_polls_per_day=6, min_interval=timedelta(minutes=1), max_logins_per_day=2)
    poll(budget, limits, login=False)  # cached session: a poll, no login
    clock.advance(minutes=1)
    poll(budget, limits, login=True)
    clock.advance(minutes=1)
    poll(budget, limits, login=True)
    clock.advance(minutes=1)
    assert len(saved(state)["anna"]["logins"]) == 2  # type: ignore[arg-type]
    error = refusal(budget, limits, login=True)
    assert (error.code, error.detail) == ("budget_exhausted", "logins_per_day")
    poll(budget, limits, login=False)  # the session is cached: no login needed, still allowed


def test_a_refused_login_does_not_spend_a_poll(
    budget: Budget, state: Path, clock: ManualClock
) -> None:
    limits = Limits(min_interval=timedelta(minutes=1), max_logins_per_day=1)
    poll(budget, limits, login=True)
    clock.advance(minutes=1)
    refusal(budget, limits, login=True)
    assert len(saved(state)["anna"]["polls"]) == 1  # type: ignore[arg-type]


def test_the_login_budget_rolls_over_with_the_day(budget: Budget, clock: ManualClock) -> None:
    limits = Limits(min_interval=timedelta(minutes=1), max_logins_per_day=1)
    clock.set(paris(2026, 11, 9, 23, 0))
    poll(budget, limits, login=True)
    clock.set(paris(2026, 11, 9, 23, 30))
    assert refusal(budget, limits, login=True).detail == "logins_per_day"
    clock.set(paris(2026, 11, 10, 0, 0))
    poll(budget, limits, login=True)


# --- backoff ---------------------------------------------------------------------------------


def test_backoff_delays() -> None:
    hours = [backoff_delay(n) // timedelta(hours=1) for n in range(1, 9)]
    assert hours == [1, 2, 4, 8, 16, 24, 24, 24]
    assert backoff_delay(0) == timedelta(hours=1)
    assert backoff_delay(10**9) == timedelta(hours=24)


def fail(budget: Budget) -> None:
    with pytest.raises(RuntimeError), budget.begin("anna", FAST, login=False):
        raise RuntimeError("the API changed")


def test_the_backoff_sequence(budget: Budget, state: Path, clock: ManualClock) -> None:
    clock.set(paris(2026, 11, 9, 7, 0))
    for expected_hours in [1, 2, 4, 8, 16, 24, 24]:
        failed_at = clock.now
        fail(budget)
        until = failed_at + timedelta(hours=expected_hours)
        error = refusal(budget)
        assert (error.code, error.until) == ("backoff_until", until)
        assert error.reason == f"backoff_until {until.astimezone(PARIS).isoformat()}"
        assert str(error) == f"degraded: {error.reason}"
        # one second before the end: still refused, and nothing was spent
        clock.set(until - timedelta(seconds=1))
        assert refusal(budget).code == "backoff_until"
        clock.set(until)  # the instant it ends, the poll is allowed (and fails again)
    assert saved(state)["anna"]["failures"] == 7


def test_a_success_clears_the_backoff_streak(
    budget: Budget, state: Path, clock: ManualClock
) -> None:
    fail(budget)
    fail_until = refusal(budget).until
    assert fail_until is not None
    clock.set(fail_until)
    poll(budget, FAST)
    entry = saved(state)["anna"]
    assert (entry["failures"], entry["backoff_until"]) == (0, None)
    clock.advance(hours=2)
    fail(budget)  # the streak starts over: 1 h, not 2 h
    assert refusal(budget).until == clock.now + timedelta(hours=1)


def test_the_backoff_still_applies_after_the_day_rolls_over(
    budget: Budget, clock: ManualClock
) -> None:
    clock.set(paris(2026, 11, 9, 23, 0))
    for _ in range(5):  # 1+2+4+8+16 = 31 h of failures, ending in a 16 h backoff
        fail(budget)
        until = refusal(budget).until
        assert until is not None
        clock.set(until)
    fail(budget)  # the sixth: 24 h
    clock.advance(hours=23)
    assert refusal(budget).code == "backoff_until"
    clock.advance(hours=1)
    poll(budget, FAST)


def test_an_unexpected_error_in_the_attempt_is_a_failure_and_propagates(budget: Budget) -> None:
    with pytest.raises(ZeroDivisionError), budget.begin("anna", NO_WINDOW, login=False):
        1 / 0  # noqa: B018
    assert refusal(budget).code == "backoff_until"


def test_a_plugin_error_in_the_attempt_is_a_transient_failure(budget: Budget) -> None:
    with pytest.raises(PluginError, match="upstream"), budget.begin("anna", NO_WINDOW, login=False):
        raise PluginError("upstream_error: HTTP 500")
    assert refusal(budget).code == "backoff_until"


def test_an_attempt_records_its_outcome_once(budget: Budget, state: Path) -> None:
    attempt = budget.begin("anna", NO_WINDOW, login=False)
    with attempt:
        attempt.failure()  # explicit, then the clean exit must not turn it into a success
    assert saved(state)["anna"]["failures"] == 1
    with pytest.raises(RuntimeError, match="used once"), attempt:
        pass


# --- auth stop -------------------------------------------------------------------------------


def test_an_auth_failure_stops_the_account_completely(
    budget: Budget, state: Path, clock: ManualClock
) -> None:
    with pytest.raises(AuthFailure) as caught, budget.begin("anna", NO_WINDOW, login=True):
        raise AuthFailure("quiz_failed")
    assert str(caught.value) == "degraded: auth_stopped (quiz_failed)"
    entry = saved(state)["anna"]
    assert entry["auth_stopped"] == {"at": clock.now.isoformat(), "code": "quiz_failed"}
    for _ in range(3):  # not an hour, not a day, not a week: only a human lifts it
        clock.advance(days=2)
        error = refusal(budget, login=True)
        assert (error.code, error.detail) == ("auth_stopped", "quiz_failed")
        assert str(error) == "degraded: auth_stopped (quiz_failed)"
    refusal(budget, login=False)


def test_an_auth_stop_does_not_touch_the_other_accounts(budget: Budget) -> None:
    with pytest.raises(AuthFailure), budget.begin("anna", NO_WINDOW, login=True):
        raise AuthFailure
    with budget.begin("ben", NO_WINDOW, login=True):
        pass


def test_the_auth_stop_outranks_a_backoff(clock: ManualClock) -> None:
    both = Account(
        failures=2,
        backoff_until=clock.now + timedelta(hours=2),
        stopped_at=clock.now,
        stop_code="login_failed",
    )
    refused = evaluate(both, NO_WINDOW, clock.now, PARIS, login=False)
    assert refused is not None
    assert refused.code == "auth_stopped"


def test_deleting_the_state_entry_resets_the_account(
    budget: Budget, state: Path, clock: ManualClock
) -> None:
    with pytest.raises(AuthFailure), budget.begin("anna", NO_WINDOW, login=True):
        raise AuthFailure("locked_out")
    with budget.begin("ben", NO_WINDOW, login=True):
        pass
    document = json.loads(state.read_text("utf-8"))
    del document["accounts"]["anna"]  # what the README tells the human to do
    state.write_text(json.dumps(document), "utf-8")
    poll(budget)
    assert "ben" in saved(state)


def test_the_stop_code_is_a_fixed_code_never_free_text(budget: Budget, state: Path) -> None:
    secret = "Mot de passe incorrect pour parent@example.org / hunter2"  # noqa: S105 (synthetic)
    with pytest.raises(AuthFailure) as caught, budget.begin("anna", NO_WINDOW, login=True):
        raise AuthFailure(secret)
    assert caught.value.code == "auth_failed"
    assert secret not in state.read_text("utf-8")
    assert secret not in str(caught.value)
    attempt = budget.begin("ben", NO_WINDOW, login=True)
    attempt.auth_failure("Bad Code!")
    assert saved(state)["ben"]["auth_stopped"]["code"] == "auth_failed"  # type: ignore[index]


# --- no network when refused -----------------------------------------------------------------


def test_a_refused_call_never_reaches_the_network(budget: Budget, clock: ManualClock) -> None:
    calls: list[str] = []

    def source(login: bool) -> str:
        """What a source does: begin first; the network only inside the attempt."""
        with budget.begin("anna", NO_WINDOW, login=login):
            calls.append("network")
        return "ok"

    assert source(True) == "ok"
    for _ in range(3):
        with pytest.raises(Refused):
            source(True)
    assert calls == ["network"]


# --- corrupt or unknown state ----------------------------------------------------------------

GOOD_ACCOUNT = {
    "polls": [],
    "logins": [],
    "failures": 0,
    "backoff_until": None,
    "auth_stopped": None,
}


def document(**account: object) -> bytes:
    return json.dumps({"version": 1, "accounts": {"anna": {**GOOD_ACCOUNT, **account}}}).encode()


BAD_STATES: dict[str, bytes] = {
    "empty": b"",
    "garbage": b"\x00\xff not json",
    "truncated": b'{"version": 1, "accounts": {"anna": {"polls": [',
    "list": b"[]",
    "empty object": b"{}",
    "unknown version": json.dumps({"version": 2, "accounts": {}}).encode(),
    "string version": json.dumps({"version": "1", "accounts": {}}).encode(),
    "extra key": json.dumps({"version": 1, "accounts": {}, "note": "x"}).encode(),
    "accounts not a dict": json.dumps({"version": 1, "accounts": []}).encode(),
    "bad label": json.dumps({"version": 1, "accounts": {"../x": GOOD_ACCOUNT}}).encode(),
    "account not a dict": json.dumps({"version": 1, "accounts": {"anna": 3}}).encode(),
    "missing field": json.dumps(
        {"version": 1, "accounts": {"anna": {"polls": [], "logins": []}}}
    ).encode(),
    "unknown account field": document(extra=1),
    "naive timestamp": document(polls=["2026-11-09T07:00:00"]),
    "non-utc timestamp": document(polls=["2026-11-09T07:00:00+01:00"]),
    "timestamp not text": document(polls=[1762671600]),
    "bad timestamp": document(polls=["yesterday"]),
    "polls not a list": document(polls="x"),
    "too many polls": document(polls=["2026-11-09T07:00:00+00:00"] * 65),
    "negative failures": document(failures=-1),
    "bool failures": document(failures=True),
    "bad stop": document(auth_stopped={"at": "2026-11-09T07:00:00+00:00"}),
    "stop code free text": document(
        auth_stopped={"at": "2026-11-09T07:00:00+00:00", "code": "No!"}
    ),
    "deeply nested": b"[" * 100_000 + b"]" * 100_000,
    "too large": b" " * (1024 * 1024 + 1),
}


@pytest.mark.parametrize("content", BAD_STATES.values(), ids=BAD_STATES.keys())
def test_a_bad_state_file_fails_closed(budget: Budget, state: Path, content: bytes) -> None:
    state.write_bytes(content)
    for account in ("anna", "ben"):  # every account is stopped, including new ones
        with pytest.raises(Refused) as caught:
            budget.begin(account, NO_WINDOW, login=True)
        assert (caught.value.code, caught.value.detail) == ("auth_stopped", "state_invalid")
        assert str(caught.value) == "degraded: auth_stopped (state_invalid)"
    assert state.read_bytes() == content  # left as it was for a human to inspect


def test_an_unreadable_state_file_fails_closed(budget: Budget, state: Path) -> None:
    state.mkdir()  # reading a directory fails with an OSError
    assert refusal(budget).detail == "state_invalid"


def test_recording_an_outcome_never_overwrites_a_file_that_went_bad(
    budget: Budget, state: Path
) -> None:
    attempt = budget.begin("anna", NO_WINDOW, login=False)
    state.write_bytes(b"corrupt")
    with pytest.raises(PluginError, match="state_invalid"), attempt:
        pass
    assert state.read_bytes() == b"corrupt"


def test_a_missing_state_file_is_a_first_run(budget: Budget, state: Path) -> None:
    assert not state.exists()
    poll(budget)
    assert state.exists()


# --- the file --------------------------------------------------------------------------------


def test_the_state_is_written_atomically_with_private_mode(
    budget: Budget, state: Path, tmp_path: Path
) -> None:
    poll(budget)
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json", "state.json.lock"]
    assert stat.S_IMODE((tmp_path / "state.json.lock").stat().st_mode) == 0o600


def test_a_failed_write_leaves_the_old_state_and_no_temporary_file(
    budget: Budget, state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: ManualClock
) -> None:
    poll(budget)
    before = state.read_bytes()

    def broken(source: str, target: str) -> None:
        raise OSError("disk full")

    clock.advance(hours=3)
    monkeypatch.setattr(os, "replace", broken)
    with pytest.raises(PluginError, match="state_unwritable"):
        budget.begin("anna", NO_WINDOW, login=False)
    monkeypatch.undo()
    assert state.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json", "state.json.lock"]


def test_a_missing_directory_is_state_unwritable(tmp_path: Path, clock: ManualClock) -> None:
    broken = Budget(str(tmp_path / "missing" / "state.json"), PARIS, clock)
    with pytest.raises(PluginError, match="state_unwritable"):
        broken.begin("anna", NO_WINDOW, login=False)


def test_the_state_holds_only_times_counters_and_codes(budget: Budget, state: Path) -> None:
    with pytest.raises(AuthFailure), budget.begin("anna", NO_WINDOW, login=True):
        raise AuthFailure("login_failed")
    assert json.loads(state.read_text("utf-8")) == {
        "version": 1,
        "accounts": {
            "anna": {
                "polls": ["2026-11-09T06:00:00+00:00"],
                "logins": ["2026-11-09T06:00:00+00:00"],
                "failures": 0,
                "backoff_until": None,
                "auth_stopped": {"at": "2026-11-09T06:00:00+00:00", "code": "login_failed"},
            }
        },
    }


# --- peek ------------------------------------------------------------------------------------


def test_peek_reports_the_state_without_changing_it(
    budget: Budget, state: Path, tmp_path: Path, clock: ManualClock
) -> None:
    poll(budget, login=True)
    before = state.read_bytes()
    clock.advance(minutes=30)
    report = budget.peek("anna", NO_WINDOW)
    assert report == {
        "account": "anna",
        "state": "ok",
        "would_refuse": "budget_exhausted",
        "polls_today": 1,
        "max_polls_per_day": 4,
        "logins_today": 1,
        "max_logins_per_day": 2,
        "failures": 0,
        "last_poll_at": "2026-11-09T07:00:00+01:00",
        "next_poll_not_before": "2026-11-09T09:00:00+01:00",
        "backoff_until": None,
        "auth_stop_code": None,
        "auth_stopped_at": None,
    }
    assert state.read_bytes() == before


def test_peek_of_an_unknown_account_and_of_the_other_states(
    budget: Budget, clock: ManualClock
) -> None:
    assert budget.peek("new", NO_WINDOW)["would_refuse"] is None
    fail(budget)
    report = budget.peek("anna", NO_WINDOW)
    assert (report["state"], report["would_refuse"]) == (
        "backoff",
        "backoff_until 2026-11-09T08:00:00+01:00",
    )
    with pytest.raises(AuthFailure), budget.begin("ben", NO_WINDOW, login=True):
        raise AuthFailure("locked_out")
    report = budget.peek("ben", NO_WINDOW)
    assert (report["state"], report["auth_stop_code"]) == ("auth_stopped", "locked_out")
    assert report["would_refuse"] == "auth_stopped"


def test_peek_of_a_bad_file(budget: Budget, state: Path) -> None:
    state.write_bytes(b"corrupt")
    assert budget.peek("anna", NO_WINDOW) == {
        "account": "anna",
        "state": "invalid",
        "would_refuse": "auth_stopped",
    }
    assert budget.accounts() == []

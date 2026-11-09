# SPDX-License-Identifier: Apache-2.0
"""`school-fr.status` through MCP, and `guard_for`, the entry point of the later sources."""

from pathlib import Path
from typing import Any

import anyio
import pytest
from horae_sdk import Call, Paths, PluginError
from horae_sdk.testing import Replayed
from horae_sdk.testing import call as call_tool

from conftest import ManualClock, paris
from horae_school_fr import Guard, Limits, Refused, build, guard_for
from horae_school_fr.guard import AuthFailure

ENV = {"TZ": "Europe/Paris"}


def run(state: Path, clock: ManualClock, *, root: Path | None = None, **params: Any) -> Replayed:
    plugin = build(clock=clock, environ=ENV)

    async def go() -> Replayed:
        return await call_tool(
            plugin,
            "school-fr.status",
            {"state_path": str(state), **params},
            paths={"write": [str(root or state.parent)]},
        )

    return anyio.run(go)


def call_with(root: Path) -> Call:
    return Call("school-fr.x", paths=Paths(write=(str(root),)))


def test_status_of_an_empty_state(state: Path, clock: ManualClock, tmp_path: Path) -> None:
    result = run(state, clock)
    assert result.error is None
    assert result.data == {"accounts": []}
    assert result.intents == []
    assert list(tmp_path.iterdir()) == []  # a diagnostic writes nothing, not even a lock


def test_status_reports_each_account_and_never_writes(
    state: Path, clock: ManualClock, tmp_path: Path
) -> None:
    guard = guard_for(
        call_with(tmp_path),
        state_path=str(state),
        account="anna",
        limits=Limits(),
        clock=clock,
        environ=ENV,
    )
    with guard.begin(login=True):
        pass
    other = guard_for(
        call_with(tmp_path),
        state_path=str(state),
        account="ben",
        limits=Limits(),
        clock=clock,
        environ=ENV,
    )
    with pytest.raises(AuthFailure), other.begin(login=True):
        raise AuthFailure("quiz_failed")
    before = sorted((p.name, p.read_bytes()) for p in tmp_path.iterdir())
    clock.advance(minutes=10)
    result = run(state, clock)
    assert result.error is None
    anna, ben = result.data["accounts"]
    assert (anna["account"], anna["state"], anna["polls_today"], anna["logins_today"]) == (
        "anna",
        "ok",
        1,
        1,
    )
    assert anna["would_refuse"] == "budget_exhausted"
    assert (ben["state"], ben["auth_stop_code"], ben["would_refuse"]) == (
        "auth_stopped",
        "quiz_failed",
        "auth_stopped",
    )
    assert sorted((p.name, p.read_bytes()) for p in tmp_path.iterdir()) == before


def test_status_takes_the_limits_and_the_accounts(state: Path, clock: ManualClock) -> None:
    clock.set(paris(2026, 11, 9, 12, 0))
    result = run(
        state,
        clock,
        accounts=["new"],
        max_polls_per_day=2,
        min_interval="30m",
        windows=["07:00-08:00"],
        max_logins_per_day=1,
    )
    (report,) = result.data["accounts"]
    assert (report["max_polls_per_day"], report["max_logins_per_day"]) == (2, 1)
    assert report["would_refuse"] == "outside_window"


def test_status_of_a_corrupt_file_says_so(state: Path, clock: ManualClock) -> None:
    state.write_text("{nope", "utf-8")
    result = run(state, clock, accounts=["anna"])
    assert result.error is None
    assert result.data["accounts"] == [
        {"account": "anna", "state": "invalid", "would_refuse": "auth_stopped"}
    ]


def test_status_refuses_a_path_that_is_not_approved(
    state: Path, clock: ManualClock, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = run(state, clock, root=elsewhere)
    assert result.error is not None
    assert "path_not_approved" in result.error


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"max_polls_per_day": 13}, "max_polls_per_day is 1 to 12"),
        ({"min_interval": "soon"}, "duration"),
        ({"windows": ["25:00-26:00"]}, "window"),
        ({"accounts": ["a", "a"]}, "distinct"),
        ({"accounts": ["a b"]}, "account is a label"),
    ],
)
def test_status_checks_its_parameters(
    state: Path, clock: ManualClock, params: dict[str, Any], message: str
) -> None:
    result = run(state, clock, **params)
    assert result.error is not None
    assert message in result.error


def test_status_needs_a_valid_time_zone(state: Path, clock: ManualClock) -> None:
    plugin = build(clock=clock, environ={"TZ": "Nowhere/Land"})

    async def go() -> Replayed:
        return await call_tool(
            plugin,
            "school-fr.status",
            {"state_path": str(state)},
            paths={"write": [str(state.parent)]},
        )

    result = anyio.run(go)
    assert result.error is not None
    assert "time zone" in result.error


def test_guard_for_checks_the_path_and_the_account(
    state: Path, clock: ManualClock, tmp_path: Path
) -> None:
    def make(call: Call, path: object, account: object = "anna") -> Guard:
        return guard_for(
            call, state_path=path, account=account, limits=Limits(), clock=clock, environ=ENV
        )

    assert isinstance(make(call_with(tmp_path), str(state)), Guard)
    for bad in (None, "", "relative.json", str(tmp_path)):
        with pytest.raises(PluginError, match=r"state_path|path_not_approved"):
            make(call_with(tmp_path), bad)
    with pytest.raises(PluginError, match="not under a path approved"):
        make(Call("school-fr.x"), str(state))
    with pytest.raises(PluginError, match="account is a label"):
        make(call_with(tmp_path), str(state), 3)


def test_a_source_built_on_the_guard_degrades_without_a_network_call(
    state: Path, clock: ManualClock, tmp_path: Path
) -> None:
    """What #2 and #3 do: guard first, network only inside the attempt."""
    network: list[str] = []

    def source() -> str:
        guard = guard_for(
            call_with(tmp_path),
            state_path=str(state),
            account="anna",
            limits=Limits(),
            clock=clock,
            environ=ENV,
        )
        with guard.begin(login=True):
            network.append("login")
        return "data"

    assert source() == "data"
    with pytest.raises(Refused, match=r"^degraded: budget_exhausted"):
        source()
    assert network == ["login"]

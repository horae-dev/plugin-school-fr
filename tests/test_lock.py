# SPDX-License-Identifier: Apache-2.0
"""Two overlapping runs cannot both log in: the file lock, across real processes."""

import fcntl
import json
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import PARIS, ManualClock
from horae_school_fr.guard import Budget, Limits, Refused

WORKER = """
import sys, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from horae_school_fr.guard import Budget, Limits, Refused

state, go = sys.argv[1], Path(sys.argv[2])
now = datetime.fromisoformat("2026-11-09T07:00:00+01:00")
budget = Budget(state, ZoneInfo("Europe/Paris"), lambda: now)
while not go.exists():
    time.sleep(0.005)
try:
    with budget.begin("anna", Limits(), login=True):
        time.sleep(0.2)  # the "login", slow enough for the others to overlap it
    print("logged-in")
except Refused as refused:
    print("refused", refused.code, refused.detail)
"""


def run_workers(state: Path, count: int) -> list[str]:
    go = state.parent / "go"
    workers = [
        subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", WORKER, str(state), str(go)],
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(count)
    ]
    go.touch()
    outputs = []
    for worker in workers:
        out, _ = worker.communicate(timeout=60)
        assert worker.returncode == 0
        outputs.append(out.strip())
    return outputs


def test_overlapping_processes_cannot_both_log_in(state: Path) -> None:
    outputs = run_workers(state, 6)
    assert outputs.count("logged-in") == 1
    assert outputs.count("refused budget_exhausted min_interval") == 5
    entry = json.loads(state.read_text("utf-8"))["accounts"]["anna"]
    assert (len(entry["polls"]), len(entry["logins"])) == (1, 1)


def test_a_held_lock_refuses_instead_of_passing(state: Path, clock: ManualClock) -> None:
    budget = Budget(str(state), PARIS, clock, lock_timeout=0.1)
    handle = open(f"{state}.lock", "w")  # noqa: SIM115
    fcntl.flock(handle, fcntl.LOCK_EX)
    try:
        started = time.monotonic()
        with pytest.raises(Refused) as caught:
            budget.begin("anna", Limits(), login=True)
        assert 0.1 <= time.monotonic() - started < 5
        assert (caught.value.code, caught.value.detail) == ("budget_exhausted", "state_busy")
        assert not state.exists()
    finally:
        handle.close()
    with budget.begin("anna", Limits(), login=True):  # released: the next run goes through
        pass


def test_the_lock_is_released_after_a_refusal_and_after_an_error(
    state: Path, clock: ManualClock
) -> None:
    budget = Budget(str(state), PARIS, clock, lock_timeout=0.1)
    with budget.begin("anna", Limits(min_interval=timedelta(minutes=1)), login=False):
        pass
    with pytest.raises(Refused):
        budget.begin("anna", Limits(), login=False)
    with open(f"{state}.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # would raise if still held

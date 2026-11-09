# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures: a manual clock, so no test depends on the real time or the real zone."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from horae_school_fr.guard import Budget

PARIS = ZoneInfo("Europe/Paris")


class ManualClock:
    """A clock the test moves. Built from a local Paris wall time or a UTC instant."""

    def __init__(self, now: datetime) -> None:
        self.now = now.astimezone(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)

    def set(self, now: datetime) -> None:
        self.now = now.astimezone(UTC)


def paris(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=PARIS)


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(paris(2026, 11, 9, 7, 0))


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state.json"


@pytest.fixture
def budget(state: Path, clock: ManualClock) -> Budget:
    return Budget(str(state), PARIS, clock, lock_timeout=0.3)

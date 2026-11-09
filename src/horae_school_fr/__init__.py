# SPDX-License-Identifier: Apache-2.0
"""The school-fr Horae plugin: the lockout guard for PRONOTE and ÉcoleDirecte."""

from horae_school_fr.guard import (
    REASONS,
    Attempt,
    AuthFailure,
    Budget,
    Guard,
    Limits,
    Refused,
)
from horae_school_fr.plugin import COMMAND, NAME, build, guard_for, manifest

__all__ = [
    "COMMAND",
    "NAME",
    "REASONS",
    "Attempt",
    "AuthFailure",
    "Budget",
    "Guard",
    "Limits",
    "Refused",
    "build",
    "guard_for",
    "manifest",
]

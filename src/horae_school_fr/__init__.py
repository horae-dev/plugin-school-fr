# SPDX-License-Identifier: Apache-2.0
"""The school-fr Horae plugin: the lockout guard, the normalizer and the school sources."""

from horae_school_fr.credentials import Credentials, CredentialStore
from horae_school_fr.guard import (
    REASONS,
    Attempt,
    AuthFailure,
    Budget,
    Guard,
    Limits,
    Refused,
)
from horae_school_fr.normalize import (
    DEIDENTIFIED_FIELDS,
    Child,
    Grade,
    Homework,
    NameScrubber,
    NormalizeError,
    Signature,
    input_view,
    normalize,
)
from horae_school_fr.plugin import COMMAND, NAME, build, guard_for, manifest

__all__ = [
    "COMMAND",
    "DEIDENTIFIED_FIELDS",
    "NAME",
    "REASONS",
    "Attempt",
    "AuthFailure",
    "Budget",
    "Child",
    "CredentialStore",
    "Credentials",
    "Grade",
    "Guard",
    "Homework",
    "Limits",
    "NameScrubber",
    "NormalizeError",
    "Refused",
    "Signature",
    "build",
    "guard_for",
    "input_view",
    "manifest",
    "normalize",
]

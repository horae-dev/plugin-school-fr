# SPDX-License-Identifier: Apache-2.0
"""What the PRONOTE and ÉcoleDirecte sources share: the transient-failure type, the parameter
checks, the credential-path check and the small helpers that map platform data to the
normalizer's model. Independent of either platform's library."""

import hashlib
import math
import os
import re
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Final

from horae_sdk import Call, PluginError

MAX_LIST: Final = 16
MAX_TEXT_PARAM: Final = 64
MAX_DAYS_AHEAD: Final = 60
UNAVAILABLE: Final = ("network", "service", "api_changed", "credentials_unwritable")


class SourceUnavailable(PluginError):
    """A transient failure: the guard backs off. The message is a fixed code."""

    def __init__(self, code: str) -> None:
        self.code = code if code in UNAVAILABLE else "api_changed"
        super().__init__(f"degraded: source_unavailable ({self.code})")


def chain(error: BaseException) -> Iterator[BaseException]:
    """`error` and its causes and contexts (a bounded walk)."""
    seen: set[int] = set()
    pending: list[BaseException | None] = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen or len(seen) > 8:
            continue
        seen.add(id(current))
        yield current
        pending += [current.__cause__, current.__context__]


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


def resolve_path(call: Call, path: object, state_path: str, default_name: str) -> str:
    """The credential store's path: `path`, else `default_name` next to the guard state; it must
    be under a directory approved for writing and not be the guard state itself."""
    wanted = path if path is not None else os.path.join(os.path.dirname(state_path), default_name)
    if not isinstance(wanted, str) or not wanted:
        raise PluginError("config_invalid: credentials_path is an absolute path")
    real = call.paths.allowed(wanted, write=True)
    if real is None or os.path.isdir(real) or real == state_path:
        raise PluginError("path_not_approved: credentials_path is not under a path approved")
    return real


def fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def child_id(raw: str) -> str:
    """An opaque, stable id: the platform's own id is never published."""
    return "c" + hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:12]


def wanted_child(name: str, opaque_id: str, wanted: tuple[str, ...] | None) -> bool:
    """Whether a child is selected by `wanted` (first name, surname or both, any case or accent,
    or the opaque id); `None` selects all."""
    if wanted is None:
        return True
    parts = {fold(p) for p in re.split(r"[\s-]+", name) if p}
    full = fold(" ".join(name.split()))
    return any(fold(w) in parts or fold(w) == full or w == opaque_id for w in wanted)


def number(text: object) -> float | None:
    try:
        value = float(str(text).replace(",", ".").strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def grade_value(text: object) -> float | str | None:
    if text is None or str(text).strip() == "":
        return None
    parsed = number(text)
    return str(text) if parsed is None else parsed


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

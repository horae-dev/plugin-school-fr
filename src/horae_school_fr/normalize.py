# SPDX-License-Identifier: Apache-2.0
"""Normalization of school items and the de-identification contract (story #4).

The PRONOTE and ÉcoleDirecte sources map what the platform returns to the small internal model
below (`Child`, `Homework`, `Grade`, `Signature`) and return `normalize(...)`: the source data
and the `task.create` intents.

What becomes what:

- **Homework** is a task only while it is **not done**; a done one is data only.
- **Notes and grades** are data only, never tasks.
- **Things to sign or acknowledge** are tasks.

A task's fingerprint is `school-fr:<account>:<kind>:<hash>` (kind `homework` or `signature`),
the hash being the SHA-256 of the stable fields only (child id, subject or title, date, text), so
a change of the done flag or of the attachments count never makes a second task. The due date is
the item's date in the **instance time zone** (`TZ`). The title is escaped by the SDK
(`horae_sdk.tasks.escape_title`, threat T02) and capped at `MAX_TITLE`.

**De-identification.** The core's contract (schemas decision 47, `horae.llm.deid`) is declared by
the **skill that consumes the data**, in its `input_view` (`skill.yaml`): a path with
`{class: person}` is replaced by a pseudonym before a cloud model is called and restored in the
answer. A plugin's manifest and a source's output carry no such annotation, so this module
publishes the declaration as data (`input_view`, `DEIDENTIFIED_FIELDS`) for the skills built on
this source to copy, and a test runs it through the core (`tests/test_deid.py`).
Once a skill declares any class, the core also scans **every** other string of the viewed input
for the known values (the declared names), so a name inside a homework description is covered.

Since horae-sdk 0.7 the plugin also declares `DEIDENTIFIED_FIELDS` as `personal_fields` of its
sources (decision 68), which the core enforces without any skill class, and its scan is accent-
folded. As defence in depth `NameScrubber` rewrites, in the free text of
the **source data**, every whole-word occurrence of a declared name in any case or accent form
(`zoe`, `ZOÉ`, a decomposed `é`) to the declared spelling. The core then pseudonymizes and
restores it like any other occurrence. It does not catch nicknames, initials, typos or names of
fewer than `MIN_NAME_LETTERS` letters (the core skips those too).
"""

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from typing import Any, Final

from horae_sdk import Output
from horae_sdk.intents import fingerprint, task_create
from horae_sdk.tasks import escape_title

from horae_school_fr.guard import LABEL, instance_timezone

KIND_HOMEWORK: Final = "homework"
KIND_SIGNATURE: Final = "signature"
MAX_TITLE: Final = 160
"""Longest task title (after escaping, an ellipsis included)."""
MAX_ITEMS: Final = 500
"""Most items of each kind in one call."""
MAX_CHILDREN: Final = 16
MAX_SUBJECT: Final = 80
MAX_TEXT: Final = 1000
"""Longest description or title kept in the data."""
MAX_COMMENT: Final = 500
MIN_NAME_LETTERS: Final = 3
"""Shortest name (or part of a name) looked for in text, as in the core."""
HASH_CHARS: Final = 20

_ID = re.compile(r"[A-Za-z0-9._-]{1,32}")
_LETTERS = re.compile(r"[^\W\d_]+")
_SPACE = re.compile(r"\s+")

CLASS_PERSON: Final = "person"
CLASS_FREE_TEXT: Final = "free_text"

DEIDENTIFIED_FIELDS: Final[Mapping[str, str]] = {
    "children[].first_name": CLASS_PERSON,
    "homework[].teacher": CLASS_PERSON,
    "grades[].teacher": CLASS_PERSON,
}
"""The fields of `data` that hold a person's name, by path (relative to `data`), and their
class. A skill built on this source declares them in its `input_view`."""

_FREE_TEXT_FIELDS: Final[tuple[str, ...]] = (
    "homework[].description",
    "grades[].comment",
    "signatures[].title",
    "signatures[].kind",
)
_PLAIN_FIELDS: Final[tuple[str, ...]] = (
    "account",
    "children[].id",
    "homework[].child_id",
    "homework[].subject",
    "homework[].due",
    "homework[].done",
    "homework[].attachments",
    "grades[].child_id",
    "grades[].subject",
    "grades[].value",
    "grades[].out_of",
    "grades[].date",
    "signatures[].child_id",
    "signatures[].date",
)


def input_view(prefix: str = "[].data.") -> dict[str, dict[str, str] | None]:
    """The `input_view` of a skill that reads this source's data, whole: every field, the names
    declared `class: person` and the texts written by others `class: free_text`. `prefix` is
    where `data` sits in the step input (`[].data.` after a `collect` step: its output is an
    array of `{source, data}`). Copy the lines the skill needs; keep every `person` line."""
    view: dict[str, dict[str, str] | None] = {}
    for path in _PLAIN_FIELDS:
        view[prefix + path] = None
    for path in _FREE_TEXT_FIELDS:
        view[prefix + path] = {"class": CLASS_FREE_TEXT}
    for path, name in DEIDENTIFIED_FIELDS.items():
        view[prefix + path] = {"class": name}
    return dict(sorted(view.items()))


class NormalizeError(ValueError):
    """The items cannot be normalized (an unknown child, a bad account label, too many items)."""


# --- the model ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Child:
    """A child of the account. `id` is the platform's opaque id (never a name)."""

    id: str
    first_name: str


@dataclass(frozen=True)
class Homework:
    """Work to do. `due` is a date, or a datetime (aware: converted to the instance time zone;
    naive: taken as already in it); `None` when the platform gives none."""

    child_id: str
    subject: str
    description: str
    due: date | datetime | None
    done: bool = False
    attachments: int = 0
    teacher: str | None = None


@dataclass(frozen=True)
class Grade:
    """A grade (data only). `value` is a number, or a platform's text such as `Abs`."""

    child_id: str
    subject: str
    value: float | str | None
    out_of: float
    date: date | datetime
    comment: str = ""
    teacher: str | None = None


@dataclass(frozen=True)
class Signature:
    """Something to sign or acknowledge (a task). `kind` is the platform's category."""

    child_id: str
    title: str
    date: date | datetime
    kind: str


# --- names in text -----------------------------------------------------------------------------


def _clean(text: str) -> str:
    """NFKC without format characters (zero-width, bidi, soft hyphen): what the core scans."""
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def _fold(word: str) -> str:
    """Case- and accent-folded: `Zoé`, `ZOE` and `zoe` are one."""
    decomposed = unicodedata.normalize("NFKD", word.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


class NameScrubber:
    """Rewrites known names in text to their declared spelling (see the module docstring)."""

    def __init__(self, names: Iterable[str]) -> None:
        self._canonical: dict[str, str] = {}
        for name in names:
            for part in _LETTERS.findall(_clean(name)):
                if len(part) >= MIN_NAME_LETTERS:
                    self._canonical.setdefault(_fold(part), part)

    def __bool__(self) -> bool:
        return bool(self._canonical)

    def knows(self, text: str) -> bool:
        """Whether `text` is, once folded, a known name or a part of one."""
        return _fold(_clean(text)) in self._canonical

    def scrub(self, text: str) -> str:
        """`text` cleaned, with every whole-word known name (any case or accent form) spelt as
        declared. Other text is unchanged (apart from NFKC and format characters)."""
        text = _clean(text)
        if not self._canonical:
            return text
        return _LETTERS.sub(lambda m: self._canonical.get(_fold(m.group()), m.group()), text)


# --- normalization -----------------------------------------------------------------------------


def _day(value: date | datetime, tz: tzinfo) -> date:
    if isinstance(value, datetime):
        return value.astimezone(tz).date() if value.tzinfo is not None else value.date()
    return value


def _text(value: str, limit: int) -> str:
    return _SPACE.sub(" ", value).strip()[:limit]


def _stable(*parts: object) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8", "surrogatepass")).hexdigest()[:HASH_CHARS]


def _key(account: str, kind: str, *stable: object) -> str:
    return fingerprint("school-fr", account, kind, _stable(*stable))


def _title(child: Child, text: str) -> str:
    return escape_title(f"{child.first_name} - {text}", max_length=MAX_TITLE)


def _bounded(name: str, items: Sequence[object]) -> None:
    if len(items) > MAX_ITEMS:
        raise NormalizeError(f"too many {name}: at most {MAX_ITEMS} per call")


def normalize(
    account: str,
    children: Sequence[Child],
    *,
    homework: Sequence[Homework] = (),
    grades: Sequence[Grade] = (),
    signatures: Sequence[Signature] = (),
    tz: tzinfo | None = None,
    environ: Mapping[str, str] | None = None,
) -> Output:
    """The source output for `account`: `data` holds the children, homework, grades and
    signatures (dates as `YYYY-MM-DD` in the instance time zone, free text cleaned and capped,
    names spelt as declared) and `intents` one `task.create` per undone homework and per
    signature. `tz` defaults to the instance time zone read from `environ["TZ"]`.

    Raises `NormalizeError` for a bad account label, an item of an unknown child, a child id
    that is a name, or too many items."""
    if not LABEL.fullmatch(account):
        raise NormalizeError("account is a label (A-Z a-z 0-9 . _ -, at most 32)")
    zone = instance_timezone(environ) if tz is None else tz
    if not 1 <= len(children) <= MAX_CHILDREN:
        raise NormalizeError(f"an account has 1 to {MAX_CHILDREN} children")
    by_id = {child.id: child for child in children}
    if len(by_id) != len(children) or not all(_ID.fullmatch(child.id) for child in children):
        raise NormalizeError("child ids are distinct opaque labels (A-Z a-z 0-9 . _ -, at most 32)")
    for name, items in (("homework", homework), ("grades", grades), ("signatures", signatures)):
        _bounded(name, items)
        if any(item.child_id not in by_id for item in items):
            raise NormalizeError(f"a {name} item belongs to an unknown child")

    names = [child.first_name for child in children]
    names += [w.teacher for w in homework if w.teacher]
    names += [g.teacher for g in grades if g.teacher]
    scrubber = NameScrubber(names)
    if any(scrubber.knows(child.id) for child in children):
        raise NormalizeError("a child id must be opaque, not a name")

    def free(value: str, limit: int) -> str:
        return scrubber.scrub(_text(value, limit))

    data_homework: list[dict[str, Any]] = []
    data_grades: list[dict[str, Any]] = []
    data_signatures: list[dict[str, Any]] = []
    intents: list[dict[str, Any]] = []

    for work in homework:
        child = by_id[work.child_id]
        due = None if work.due is None else _day(work.due, zone)
        subject = free(work.subject, MAX_SUBJECT)
        description = free(work.description, MAX_TEXT)
        row: dict[str, Any] = {
            "child_id": work.child_id,
            "subject": subject,
            "description": description,
            "due": None if due is None else due.isoformat(),
            "done": bool(work.done),
            "attachments": max(0, int(work.attachments)),
        }
        if work.teacher:
            row["teacher"] = _text(work.teacher, MAX_SUBJECT)
        data_homework.append(row)
        if not work.done:
            stable = (
                child.id,
                _text(work.subject, MAX_SUBJECT).casefold(),
                due and due.isoformat(),
            )
            raw = _SPACE.sub(" ", work.description).strip().casefold()
            intents.append(
                task_create(
                    _title(child, f"{subject} : {description}"),
                    key=_key(account, KIND_HOMEWORK, *stable, raw),
                    due=due,
                )
            )

    for grade in grades:
        row = {
            "child_id": grade.child_id,
            "subject": free(grade.subject, MAX_SUBJECT),
            "value": grade.value,
            "out_of": grade.out_of,
            "date": _day(grade.date, zone).isoformat(),
            "comment": free(grade.comment, MAX_COMMENT),
        }
        if grade.teacher:
            row["teacher"] = _text(grade.teacher, MAX_SUBJECT)
        data_grades.append(row)

    for sign in signatures:
        child = by_id[sign.child_id]
        day = _day(sign.date, zone)
        title = free(sign.title, MAX_TEXT)
        kind = free(sign.kind, MAX_SUBJECT)
        data_signatures.append(
            {
                "child_id": sign.child_id,
                "title": title,
                "date": day.isoformat(),
                "kind": kind,
            }
        )
        raw_title = _SPACE.sub(" ", sign.title).strip().casefold()
        intents.append(
            task_create(
                _title(child, f"À signer ({kind}) : {title}" if kind else f"À signer : {title}"),
                key=_key(
                    account,
                    KIND_SIGNATURE,
                    child.id,
                    raw_title,
                    day.isoformat(),
                    _text(sign.kind, MAX_SUBJECT).casefold(),
                ),
                due=day,
            )
        )

    data = {
        "account": account,
        "children": [{"id": child.id, "first_name": child.first_name} for child in children],
        "homework": data_homework,
        "grades": data_grades,
        "signatures": data_signatures,
    }
    return Output(data, intents)


__all__ = [
    "CLASS_FREE_TEXT",
    "CLASS_PERSON",
    "DEIDENTIFIED_FIELDS",
    "KIND_HOMEWORK",
    "KIND_SIGNATURE",
    "MAX_TITLE",
    "Child",
    "Grade",
    "Homework",
    "NameScrubber",
    "NormalizeError",
    "Signature",
    "input_view",
    "normalize",
]

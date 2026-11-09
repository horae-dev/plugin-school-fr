# SPDX-License-Identifier: Apache-2.0
"""The normalizer: what becomes data, what becomes a task, fingerprints, dates and titles."""

import dataclasses
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from horae_sdk.intents import MAX_FINGERPRINT
from horae_sdk.tasks import plain_text

from fixtures_school import (
    ACCOUNT,
    CHILDREN,
    GRADES,
    HOMEWORK,
    PARIS,
    SIGNATURES,
)
from horae_school_fr import (
    Child,
    Homework,
    NameScrubber,
    NormalizeError,
    Signature,
    normalize,
)
from horae_school_fr.normalize import MAX_ITEMS, MAX_TITLE


def run(**kwargs: Any) -> Any:
    options: dict[str, Any] = {
        "homework": HOMEWORK,
        "grades": GRADES,
        "signatures": SIGNATURES,
        "tz": PARIS,
    }
    return normalize(ACCOUNT, CHILDREN, **{**options, **kwargs})


def test_homework_not_done_and_signatures_are_tasks_grades_are_data_only() -> None:
    out = run()
    assert len(out.intents) == 4  # 2 undone homework + 2 signatures
    assert {i["type"] for i in out.intents} == {"task.create"}
    kinds = [i["fingerprint"].split(":")[2] for i in out.intents]
    assert sorted(kinds) == ["homework", "homework", "signature", "signature"]
    assert len(out.data["grades"]) == 2
    assert len(out.data["homework"]) == 3  # the done one stays in the data
    assert not any("Maths" in i["title"] for i in out.intents)  # done: no task
    assert not any("Anglais" in i["title"] for i in out.intents)  # grades: no task


def test_a_done_homework_has_no_task_and_an_undone_one_does() -> None:
    work = Homework("c1", "Maths", "Exercice 1", date(2026, 11, 10))
    assert len(run(homework=[work], grades=[], signatures=[]).intents) == 1
    done = dataclasses.replace(work, done=True)
    out = run(homework=[done], grades=[], signatures=[])
    assert out.intents == []
    assert out.data["homework"][0]["done"] is True


def test_fingerprints_are_stable_and_follow_the_scheme() -> None:
    first = [i["fingerprint"] for i in run().intents]
    again = [i["fingerprint"] for i in run().intents]
    assert first == again
    assert len(set(first)) == len(first)
    for key in first:
        assert re.fullmatch(rf"school-fr:{ACCOUNT}:(homework|signature):[0-9a-f]{{20}}", key)
        assert len(key) <= MAX_FINGERPRINT


def test_unstable_fields_do_not_change_the_fingerprint() -> None:
    work = Homework("c1", "Maths", "Exercice 1", date(2026, 11, 10), attachments=0)
    changed = dataclasses.replace(work, attachments=3, teacher="Mme Autre")
    one = run(homework=[work], grades=[], signatures=[]).intents[0]["fingerprint"]
    two = run(homework=[changed], grades=[], signatures=[]).intents[0]["fingerprint"]
    assert one == two
    spaced = dataclasses.replace(work, description="  EXERCICE   1 ")
    assert run(homework=[spaced], grades=[], signatures=[]).intents[0]["fingerprint"] == one


@pytest.mark.parametrize(
    "change",
    [
        {"subject": "Physique"},
        {"description": "Exercice 2"},
        {"due": date(2026, 11, 11)},
        {"child_id": "c2"},
    ],
)
def test_stable_fields_change_the_fingerprint(change: dict[str, Any]) -> None:
    work = Homework("c1", "Maths", "Exercice 1", date(2026, 11, 10))
    other = dataclasses.replace(work, **change)
    one = run(homework=[work], grades=[], signatures=[]).intents[0]["fingerprint"]
    two = run(homework=[other], grades=[], signatures=[]).intents[0]["fingerprint"]
    assert one != two


def test_the_fingerprint_never_holds_a_name() -> None:
    for intent in run().intents:
        assert "zo" not in intent["fingerprint"].casefold()
        assert "mathis" not in intent["fingerprint"].casefold()


def test_due_dates_are_in_the_instance_timezone() -> None:
    late_utc = datetime(2026, 11, 12, 23, 30, tzinfo=ZoneInfo("UTC"))  # 00:30 on the 13th, Paris
    work = Homework("c1", "Maths", "x", late_utc)
    out = run(homework=[work], grades=[], signatures=[])
    assert out.intents[0]["due"] == "2026-11-13"
    assert out.data["homework"][0]["due"] == "2026-11-13"
    utc = run(homework=[work], grades=[], signatures=[], tz=ZoneInfo("UTC"))
    assert utc.intents[0]["due"] == "2026-11-12"


def test_naive_datetimes_and_dates_are_taken_as_local_and_none_has_no_due() -> None:
    naive = Homework("c1", "A", "x", datetime(2026, 11, 12, 23, 59))
    plain = Homework("c1", "B", "x", date(2026, 11, 12))
    none = Homework("c1", "C", "x", None)
    out = run(homework=[naive, plain, none], grades=[], signatures=[])
    assert [i.get("due") for i in out.intents] == ["2026-11-12", "2026-11-12", None]
    assert out.data["homework"][2]["due"] is None


def test_the_timezone_is_read_from_tz_when_not_given() -> None:
    late = datetime(2026, 11, 12, 23, 30, tzinfo=ZoneInfo("UTC"))
    work = Homework("c1", "Maths", "x", late)
    out = normalize(
        ACCOUNT, CHILDREN, homework=[work], environ={"TZ": "Pacific/Auckland"}
    )  # +13: the 13th
    assert out.intents[0]["due"] == "2026-11-13"
    unset = normalize(ACCOUNT, CHILDREN, homework=[work], environ={})
    assert unset.intents[0]["due"] == "2026-11-12"  # unset: UTC


def test_titles_are_escaped_and_capped() -> None:
    nasty = Homework(
        "c1",
        "Maths #task 📅 2026-01-01",
        "[x](https://evil.example/) 🔁 every day ⏫ #urgent " + "très long " * 80,
        date(2026, 11, 10),
    )
    [intent] = run(homework=[nasty], grades=[], signatures=[]).intents
    title = intent["title"]
    assert len(title) <= MAX_TITLE
    assert title.endswith("…")
    assert not re.search(r"(?<!\\)#urgent|📅 2026|🔁|⏫", title)  # inert for Obsidian Tasks
    assert "evil.example" in plain_text(title) or "…" in title
    sign = Signature("c1", "Signer #urgent", date(2026, 11, 14), "x")
    [signed] = run(homework=[], grades=[], signatures=[sign]).intents
    assert "#urgent" not in signed["title"].replace("\\#", "")


def test_the_data_keeps_ids_not_names_per_item_and_caps_text() -> None:
    long = Homework("c1", "Maths", "mot " * 1000, date(2026, 11, 10))
    out = run(homework=[long], grades=[], signatures=[])
    assert len(out.data["homework"][0]["description"]) <= 1000
    for row in run().data["homework"]:
        assert row["child_id"] in {"c1", "c2"}
    assert out.data["account"] == ACCOUNT
    assert out.data["children"][0] == {"id": "c1", "first_name": "Zoé"}


def test_grade_values_pass_through() -> None:
    grades = run().data["grades"]
    assert (grades[0]["value"], grades[0]["out_of"], grades[0]["date"]) == (14.5, 20, "2026-11-05")
    assert grades[1]["value"] == "Abs"


@pytest.mark.parametrize(
    "bad",
    [
        {"account": "bad label"},
        {"account": "x" * 33},
        {"children": []},
        {"children": [Child("c1", "A")] * 2},
        {"children": [Child("has space", "Zoé")]},
        {"children": [Child("zoe", "Zoé")]},  # an id that is a name
        {"homework": [Homework("nobody", "A", "b", None)]},
        {"signatures": [Signature("nobody", "A", date(2026, 11, 1), "k")]},
        {"homework": [Homework("c1", "A", str(n), None) for n in range(MAX_ITEMS + 1)]},
    ],
)
def test_bad_input_is_refused(bad: dict[str, Any]) -> None:
    kwargs: dict[str, Any] = {"account": ACCOUNT, "children": CHILDREN, "tz": PARIS, **bad}
    account = kwargs.pop("account")
    children = kwargs.pop("children")
    with pytest.raises(NormalizeError):
        normalize(account, children, **kwargs)


def test_names_in_free_text_are_spelt_as_declared() -> None:
    data = run().data
    description = data["homework"][0]["description"]
    assert "Zoé la présentera" in description
    assert "ZOÉ" not in description
    assert "Lefèvre" in description  # a teacher's name is a declared name too
    assert data["signatures"][0]["title"] == "Sortie de Zoé au musée"


def test_scrubber() -> None:
    scrub = NameScrubber(["Zoé", "Anne-Marie", "Ly"])
    assert scrub.scrub("zoe, ZOE et Zoé.") == "Zoé, Zoé et Zoé."
    assert scrub.scrub("Zoélie et Zoeur") == "Zoélie et Zoeur"  # whole words only
    assert scrub.scrub("anne et MARIE") == "Anne et Marie"
    assert scrub.scrub("ly") == "ly"  # too short to be looked for
    assert scrub.scrub("Zo​e") == "Zoé"  # zero-width characters removed first
    assert bool(scrub)
    assert not NameScrubber([])
    assert NameScrubber([]).scrub("zoe") == "zoe"
    assert scrub.knows("ZOE")
    assert not scrub.knows("Zoelie")

# SPDX-License-Identifier: Apache-2.0
"""`ecoledirecte-setup`: the one place a security question is answered, by the user, once."""

import io
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from fake_ecoledirecte import CHOICES, QUESTION, RIGHT, Fake
from horae_school_fr import ecoledirecte
from horae_school_fr.__main__ import main
from horae_school_fr.credentials import ED_SECRET, DeviceStore

SECRET_ENV = {
    ED_SECRET: json.dumps({"famille": {"username": "user-fixture", "password": "pw-fixture"}})
}


def run(
    tmp_path: Path, fake: Fake, stdin: str = "", env: dict[str, str] | None = None, **kwargs: Any
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = ecoledirecte.setup(
        ["--account", "famille", "--store", str(tmp_path / "device.json")],
        stdin=io.StringIO(stdin),
        stdout=out,
        stderr=err,
        session=fake,  # type: ignore[arg-type]
        environ=SECRET_ENV if env is None else env,
        **kwargs,
    )
    return code, out.getvalue(), err.getvalue()


def test_the_question_and_the_choices_are_shown_and_the_device_is_stored(tmp_path: Path) -> None:
    fake = Fake()
    code, out, err = run(tmp_path, fake, f"{CHOICES.index(RIGHT) + 1}\n")
    assert code == 0, err
    assert QUESTION in out
    assert all(f"{i}. {c}" in out for i, c in enumerate(CHOICES, 1))
    store = tmp_path / "device.json"
    assert DeviceStore(str(store)).load("famille") is not None
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert not any(s in out + err for s in ("pw-fixture", "user-fixture", "cn-1", "cv-1", "token-"))
    logins = fake.logins()
    assert len(logins) == 2  # the first (the question), then the one with the new device
    assert logins[1].body is not None
    assert logins[1].body["fa"] == [{"cn": "cn-1", "cv": "cv-1"}]


def test_a_wrong_answer_is_one_attempt_and_stores_nothing(tmp_path: Path) -> None:
    fake = Fake()
    code, _, err = run(tmp_path, fake, f"{CHOICES.index('bleu') + 1}\n")
    assert code == 1
    assert err == "error: login_failed: the login did not work (quiz_failed)\n"
    assert len(fake.logins()) == 1
    assert [r.query["verbe"] for r in fake.quizzes()] == [
        "get",
        "post",
    ]  # asked once, answered once
    assert DeviceStore(str(tmp_path / "device.json")).load("famille") is None


@pytest.mark.parametrize("answer", ["", "x", "0", "4", "1 2"])
def test_an_answer_that_is_not_a_choice_is_refused_without_sending_it(
    tmp_path: Path, answer: str
) -> None:
    fake = Fake()
    code, _, err = run(tmp_path, fake, answer + "\n")
    assert code == 1
    assert "quiz_failed" in err
    assert [r.query["verbe"] for r in fake.quizzes()] == ["get"]


def test_a_wrong_password_is_one_login_and_a_fixed_message(tmp_path: Path) -> None:
    env = {ED_SECRET: json.dumps({"famille": {"username": "user-fixture", "password": "nope"}})}
    fake = Fake()
    code, out, err = run(tmp_path, fake, env=env)
    assert code == 1
    assert err == "error: login_failed: the login did not work (login_failed)\n"
    assert "nope" not in out + err
    assert len(fake.logins()) == 1
    assert fake.quizzes() == []


def test_a_network_failure_is_one_attempt(tmp_path: Path) -> None:
    fake = Fake()
    fake.fail = ConnectionError("https://api.ecoledirecte.com pw-fixture")
    code, _, err = run(tmp_path, fake)
    assert code == 1
    assert err == "error: login_failed: the login did not work (unavailable)\n"
    assert len(fake.requests) == 1


def test_an_account_that_is_not_asked_has_no_device_to_store(tmp_path: Path) -> None:
    fake = Fake()
    fake.quiz_needed = False
    code, _, err = run(tmp_path, fake)
    assert code == 0
    assert "no trusted device" in err
    assert DeviceStore(str(tmp_path / "device.json")).load("famille") is None
    assert fake.quizzes() == []


def test_without_the_secret_the_user_is_prompted(tmp_path: Path) -> None:
    fake = Fake()
    code, _, err = run(
        tmp_path,
        fake,
        "2\n",
        env={},
        ask_user=lambda: " user-fixture ",
        ask_password=lambda: "pw-fixture",
    )
    assert code == 0, err
    assert DeviceStore(str(tmp_path / "device.json")).load("famille") is not None


def test_an_empty_prompt_is_refused_before_any_request(tmp_path: Path) -> None:
    fake = Fake()
    code, _, err = run(tmp_path, fake, env={}, ask_user=lambda: "", ask_password=lambda: "")
    assert code == 1
    assert "credentials_invalid" in err
    assert fake.requests == []


def test_a_store_that_cannot_be_written_is_found_before_the_login(tmp_path: Path) -> None:
    fake = Fake()
    (tmp_path / "device.json").write_text("not json")
    code, _, err = run(tmp_path, fake, "2\n")
    assert code == 1
    assert "credentials_invalid" in err
    assert fake.requests == []


@pytest.mark.parametrize(
    "args",
    [[], ["--account", "famille"], ["--account", "bad label", "--store", "/x"], ["--nope"]],
)
def test_bad_arguments_are_refused_before_any_request(args: list[str]) -> None:
    err = io.StringIO()
    fake = Fake()
    code = ecoledirecte.setup(
        args,
        stdout=io.StringIO(),
        stderr=err,
        session=fake,  # type: ignore[arg-type]
        environ=SECRET_ENV,
    )
    assert code == 1
    assert err.getvalue()
    assert fake.requests == []


def test_the_module_entry_point_runs_the_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def setup(args: list[str]) -> int:
        seen.append(args)
        return 0

    monkeypatch.setattr(ecoledirecte, "setup", setup)
    with pytest.raises(SystemExit) as caught:
        main(["ecoledirecte-setup", "--account", "famille"])
    assert caught.value.code == 0
    assert seen == [["--account", "famille"]]

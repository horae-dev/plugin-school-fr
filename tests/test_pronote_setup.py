# SPDX-License-Identifier: Apache-2.0
"""`python -m horae_school_fr pronote-setup`: one QR login, no retry, no secret in a message."""

import io
import json
import stat
from pathlib import Path
from typing import Any

import pronotepy
import pytest

from fake_pronote import FIRST, URL, Fake
from horae_school_fr import pronote
from horae_school_fr.__main__ import main
from horae_school_fr.credentials import CredentialStore

QR = {
    "login": "0a1b2c3d",
    "jeton": "4e5f6a7b",
    "url": "https://0000000a.index-education.net/pronote/parent.html",
}


class Setup:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.calls: list[tuple[dict[str, str], str, str]] = []
        self.failure: BaseException | None = None
        self.out = io.StringIO()
        self.err = io.StringIO()

    def connect(self, qr: dict[str, str], pin: str, uuid: str) -> Any:
        self.calls.append((qr, pin, uuid))
        if self.failure is not None:
            raise self.failure
        return Fake().connect(FIRST)

    def run(
        self, *args: str, qr: object = QR, pin: str = "1234", environ: dict[str, str] | None = None
    ) -> int:
        text = qr if isinstance(qr, str) else json.dumps(qr)
        return pronote.setup(
            args,
            stdin=io.StringIO(text),
            stdout=self.out,
            stderr=self.err,
            ask_pin=lambda: pin,
            connect=self.connect,
            environ=environ or {},
        )


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path)


def test_it_prints_the_credentials_once(setup: Setup) -> None:
    assert setup.run("--account", "famille") == 0
    printed = json.loads(setup.out.getvalue())
    assert printed == {
        "famille": {
            "url": URL,
            "username": "user-fixture",
            "password": "token-2",
            "uuid": "uuid-fixture",
            "client_identifier": "client-fixture",
        }
    }
    assert len(setup.calls) == 1  # one login, no retry
    qr, pin, uuid = setup.calls[0]
    assert (qr, pin) == (QR, "1234")
    assert len(uuid) == 36
    assert "PRONOTE_CREDENTIALS" in setup.err.getvalue()
    assert "token-2" not in setup.err.getvalue()


def test_with_store_it_writes_a_private_file_and_prints_no_secret(setup: Setup) -> None:
    path = setup.tmp / "creds.json"
    assert setup.run("--account", "famille", "--store", str(path)) == 0
    assert setup.out.getvalue() == ""
    assert "token-2" not in setup.err.getvalue()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    stored = CredentialStore(str(path)).load("famille")
    assert stored is not None
    assert stored.password == "token-2"


def test_the_qr_code_can_come_from_a_file(setup: Setup) -> None:
    path = setup.tmp / "qr.json"
    path.write_text(json.dumps(QR), "utf-8")
    assert setup.run("--account", "famille", "--qr", str(path), qr="ignored") == 0
    assert setup.calls[0][0] == QR


def test_the_pin_can_come_from_the_environment(setup: Setup) -> None:
    code = pronote.setup(
        ["--account", "famille"],
        stdin=io.StringIO(json.dumps(QR)),
        stdout=setup.out,
        stderr=setup.err,
        connect=setup.connect,
        environ={"PRONOTE_PIN": "4321"},
    )
    assert code == 0
    assert setup.calls[0][1] == "4321"


@pytest.mark.parametrize(
    ("args", "kwargs", "message"),
    [
        (("--account", "bad label"), {}, "--account is a label"),
        (("--account", "a"), {"qr": "{nope"}, "qr_invalid"),
        (
            ("--account", "a"),
            {"qr": {"login": "zz", "jeton": "00", "url": QR["url"]}},
            "qr_invalid",
        ),
        (
            ("--account", "a"),
            {"qr": {**QR, "url": "https://evil.example.org/p"}},
            "network_not_approved",
        ),
        (
            ("--account", "a"),
            {"qr": {**QR, "url": "http://0000000a.index-education.net/p"}},
            "network_not_approved",
        ),
        (("--account", "a"), {"pin": "12"}, "the PIN is the 4 digits"),
        (("--account", "a", "--qr", "/nonexistent/qr.json"), {}, "cannot read a file"),
    ],
)
def test_bad_input_fails_before_any_login(
    setup: Setup, args: tuple[str, ...], kwargs: dict[str, Any], message: str
) -> None:
    assert setup.run(*args, **kwargs) == 1
    assert message in setup.err.getvalue()
    assert setup.calls == []
    assert setup.out.getvalue() == ""


def test_a_custom_domain_needs_to_be_named(setup: Setup) -> None:
    qr = {**QR, "url": "https://pronote.college-exemple.fr/pronote/parent.html"}
    assert setup.run("--account", "a", qr=qr) == 1
    assert setup.run("--account", "a", "--host", "pronote.college-exemple.fr", qr=qr) == 0


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (pronotepy.QRCodeDecryptError("invalid confirmation code"), "wrong PIN, or the QR code"),
        (pronotepy.PronoteAPIError("body", pronote_error_code=25), "(locked_out)"),
        (pronote._LoginRejected(), "(login_failed)"),
        (ConnectionError("https://secret.example/token-1"), "(unavailable)"),
    ],
)
def test_a_failed_login_is_one_attempt_and_a_fixed_message(
    setup: Setup, failure: BaseException, message: str
) -> None:
    setup.failure = failure
    assert setup.run("--account", "a") == 1
    assert message in setup.err.getvalue()
    assert "secret.example" not in setup.err.getvalue()
    assert setup.out.getvalue() == ""
    assert len(setup.calls) == 1


def test_bad_arguments_print_the_usage(setup: Setup) -> None:
    assert setup.run("--nope") == 1
    assert "usage:" in setup.err.getvalue()


def test_main_dispatches_to_the_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_setup(args: Any) -> int:
        seen.append(list(args))
        return 0

    monkeypatch.setattr(pronote, "setup", fake_setup)
    with pytest.raises(SystemExit) as stop:
        main(["pronote-setup", "--account", "a"])
    assert stop.value.code == 0
    assert seen == [["--account", "a"]]

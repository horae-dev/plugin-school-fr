# SPDX-License-Identifier: Apache-2.0
"""The credential store: atomic, mode 0600, fail closed, no secret in a message or a repr."""

import json
import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
from horae_sdk import PluginError

from fake_pronote import FIRST
from horae_school_fr.credentials import (
    SECRET,
    Credentials,
    CredentialStore,
    from_secret,
    parse,
)

ENTRY = FIRST.to_dict()


@pytest.fixture
def store(tmp_path: Path) -> CredentialStore:
    return CredentialStore(str(tmp_path / "creds.json"), lock_timeout=0.2)


def test_save_then_load_round_trips_and_is_private(store: CredentialStore) -> None:
    assert store.load("anna") is None  # no file yet
    store.save("anna", FIRST)
    assert store.load("anna") == FIRST
    assert store.load("ben") is None
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.path + ".lock").st_mode) == 0o600
    document = json.loads(Path(store.path).read_text("utf-8"))
    assert document == {"version": 1, "accounts": {"anna": ENTRY}}


def test_a_rewrite_keeps_the_other_accounts_and_leaves_no_temporary_file(
    store: CredentialStore, tmp_path: Path
) -> None:
    store.save("anna", FIRST)
    store.save("ben", Credentials(*[f"{v}-2" for v in ENTRY.values()]))
    store.save("anna", Credentials(FIRST.url, FIRST.username, "token-9", FIRST.uuid, "client-x"))
    anna = store.load("anna")
    assert anna is not None
    assert (anna.password, anna.client_identifier) == ("token-9", "client-x")
    assert store.load("ben") is not None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["creds.json", "creds.json.lock"]


def test_check_writable_rewrites_the_file_unchanged(store: CredentialStore) -> None:
    store.save("anna", FIRST)
    before = Path(store.path).read_bytes()
    store.check_writable()
    assert Path(store.path).read_bytes() == before


@pytest.mark.parametrize(
    "content",
    [
        "{nope",
        "[]",
        '{"version": 2, "accounts": {}}',
        '{"version": 1, "accounts": {"bad label": {}}}',
        '{"version": 1, "accounts": {"anna": {"url": "x"}}}',
        '{"version": 1, "accounts": [], "extra": 1}',
        "x" * (256 * 1024 + 1),
    ],
)
def test_a_file_that_is_not_a_store_is_refused_and_left_alone(
    store: CredentialStore, content: str
) -> None:
    Path(store.path).write_text(content, "utf-8")
    actions: list[Callable[[], object]] = [
        lambda: store.load("anna"),
        lambda: store.save("anna", FIRST),
        store.check_writable,
    ]
    for action in actions:
        with pytest.raises(PluginError, match="credentials_invalid"):
            action()
    assert Path(store.path).read_text("utf-8") == content


def test_an_unwritable_directory_is_reported_without_a_path_or_a_value(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    store = CredentialStore(str(locked / "creds.json"))
    store.save("anna", FIRST)
    locked.chmod(0o500)
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("directory permissions are not enforced (root)")
        with pytest.raises(PluginError, match="credentials_unwritable") as error:
            store.save("anna", Credentials(FIRST.url, FIRST.username, "token-9", FIRST.uuid))
        assert "token-9" not in str(error.value)
        assert str(locked) not in str(error.value)
    finally:
        locked.chmod(0o700)
    loaded = store.load("anna")
    assert loaded is not None
    assert loaded.password == FIRST.password  # the old file is intact


def test_a_busy_store_gives_up_instead_of_waiting_forever(store: CredentialStore) -> None:
    with store._locked(), pytest.raises(PluginError, match="busy"):
        CredentialStore(store.path, lock_timeout=0.05).save("anna", FIRST)


def test_the_store_lock_cannot_be_a_symlink(store: CredentialStore, tmp_path: Path) -> None:
    (tmp_path / "elsewhere").write_text("", "utf-8")
    os.symlink(tmp_path / "elsewhere", store.path + ".lock")
    with pytest.raises(PluginError, match="credentials_unwritable"):
        store.save("anna", FIRST)


def test_repr_shows_no_value() -> None:
    text = repr(FIRST)
    assert text == "Credentials()"


@pytest.mark.parametrize(
    "entry",
    [
        None,
        "text",
        {},
        {**ENTRY, "extra": "x"},
        {**ENTRY, "password": ""},
        {**ENTRY, "password": 5},
        {**ENTRY, "uuid": "line\nbreak"},
        {**ENTRY, "url": "x" * 3000},
        {**ENTRY, "client_identifier": 1},
    ],
)
def test_parse_refuses_a_bad_entry_without_echoing_it(entry: object) -> None:
    with pytest.raises(PluginError, match="credentials_invalid") as error:
        parse(entry)
    assert "token-1" not in str(error.value)


def test_the_secret_is_an_object_keyed_by_account() -> None:
    raw = json.dumps({"anna": ENTRY})
    assert from_secret(raw, "anna") == FIRST
    assert from_secret(raw, "ben") is None
    assert from_secret(None, "anna") is None
    assert from_secret("", "anna") is None
    for bad in ("{nope", "[]", json.dumps({str(i): ENTRY for i in range(40)})):
        with pytest.raises(PluginError, match=SECRET):
            from_secret(bad, "anna")


# --- the ÉcoleDirecte login and trusted device -------------------------------------------------


def test_the_device_store_round_trips_privately(tmp_path: Path) -> None:
    from horae_school_fr.credentials import Device, DeviceStore

    store = DeviceStore(str(tmp_path / "d.json"))
    assert store.load("famille") is None
    store.save("famille", Device("cn-x", "cv-x"))
    store.save("autre", Device("cn-y", "cv-y"))
    assert store.load("famille") == Device("cn-x", "cv-x")
    assert stat.S_IMODE((tmp_path / "d.json").stat().st_mode) == 0o600
    assert "cn-x" not in repr(Device("cn-x", "cv-x"))
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


@pytest.mark.parametrize(
    "entry",
    [
        None,
        {},
        {"cn": "a"},
        {"cn": "a", "cv": ""},
        {"cn": "a", "cv": 1},
        {"cn": "a", "cv": "b", "x": "c"},
    ],
)
def test_a_bad_device_is_refused_without_echoing_it(entry: object) -> None:
    from horae_school_fr.credentials import parse_device

    with pytest.raises(PluginError, match="credentials_invalid") as caught:
        parse_device(entry)
    assert "cn" not in str(caught.value).replace("cn and cv", "")


def test_the_login_secret_is_an_object_keyed_by_account() -> None:
    from horae_school_fr.credentials import ED_SECRET, login_from_secret, parse_login

    raw = json.dumps({"famille": {"username": "u", "password": "p"}})
    assert login_from_secret(raw, "famille") == parse_login({"username": "u", "password": "p"})
    assert login_from_secret(raw, "autre") is None
    assert login_from_secret(None, "famille") is None
    for bad in ("nope", "[]", json.dumps({"famille": {"username": "u"}})):
        with pytest.raises(PluginError, match="credentials_invalid") as caught:
            login_from_secret(bad, "famille")
        assert ED_SECRET in str(caught.value) or "needs" in str(caught.value)

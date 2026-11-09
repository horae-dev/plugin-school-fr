# SPDX-License-Identifier: Apache-2.0
"""`school-fr.pronote` (story #2): the guard, the rotation, the failures, the read-only rule.

No network: pronotepy objects are replaced by `fake_pronote.Fake`. Names are synthetic."""

import json
import re
import socket
import stat
from datetime import date
from pathlib import Path
from typing import Any

import anyio
import pronotepy
import pytest
from horae.llm.deid import pseudonymize
from horae_sdk import Call, PluginError
from horae_sdk.testing import Replayed
from horae_sdk.testing import call as call_tool

from conftest import ManualClock, paris
from fake_pronote import FIRST, URL, Fake
from horae_school_fr import DEIDENTIFIED_FIELDS, AuthFailure, build, input_view, pronote
from horae_school_fr.credentials import SECRET, CredentialStore

ENV_TZ = {"TZ": "Europe/Paris"}
HOST = "0000000a.index-education.net"
TOKEN = FIRST.password


def secret_env(token: str = TOKEN) -> dict[str, str]:
    entry = {**FIRST.to_dict(), "password": token}
    return {**ENV_TZ, SECRET: json.dumps({"famille": entry})}


class Harness:
    def __init__(self, tmp_path: Path, clock: ManualClock) -> None:
        self.tmp = tmp_path
        self.clock = clock
        self.fake = Fake()
        self.state = tmp_path / "state.json"
        self.store = tmp_path / "pronote-credentials.json"
        self.env = secret_env()

    def run(
        self,
        network: list[str] | None = None,
        paths: list[str] | None = None,
        **params: Any,
    ) -> Replayed:
        plugin = build(clock=self.clock, environ=self.env, connect=self.fake.connect)
        arguments = {"account": "famille", "state_path": str(self.state), **params}

        async def go() -> Replayed:
            return await call_tool(
                plugin,
                "school-fr.pronote",
                arguments,
                paths={"write": paths if paths is not None else [str(self.tmp)]},
                network=[HOST] if network is None else network,
            )

        return anyio.run(go)

    def poll(self, **params: Any) -> Replayed:
        """A poll two hours after the previous one would be allowed."""
        result = self.run(**params)
        self.clock.advance(hours=2, minutes=1)
        return result

    def stored(self) -> dict[str, Any]:
        document = json.loads(self.store.read_text("utf-8"))
        accounts: dict[str, Any] = document["accounts"]
        return accounts

    def guard(self) -> dict[str, Any]:
        document = json.loads(self.state.read_text("utf-8"))
        accounts: dict[str, Any] = document["accounts"]
        entry: dict[str, Any] = accounts["famille"]
        return entry


@pytest.fixture
def h(tmp_path: Path, clock: ManualClock) -> Harness:
    return Harness(tmp_path, clock)


# --- the happy path ----------------------------------------------------------------------------


def test_a_poll_reads_and_normalizes(h: Harness) -> None:
    result = h.run()
    assert result.error is None
    data = result.data
    assert data["account"] == "famille"
    assert [c["first_name"] for c in data["children"]] == ["LEROY Zoé", "LEROY Mathis"]
    assert all(re.fullmatch(r"c[0-9a-f]{12}", c["id"]) for c in data["children"])
    assert "raw-id-1" not in json.dumps(data)  # the platform's own id is never published
    assert len(data["homework"]) == 4  # 2 per child
    assert {w["subject"] for w in data["homework"]} == {"Français", "Maths"}
    grades = [g for g in data["grades"] if g["child_id"] == data["children"][0]["id"]]
    assert [(g["subject"], g["value"], g["out_of"]) for g in grades] == [
        ("Maths", 14.5, 20.0),
        ("Anglais", "Abs", 10.0),
    ]
    assert [s["kind"] for s in data["signatures"]] == ["information"]  # once, not per child
    # undone homework (2) and the signature (1) are tasks; done homework and grades are not
    assert len(result.intents) == 3
    assert {i["type"] for i in result.intents} == {"task.create"}
    assert h.fake.writes == []
    assert h.fake.selected == ["raw-id-1", "raw-id-2"]


def test_the_children_and_the_period_can_be_chosen(h: Harness) -> None:
    result = h.run(children=["mathis"], periods=["trimestre 2"], days_ahead=3)
    assert result.error is None
    assert [c["first_name"] for c in result.data["children"]] == ["LEROY Mathis"]
    assert h.fake.selected == ["raw-id-2"]
    assert len(result.data["grades"]) == 2


def test_an_unknown_child_or_period_is_a_config_error_after_a_good_login(h: Harness) -> None:
    for params in ({"children": ["Nobody"]}, {"periods": ["Trimestre 9"]}):
        h.fake.connects.clear()
        result = h.run(**params)
        assert result.error is not None
        assert "config_invalid" in result.error
        h.clock.advance(hours=2, minutes=1)
    guard = h.guard()
    assert guard["failures"] == 0  # the account is fine: no backoff, no stop
    assert guard["backoff_until"] is None
    assert guard["auth_stopped"] is None
    assert len(guard["polls"]) == 2


# --- acceptance: never more than the configured polls per day -----------------------------------


def test_never_more_than_the_configured_polls_per_day(h: Harness) -> None:
    for _ in range(3):
        assert h.poll(max_polls_per_day=3, min_interval="1m").error is None
        h.clock.advance(minutes=-120)  # ... one minute apart is enough here
    h.clock.advance(minutes=60 * 2)
    result = h.run(max_polls_per_day=3, min_interval="1m")
    assert result.error is not None
    assert result.error.startswith("degraded: budget_exhausted")
    assert len(h.fake.connects) == 3  # the refused call made no login
    # the next local day starts again
    h.clock.set(paris(2026, 11, 10, 7, 0))
    assert h.run(max_polls_per_day=3, min_interval="1m").error is None
    assert len(h.fake.connects) == 4


def test_the_default_is_four_polls_and_a_login_each(h: Harness) -> None:
    h.clock.set(paris(2026, 11, 9, 1, 0))
    outcomes = [h.poll().error for _ in range(5)]
    assert outcomes[:4] == [None] * 4
    assert outcomes[4] is not None
    assert outcomes[4].startswith("degraded: budget_exhausted")
    assert len(h.fake.connects) == 4


def test_min_interval_and_windows_are_honoured_without_a_login(h: Harness) -> None:
    assert h.run().error is None
    again = h.run()
    assert again.error is not None
    assert "budget_exhausted" in again.error
    h.clock.set(paris(2026, 11, 9, 12, 0))
    outside = h.run(windows=["07:00-08:00"])
    assert outside.error is not None
    assert outside.error.startswith("degraded: outside_window")
    assert len(h.fake.connects) == 1


def test_the_login_budget_caps_the_polls_too(h: Harness) -> None:
    for _ in range(2):
        assert h.poll(max_polls_per_day=4, max_logins_per_day=2).error is None
    result = h.run(max_polls_per_day=4, max_logins_per_day=2)
    assert result.error is not None
    assert "logins_per_day" in result.error
    assert len(h.fake.connects) == 2


# --- acceptance: credential rotation persisted --------------------------------------------------


def test_the_rotated_credentials_are_persisted_after_every_login(h: Harness) -> None:
    assert h.run().error is None
    assert h.fake.connects == ["token-1"]  # the first one, from the secret
    entry = h.stored()["famille"]
    assert entry["password"] == "token-2"
    assert (entry["url"], entry["username"], entry["uuid"]) == (URL, "user-fixture", "uuid-fixture")
    assert stat.S_IMODE(h.store.stat().st_mode) == 0o600
    h.clock.advance(hours=3)
    assert h.run().error is None
    assert h.fake.connects == ["token-1", "token-2"]  # the store wins over the stale secret
    assert h.stored()["famille"]["password"] == "token-3"
    assert [p.name for p in h.tmp.iterdir() if p.name.startswith(".school-fr")] == []


def test_the_rotation_is_saved_before_anything_is_read(h: Harness) -> None:
    h.fake.fail_read = OSError("connection reset")
    result = h.run()
    assert result.error == "degraded: source_unavailable (network)"
    assert h.stored()["famille"]["password"] == "token-2"  # not lost with the failed read


def test_a_rotation_during_the_reads_is_persisted_too(h: Harness) -> None:
    h.fake.rotate_on_read = True
    assert h.run().error is None
    assert h.stored()["famille"]["password"] == "token-rotated-during-read"


def test_other_accounts_in_the_store_are_kept(h: Harness) -> None:
    CredentialStore(str(h.store)).save("autre", FIRST)
    assert h.run().error is None
    assert set(h.stored()) == {"autre", "famille"}


def test_a_store_that_cannot_be_written_stops_the_poll_before_the_login(h: Harness) -> None:
    h.store.write_text("{broken", "utf-8")
    result = h.run()
    assert result.error is not None
    assert "credentials_invalid" in result.error
    assert h.fake.connects == []
    assert not h.state.exists()  # no budget spent
    assert h.store.read_text("utf-8") == "{broken"  # left for a human


def test_a_failed_save_after_the_login_is_a_transient_failure(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: CredentialStore, account: str, credentials: Any) -> None:
        raise PluginError("credentials_unwritable: x")

    monkeypatch.setattr(CredentialStore, "save", refuse)
    result = h.run()
    assert result.error == "degraded: source_unavailable (credentials_unwritable)"
    assert h.guard()["failures"] == 1


def test_without_credentials_nothing_is_spent(h: Harness) -> None:
    h.env = dict(ENV_TZ)
    result = h.run()
    assert result.error is not None
    assert "credentials_missing" in result.error
    assert SECRET in result.error
    assert h.fake.connects == []
    assert not h.state.exists()


def test_the_store_path_must_be_approved(h: Harness, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = h.run(credentials_path=str(elsewhere / "c.json"), paths=[str(h.tmp / "sub")])
    assert result.error is not None
    assert "path_not_approved" in result.error
    assert h.fake.connects == []


# --- acceptance: degraded on any login failure, no retry loop ----------------------------------


def lockout() -> BaseException:
    return pronotepy.PronoteAPIError("secret body", pronote_error_code=25, pronote_error_msg="x")


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (pronote._LoginRejected(), "login_failed"),
        (pronotepy.CryptoError("probably bad credentials"), "login_failed"),
        (lockout(), "locked_out"),
        (pronotepy.MFAError("Invalid PIN"), "mfa_failed"),
        (
            pronotepy.PronoteAPIError("x", pronote_error_code=3, pronote_error_msg="y"),
            "login_failed",
        ),
        (pronote._SessionLost(), None),
    ],
)
def test_an_auth_failure_stops_the_account(
    h: Harness, failure: BaseException, code: str | None
) -> None:
    if code is None:  # a lost session with no coded cause is not an auth failure
        h.fake.fail = failure
        assert h.run().error == "degraded: source_unavailable (api_changed)"
        return
    h.fake.fail = failure
    result = h.run()
    assert result.error == f"degraded: auth_stopped ({code})"
    assert h.guard()["auth_stopped"]["code"] == code
    # however much time passes, no attempt: no login, no retry
    for days in (1, 5):
        h.clock.advance(hours=24 * days)
        again = h.run()
        assert again.error is not None
        assert again.error.startswith("degraded: auth_stopped")
    assert len(h.fake.connects) == 1


def test_the_lockout_found_behind_pronotepys_refresh_is_an_auth_failure(h: Harness) -> None:
    def refresh() -> None:
        try:
            raise lockout()
        except pronotepy.PronoteAPIError:
            raise pronote._SessionLost from None

    with pytest.raises(pronote._SessionLost) as caught:
        refresh()
    # `from None` hides the cause from tracebacks, but not from `__context__`
    assert isinstance(pronote.classify(caught.value, logging_in=True), AuthFailure)


def test_a_network_failure_is_transient_and_backs_off(h: Harness) -> None:
    for failure in (ConnectionError("dns"), TimeoutError("slow"), OSError("reset")):
        h.fake.connects.clear()
        h.fake.fail = failure
        h.state.unlink(missing_ok=True)
        result = h.run()
        assert result.error == "degraded: source_unavailable (network)"
        assert h.guard()["auth_stopped"] is None
        assert h.guard()["failures"] == 1
        assert h.fake.connects == [TOKEN]  # one attempt, no retry
    # still within the hour: refused without a login
    h.fake.connects.clear()
    h.clock.advance(minutes=30)
    refused = h.run()
    assert refused.error is not None
    assert refused.error.startswith("degraded: backoff_until")
    assert h.fake.connects == []
    # after the backoff the account is tried again, and one success resets the streak
    h.fake.fail = None
    h.clock.advance(hours=2)
    assert h.run().error is None
    assert h.guard()["failures"] == 0


def test_an_api_change_is_transient_not_an_auth_failure(h: Harness) -> None:
    h.fake.fail = KeyError("dataSec")
    assert h.run().error == "degraded: source_unavailable (api_changed)"
    h.state.unlink()
    h.fake.fail = pronotepy.PronoteAPIError("Bad request (http status: 503)")
    assert h.run().error == "degraded: source_unavailable (service)"
    assert h.guard()["auth_stopped"] is None


def test_an_error_while_reading_is_transient_even_with_a_pronote_code(h: Harness) -> None:
    h.fake.fail_read = pronotepy.ExpiredObject("session")
    result = h.run()
    assert result.error == "degraded: source_unavailable (service)"
    assert h.guard()["auth_stopped"] is None


def test_failures_never_carry_a_credential_or_a_body(h: Harness) -> None:
    secrets = [TOKEN, "user-fixture", "uuid-fixture", "secret body", HOST]
    for failure in (lockout(), OSError(f"{URL} {TOKEN}"), KeyError(TOKEN)):
        h.state.unlink(missing_ok=True)
        h.fake.fail = failure
        error = h.run().error
        assert error is not None
        assert not any(s in error for s in secrets)
        assert error.startswith("degraded: ")


# --- the network -------------------------------------------------------------------------------


def test_a_host_the_lock_does_not_approve_is_refused_before_any_login(h: Harness) -> None:
    result = h.run(network=["other.example.org"])
    assert result.error is not None
    assert "network_not_approved" in result.error
    assert h.fake.connects == []
    assert not h.state.exists()


def test_without_a_list_the_source_falls_back_to_index_education(h: Harness) -> None:
    plugin = build(clock=h.clock, environ=h.env, connect=h.fake.connect)

    async def go(**params: Any) -> Replayed:
        return await call_tool(
            plugin,
            "school-fr.pronote",
            {"account": "famille", "state_path": str(h.state), **params},
            paths={"write": [str(h.tmp)]},
            network=None,  # an older core: unknown, not empty
        )

    assert anyio.run(go).error is None
    custom = {
        **FIRST.to_dict(),
        "url": "https://pronote.college-exemple.fr/pronote/mobile.parent.html",
    }
    h.store.unlink()
    h.env = {**h.env, SECRET: json.dumps({"famille": custom})}
    plugin = build(clock=h.clock, environ=h.env, connect=h.fake.connect)
    h.clock.advance(hours=3)
    refused = anyio.run(go)
    assert refused.error is not None
    assert "network_not_approved" in refused.error
    allowed = anyio.run(lambda: go(hosts=["pronote.college-exemple.fr"]))
    assert allowed.error is None


@pytest.mark.parametrize(
    "url",
    [
        "http://0000000a.index-education.net/pronote/",
        "https://user@0000000a.index-education.net/pronote/",
        "https://127.0.0.1/pronote/",
    ],
)
def test_check_host_refuses_what_is_not_a_plain_https_host(url: str) -> None:
    with pytest.raises(PluginError, match=r"config_invalid|network_not_approved"):
        pronote.check_host(Call("school-fr.pronote"), url)


# --- read-only ---------------------------------------------------------------------------------


def test_the_allow_list_has_no_write_method() -> None:
    writers = re.compile(r"^(set_(?!child)|mark|post|new_|delete|save|send|update|create|answer)")
    assert not [n for n in pronote.ALLOWED if writers.match(n)]
    assert pronote.READ_METHODS <= pronote.ALLOWED


def test_the_proxy_refuses_every_method_not_on_the_allow_list() -> None:
    fake = Fake()
    proxy = pronote.ReadOnly(fake.connect(FIRST))
    for name in ("post", "new_discussion", "refresh", "password", "fake", "__class__x", "_target"):
        with pytest.raises(pronote.ReadOnlyViolation):
            getattr(proxy, name)
    with pytest.raises(pronote.ReadOnlyViolation):
        proxy.post("PageCahierDeTexte", 88, {})
    with pytest.raises(pronote.ReadOnlyViolation):
        proxy.password = "x"
    with pytest.raises(pronote.ReadOnlyViolation):
        del proxy.children
    assert fake.writes == []


def test_objects_the_proxy_returns_are_read_only_too() -> None:
    fake = Fake()
    proxy = pronote.ReadOnly(fake.connect(FIRST))
    proxy.set_child(proxy.children[0])
    (work, _) = proxy.homework(date(2026, 11, 9), date(2026, 11, 20))
    assert work.done is False
    for name in ("set_done", "mark_as_read"):
        with pytest.raises(pronote.ReadOnlyViolation):
            getattr(work, name)
    child = proxy.children[0]
    assert (child.name, repr(child)) == ("LEROY Zoé", "<ReadOnly>")
    proxy.set_child(child)  # the proxy is unwrapped for the call (the fake asserts it)
    (info,) = proxy.information_and_surveys(only_unread=True, date_from=date(2026, 1, 1))
    with pytest.raises(pronote.ReadOnlyViolation):
        info.mark_as_read(True)
    assert fake.writes == []


def test_a_whole_poll_never_calls_a_write_method(h: Harness) -> None:
    assert h.run().error is None
    assert h.fake.writes == []


def test_every_public_name_of_pronotepy_outside_the_allow_list_is_refused() -> None:
    """If a pronotepy update adds a method, it is refused until someone adds it on purpose."""
    classes = (
        pronotepy.ParentClient,
        pronotepy.Homework,
        pronotepy.Information,
        pronotepy.Period,
        pronotepy.Grade,
        pronotepy.ClientInfo,
        pronotepy.Subject,
    )
    for cls in classes:
        for name in dir(cls):
            if name.startswith("_") or name in pronote.ALLOWED:
                continue
            with pytest.raises(pronote.ReadOnlyViolation):
                getattr(pronote.ReadOnly(object.__new__(cls)), name)
    for name in pronote.READ_METHODS - {"set_child"}:
        assert callable(getattr(pronotepy.ParentClient, name))


def test_the_real_client_never_retries_on_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    client = object.__new__(pronote._Client)
    with pytest.raises(pronote._SessionLost):
        client.refresh()
    monkeypatch.setattr(pronotepy.clients.ClientBase, "_login", lambda self: False)
    with pytest.raises(pronote._LoginRejected):
        client._login()
    monkeypatch.setattr(pronotepy.clients.ClientBase, "_login", lambda self: True)
    assert client._login() is True


def test_login_bounds_every_request_and_restores_the_socket_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float | None] = []

    def token_login(*args: Any, **kwargs: Any) -> str:
        seen.append(socket.getdefaulttimeout())
        return "client"

    monkeypatch.setattr(pronote._Client, "token_login", token_login)
    before = socket.getdefaulttimeout()
    assert pronote.login(FIRST) == "client"
    assert seen == [pronote.HTTP_TIMEOUT_S]
    assert socket.getdefaulttimeout() == before


def test_pronotepy_is_imported_by_one_module_only() -> None:
    source = Path(pronote.__file__).parent
    importers = {
        p.name
        for p in source.glob("*.py")
        if re.search(r"^\s*(import|from)\s+pronotepy\b", p.read_text("utf-8"), re.MULTILINE)
    }
    assert importers == {"pronote.py"}


def test_pronotepys_logger_is_silenced() -> None:
    import logging

    logger = logging.getLogger("pronotepy")
    assert not logger.isEnabledFor(logging.ERROR)
    assert logger.propagate is False


# --- de-identification -------------------------------------------------------------------------


def test_the_names_the_source_outputs_are_declared_for_de_identification(h: Harness) -> None:
    data = h.run().data
    declared = {
        path: options
        for path, options in input_view().items()
        if options is not None and options.get("class") == "person"
    }
    assert set(declared) == {"[].data." + p for p in DEIDENTIFIED_FIELDS}
    assert "children[].first_name" in DEIDENTIFIED_FIELDS
    sent = pseudonymize(
        [{"source": "school-fr.pronote", "data": data}],
        {path: options for path, options in input_view().items() if options is not None},
    ).data
    text = json.dumps(sent, ensure_ascii=False)
    for name in ("Zoé", "Mathis", "LEROY"):
        assert name.casefold() not in text.casefold(), name
    # the accent-less spelling in a homework description (the fake writes "Zoé"/"Mathis" there)
    assert "[PERSON_" in text


def test_a_pronote_error_during_a_read_does_not_make_pronotepy_log_in_again() -> None:
    """`ParentClient.post` calls `refresh()` (a second login) after an API error."""

    class Communication:
        def post(self, name: str, data: dict[str, Any]) -> None:
            raise pronotepy.PronoteAPIError("x", pronote_error_code=10)

    class Selected:
        id = "raw-id-1"

    client = object.__new__(pronote._Client)
    client.communication = Communication()  # type: ignore[assignment]
    client._selected_child = Selected()  # type: ignore[assignment]
    with pytest.raises(pronote._SessionLost) as caught:
        client.post("PageCahierDeTexte", 88, {})
    assert isinstance(pronote.classify(caught.value, logging_in=False), pronote.SourceUnavailable)

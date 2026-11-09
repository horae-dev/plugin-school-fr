# SPDX-License-Identifier: Apache-2.0
"""`school-fr.ecoledirecte` (story #3): the guard, the trusted device, the quiz, the failures and
the read-only rule. No network: the HTTP session is `fake_ecoledirecte.Fake`. Names are
synthetic."""

import io
import json
import re
import stat
import time
from pathlib import Path
from typing import Any

import anyio
import pytest
import requests
from horae.llm.deid import pseudonymize
from horae_sdk import Call, PluginError
from horae_sdk.protocol import Network
from horae_sdk.testing import Replayed
from horae_sdk.testing import call as call_tool

from conftest import ManualClock, paris
from fake_ecoledirecte import LOGIN, RIGHT, Fake, Request
from hanging_server import hanging_server
from horae_school_fr import DEIDENTIFIED_FIELDS, AuthFailure, build, ecoledirecte, input_view
from horae_school_fr.credentials import ED_SECRET, Device, DeviceStore

ENV_TZ = {"TZ": "Europe/Paris"}
HOST = "api.ecoledirecte.com"
ENV = {
    **ENV_TZ,
    ED_SECRET: json.dumps({"famille": {"username": LOGIN.username, "password": LOGIN.password}}),
}
SECRETS = [LOGIN.username, LOGIN.password, "cn-1", "cv-1", "gtk-fixture", "token-", HOST]


class Harness:
    def __init__(self, tmp_path: Path, clock: ManualClock) -> None:
        self.tmp = tmp_path
        self.clock = clock
        self.fake = Fake()
        self.state = tmp_path / "state.json"
        self.store = tmp_path / "ecoledirecte-device.json"
        self.env = dict(ENV)

    def run(
        self,
        network: list[str] | None = None,
        paths: list[str] | None = None,
        **params: Any,
    ) -> Replayed:
        plugin = build(clock=self.clock, environ=self.env, ed_connect=self.fake.connect)
        arguments = {"account": "famille", "state_path": str(self.state), **params}

        async def go() -> Replayed:
            return await call_tool(
                plugin,
                "school-fr.ecoledirecte",
                arguments,
                paths={"write": paths if paths is not None else [str(self.tmp)]},
                network=[HOST] if network is None else network,
            )

        return anyio.run(go)

    def poll(self, **params: Any) -> Replayed:
        """A poll; the next one is two hours later, which the default interval allows."""
        result = self.run(**params)
        self.clock.advance(hours=2, minutes=1)
        return result

    def trust(self) -> None:
        """What `ecoledirecte-setup` leaves behind."""
        device = self.fake.issue()
        DeviceStore(str(self.store)).save("famille", Device(device["cn"], device["cv"]))

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


@pytest.fixture
def trusted(h: Harness) -> Harness:
    h.trust()
    return h


# --- the happy path ----------------------------------------------------------------------------


def test_a_poll_reads_and_normalizes(trusted: Harness) -> None:
    h = trusted
    result = h.run()
    assert result.error is None
    data = result.data
    assert data["account"] == "famille"
    assert [c["first_name"] for c in data["children"]] == ["LEROY Zoé", "LEROY Mathis"]
    assert all(re.fullmatch(r"c[0-9a-f]{12}", c["id"]) for c in data["children"])
    assert "7001" not in json.dumps(data)  # the platform's own id is never published
    assert len(data["homework"]) == 4  # 2 per child; the one beyond days_ahead is not read
    descriptions = {w["description"] for w in data["homework"]}
    assert "Exercices 4 et 5" in descriptions  # the HTML is text
    assert {w["teacher"] for w in data["homework"]} == {"M. DUPONT", "Mme LEFEVRE"}
    first = data["children"][0]["id"]
    grades = [g for g in data["grades"] if g["child_id"] == first]
    assert [(g["subject"], g["value"], g["out_of"]) for g in grades] == [
        ("Maths", 14.5, 20.0),
        ("Anglais", "Abs", 10.0),
    ]  # the current period only, newest first
    assert [(s["kind"], s["title"]) for s in data["signatures"]] == [
        ("message", "Autorisation de sortie")
    ]  # unread, recent, once
    # undone homework (2) and the signature (1) are tasks; done homework and grades are not
    assert len(result.intents) == 3
    assert {i["type"] for i in result.intents} == {"task.create"}


def test_the_children_the_period_and_the_horizon_can_be_chosen(trusted: Harness) -> None:
    result = trusted.run(children=["mathis"], periods=["trimestre 2"], days_ahead=60)
    assert result.error is None
    assert [c["first_name"] for c in result.data["children"]] == ["LEROY Mathis"]
    assert [g["subject"] for g in result.data["grades"]] == ["Sport"]
    assert len(result.data["homework"]) == 3  # the 30 December day is now in range
    assert not any("/7001/" in p for p in trusted.fake.paths())


def test_an_unknown_child_or_period_is_a_config_error_after_a_good_login(trusted: Harness) -> None:
    for params in ({"children": ["nobody"]}, {"periods": ["trimestre 9"]}):
        result = trusted.run(**params)
        assert result.error is not None
        assert "config_invalid" in result.error
        trusted.clock.advance(hours=2, minutes=1)
    guard = trusted.guard()
    assert guard["failures"] == 0  # the account is fine: no backoff, no stop
    assert guard["auth_stopped"] is None


# --- acceptance: the guard limits --------------------------------------------------------------


def test_never_more_than_the_configured_polls_per_day(trusted: Harness) -> None:
    h = trusted
    for _ in range(3):
        assert h.run(max_polls_per_day=3, min_interval="1m").error is None
        h.clock.advance(minutes=2)
    result = h.run(max_polls_per_day=3, min_interval="1m")
    assert result.error is not None
    assert result.error.startswith("degraded: budget_exhausted")
    assert len(h.fake.logins()) == 3  # the refused call made no request at all
    h.clock.set(paris(2026, 11, 10, 7, 0))  # the next local day starts again
    assert h.run(max_polls_per_day=3, min_interval="1m").error is None
    assert len(h.fake.logins()) == 4


def test_the_default_is_four_polls_and_a_login_each(trusted: Harness) -> None:
    trusted.clock.set(paris(2026, 11, 9, 1, 0))
    outcomes = [trusted.poll().error for _ in range(5)]
    assert outcomes[:4] == [None] * 4
    assert outcomes[4] is not None
    assert outcomes[4].startswith("degraded: budget_exhausted")
    assert len(trusted.fake.logins()) == 4


def test_min_interval_windows_and_the_login_budget_are_honoured_without_a_request(
    trusted: Harness,
) -> None:
    h = trusted
    assert h.run().error is None
    sent = len(h.fake.requests)
    again = h.run()
    assert again.error is not None
    assert "budget_exhausted" in again.error
    h.clock.set(paris(2026, 11, 9, 12, 0))
    outside = h.run(windows=["07:00-08:00"])
    assert outside.error is not None
    assert outside.error.startswith("degraded: outside_window")
    h.clock.advance(hours=3)
    capped = h.run(max_polls_per_day=4, max_logins_per_day=1)
    assert capped.error is not None
    assert "logins_per_day" in capped.error
    assert len(h.fake.requests) == sent


# --- acceptance: a trusted device is persisted, no repeated quiz -------------------------------


def presented(login: Request) -> Any:
    """The trusted device a login request presented."""
    assert login.body is not None
    return login.body["fa"]


def run_setup(h: Harness, choice: str = RIGHT, **kwargs: Any) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = ecoledirecte.setup(
        ["--account", "famille", "--store", str(h.store)],
        stdout=out,
        stderr=err,
        ask_choice=lambda question, choices: choice,
        session=h.fake,  # type: ignore[arg-type]
        environ=h.env,
        **kwargs,
    )
    return code, out.getvalue(), err.getvalue()


def test_the_setup_persists_the_device_and_the_scheduled_runs_are_never_asked_again(
    h: Harness,
) -> None:
    code, out, err = run_setup(h)
    assert code == 0, err
    assert len(h.fake.quizzes()) == 2  # the question was read and answered once, in the setup
    device = h.stored()["famille"]
    assert device == {"cn": "cn-1", "cv": "cv-1"}
    assert stat.S_IMODE(h.store.stat().st_mode) == 0o600
    assert not any(s in out + err for s in (LOGIN.password, "cn-1", "cv-1", "token-"))
    h.fake.requests.clear()
    for _ in range(3):  # three scheduled runs on three different hours
        assert h.poll().error is None
    assert h.fake.quizzes() == []  # no quiz, ever, in a scheduled run
    assert [presented(r) for r in h.fake.logins()] == [[{"cn": "cn-1", "cv": "cv-1"}]] * 3
    assert h.guard()["failures"] == 0


def test_a_rotated_device_is_persisted_right_after_the_login(trusted: Harness) -> None:
    h = trusted
    h.fake.rotate = True
    h.fake.read_code = 520  # every read fails: the new device must already be saved
    result = h.run()
    assert result.error == "degraded: source_unavailable (service)"
    assert h.stored()["famille"] == {"cn": "cn-2", "cv": "cv-2"}
    # the next login presents the new device, which the server now trusts
    h.fake.read_code = 200
    h.clock.advance(hours=3)
    assert h.run().error is None
    assert presented(h.fake.logins()[-1]) == [{"cn": "cn-2", "cv": "cv-2"}]
    assert h.stored()["famille"] == {"cn": "cn-3", "cv": "cv-3"}
    assert stat.S_IMODE(h.store.stat().st_mode) == 0o600


def test_a_store_that_cannot_be_written_stops_the_poll_before_the_login(trusted: Harness) -> None:
    trusted.store.write_text("not json")
    result = trusted.run()
    assert result.error is not None
    assert "credentials_invalid" in result.error
    assert trusted.fake.requests == []
    assert not trusted.state.exists()


def test_without_the_secret_nothing_is_spent(trusted: Harness) -> None:
    trusted.env = dict(ENV_TZ)
    result = trusted.run()
    assert result.error is not None
    assert "credentials_missing" in result.error
    assert trusted.fake.requests == []
    assert not trusted.state.exists()


def test_the_store_path_must_be_approved(trusted: Harness, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "sub").mkdir()
    result = trusted.run(credentials_path=str(elsewhere / "d.json"), paths=[str(tmp_path / "sub")])
    assert result.error is not None
    assert "path_not_approved" in result.error
    assert trusted.fake.requests == []


# --- acceptance: the quiz of a scheduled run stops the account ---------------------------------


def test_a_quiz_in_a_scheduled_run_is_never_answered_and_stops_the_account(h: Harness) -> None:
    result = h.run()  # no device was ever set up
    assert result.error == "degraded: auth_stopped (quiz_required)"
    assert h.guard()["auth_stopped"]["code"] == "quiz_required"
    assert h.fake.quizzes() == []  # the question was not even fetched, let alone answered
    assert h.stored() == {}  # nothing was stored: there is no device
    for days in (1, 5):
        h.clock.advance(hours=24 * days)
        again = h.run()
        assert again.error is not None
        assert again.error.startswith("degraded: auth_stopped")
    assert len(h.fake.logins()) == 1  # no retry, and no later attempt
    # the user reruns the setup and resets the stop (see the README): then it works
    code, _, err = run_setup(h)
    assert code == 0, err


def test_a_device_the_server_no_longer_trusts_is_a_quiz_in_a_scheduled_run_too(
    trusted: Harness,
) -> None:
    trusted.fake.trusted = set()
    assert trusted.run().error == "degraded: auth_stopped (quiz_required)"
    assert trusted.fake.quizzes() == []


# --- acceptance: degraded on any failure, no retry loop ----------------------------------------


def test_wrong_credentials_stop_the_account(trusted: Harness) -> None:
    trusted.env = {
        **ENV_TZ,
        ED_SECRET: json.dumps({"famille": {"username": "user-fixture", "password": "wrong"}}),
    }
    result = trusted.run()
    assert result.error == "degraded: auth_stopped (login_failed)"
    assert trusted.guard()["auth_stopped"]["code"] == "login_failed"
    for days in (1, 5):
        trusted.clock.advance(hours=24 * days)
        again = trusted.run()
        assert again.error is not None
        assert again.error.startswith("degraded: auth_stopped")
    assert len(trusted.fake.logins()) == 1


def test_a_lockout_message_stops_the_account(trusted: Harness) -> None:
    trusted.fake.login_answer = {"code": 505, "message": "Compte bloqué, trop de tentatives"}
    assert trusted.run().error == "degraded: auth_stopped (locked_out)"
    assert len(trusted.fake.logins()) == 1


@pytest.mark.parametrize(
    "failure",
    [ConnectionError("dns"), TimeoutError("slow"), requests.Timeout("slow"), OSError("reset")],
)
def test_a_network_failure_is_transient_and_backs_off(trusted: Harness, failure: Exception) -> None:
    h = trusted
    h.fake.fail = failure
    result = h.run()
    assert result.error == "degraded: source_unavailable (network)"
    assert h.guard()["auth_stopped"] is None
    assert h.guard()["failures"] == 1
    assert len(h.fake.requests) == 1  # one attempt, one request, no retry
    h.fake.fail = None
    h.clock.advance(minutes=30)  # still within the hour: refused without a request
    refused = h.run()
    assert refused.error is not None
    assert refused.error.startswith("degraded: backoff_until")
    assert len(h.fake.requests) == 1
    h.clock.advance(hours=2)  # after the backoff, one success resets the streak
    assert h.run().error is None
    assert h.guard()["failures"] == 0


@pytest.mark.parametrize(
    ("setting", "code"),
    [
        ({"status": 503}, "service"),
        ({"status": 429}, "service"),
        ({"status": 302}, "api_changed"),  # a redirect is never followed
        ({"raw": b"<html>maintenance</html>"}, "api_changed"),
        ({"raw": b'["not", "an", "object"]'}, "api_changed"),
        ({"raw": b'{"code": 200, "data": {"accounts": []}}'}, "api_changed"),
        ({"raw": b"x" * (ecoledirecte.MAX_BODY_BYTES + 1)}, "api_changed"),
    ],
)
def test_an_api_change_or_an_outage_is_transient_not_an_auth_failure(
    trusted: Harness, setting: dict[str, Any], code: str
) -> None:
    for name, value in setting.items():
        setattr(trusted.fake, name, value)
    assert trusted.run().error == f"degraded: source_unavailable ({code})"
    assert trusted.guard()["auth_stopped"] is None
    assert len(trusted.fake.logins()) <= 1


def test_a_refused_read_is_transient_even_with_an_error_code(trusted: Harness) -> None:
    trusted.fake.read_code = 403
    assert trusted.run().error == "degraded: source_unavailable (service)"
    assert trusted.guard()["auth_stopped"] is None
    assert len(trusted.fake.logins()) == 1  # and the session is not renewed behind our back


def test_failures_never_carry_a_credential_or_a_body(trusted: Harness) -> None:
    trusted.fake.rotate = False
    failures: list[dict[str, Any]] = [
        {"login_answer": {"code": 505, "message": "secret body for user-fixture"}},
        {"fail": OSError(f"https://{HOST} pw-fixture cn-1")},
        {"read_code": 403},
        {"raw": b"secret body cn-1 pw-fixture"},
    ]
    for setting in failures:
        trusted.fake = Fake()
        trusted.trust()
        trusted.state.unlink(missing_ok=True)
        for name, value in setting.items():
            setattr(trusted.fake, name, value)
        error = trusted.run().error
        assert error is not None
        assert error.startswith("degraded: ")
        assert not any(s in error for s in [*SECRETS, "secret body"])


# --- the network -------------------------------------------------------------------------------


def test_a_host_the_lock_does_not_approve_is_refused_before_any_request(trusted: Harness) -> None:
    result = trusted.run(network=["other.example.org"])
    assert result.error is not None
    assert "network_not_approved" in result.error
    assert trusted.fake.requests == []
    assert not trusted.state.exists()


def test_without_a_list_the_source_falls_back_to_the_declared_host(trusted: Harness) -> None:
    plugin = build(clock=trusted.clock, environ=trusted.env, ed_connect=trusted.fake.connect)

    async def go() -> Replayed:
        return await call_tool(
            plugin,
            "school-fr.ecoledirecte",
            {"account": "famille", "state_path": str(trusted.state)},
            paths={"write": [str(trusted.tmp)]},
            network=None,  # an older core: unknown, not empty
        )

    assert anyio.run(go).error is None


def test_check_host_follows_host_allowed() -> None:
    ecoledirecte.check_host(Call("school-fr.ecoledirecte"))  # None: the declared host
    with pytest.raises(PluginError, match="network_not_approved"):
        ecoledirecte.check_host(Call("school-fr.ecoledirecte", network=Network(("other.org",))))
    ecoledirecte.check_host(Call("school-fr.ecoledirecte", network=Network((HOST,))))


def test_every_request_goes_to_the_one_host_without_redirect_and_with_a_timeout(
    trusted: Harness,
) -> None:
    assert trusted.run().error is None
    assert trusted.fake.requests
    assert f"https://{HOST}/v3" == ecoledirecte.BASE
    for request in trusted.fake.requests:
        assert request.allow_redirects is False
        assert request.timeout == ecoledirecte.HTTP_TIMEOUT_S


def test_the_manifest_declares_the_host() -> None:
    from horae_school_fr import manifest

    assert manifest()["permissions"]["network"] == [HOST]
    assert ED_SECRET in manifest()["permissions"]["optional_secrets"]


# --- read-only ---------------------------------------------------------------------------------


def test_a_whole_poll_only_reads(trusted: Harness) -> None:
    assert trusted.run().error is None
    for request in trusted.fake.requests:
        if request.path == "/login.awp":
            continue  # the login itself (and the GTK request that precedes it)
        assert request.query["verbe"] == "get", request.path
        assert any(route.fullmatch(request.path) for route in ecoledirecte.READ_ROUTES)
    assert trusted.fake.quizzes() == []
    sent = {r.path.split("/")[-1] for r in trusted.fake.requests}
    assert "messages.awp" in sent  # the list is read, and no message is ever opened
    assert not any("/messages/" in p for p in trusted.fake.paths())


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("/eleves/7001/cahierdetexte.awp", {"verbe": "put"}),  # marking homework done
        ("/eleves/7001/cahierdetexte.awp", {"verbe": "get"}),  # the verb is not the caller's
        ("/familles/9001/messages.awp", {"verbe": "post"}),  # replying
        ("/familles/9001/messages/123.awp", {}),  # opening a message marks it read
        ("/eleves/7001/notes.awp/../../x", {}),
        ("/eleves/abc/notes.awp", {}),
        ("/connexion/doubleauth.awp", {}),
        ("/login.awp", {}),
        ("https://elsewhere.example.org/v3/eleves/7001/notes.awp", {}),
    ],
)
def test_the_api_refuses_every_route_or_verb_outside_the_read_allow_list(
    path: str, query: dict[str, str]
) -> None:
    fake = Fake()
    api = ecoledirecte.Api(fake)  # type: ignore[arg-type]
    with pytest.raises(ecoledirecte.ReadOnlyViolation):
        api.read(path, query)
    assert fake.requests == []


def test_the_client_has_the_read_methods_and_no_others() -> None:
    public = {
        name
        for name in dir(ecoledirecte.Client)
        if not name.startswith("_") and callable(getattr(ecoledirecte.Client, name))
    }
    assert public == ecoledirecte.READ_METHODS


def test_the_module_has_no_third_party_client_and_no_write_verb() -> None:
    source = Path(ecoledirecte.__file__).read_text("utf-8")
    imports = set(re.findall(r"^(?:import|from)\s+([A-Za-z_][\w]*)", source, re.MULTILINE))
    assert imports - {"horae_school_fr"} <= {
        "argparse",
        "base64",
        "binascii",
        "getpass",
        "html",
        "json",
        "os",
        "re",
        "sys",
        "collections",
        "dataclasses",
        "datetime",
        "typing",
        "urllib",
        "requests",
        "horae_sdk",
    }
    assert not re.search(r"verbe[\"']?\s*[:=]\s*[\"'](put|delete|patch)", source)


# --- timeouts ----------------------------------------------------------------------------------


def test_the_login_is_cut_off_when_the_server_never_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ecoledirecte, "HTTP_TIMEOUT_S", 0.4)
    with hanging_server() as port:
        monkeypatch.setattr(ecoledirecte, "BASE", f"http://127.0.0.1:{port}/v3")
        started = time.monotonic()
        with pytest.raises(OSError, match=r".") as caught:
            ecoledirecte.login(LOGIN, None)
    assert time.monotonic() - started < 3
    assert isinstance(
        ecoledirecte.classify(caught.value, logging_in=True), ecoledirecte.SourceUnavailable
    )


def test_a_read_after_the_login_is_cut_off_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ecoledirecte, "HTTP_TIMEOUT_S", 0.4)
    with hanging_server() as port:
        monkeypatch.setattr(ecoledirecte, "BASE", f"http://127.0.0.1:{port}/v3")
        api = ecoledirecte.Api()
        api.token = "token-1"
        started = time.monotonic()
        with pytest.raises(OSError, match=r"."):
            api.read("/eleves/7001/notes.awp")
    assert time.monotonic() - started < 3


def test_the_session_itself_carries_a_default_timeout() -> None:
    api = ecoledirecte.Api()
    assert api.session.default_timeout == ecoledirecte.HTTP_TIMEOUT_S  # type: ignore[attr-defined]


# --- de-identification -------------------------------------------------------------------------


def test_the_names_the_source_outputs_are_declared_for_de_identification(trusted: Harness) -> None:
    data = trusted.run().data
    assert "children[].first_name" in DEIDENTIFIED_FIELDS
    assert "homework[].teacher" in DEIDENTIFIED_FIELDS
    sent = pseudonymize(
        [{"source": "school-fr.ecoledirecte", "data": data}],
        {path: options for path, options in input_view().items() if options is not None},
    ).data
    text = json.dumps(sent, ensure_ascii=False)
    for name in ("Zoé", "Mathis", "LEROY", "LEFEVRE", "DUPONT"):
        assert name.casefold() not in text.casefold(), name
    assert "[PERSON_" in text


def test_a_quiz_in_a_scheduled_run_classifies_as_the_guards_auth_failure() -> None:
    failure = ecoledirecte.classify(ecoledirecte._QuizRequired(), logging_in=True)
    assert isinstance(failure, AuthFailure)
    assert failure.code == "quiz_required"

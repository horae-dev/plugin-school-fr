# SPDX-License-Identifier: Apache-2.0
"""The ÉcoleDirecte source `school-fr.ecoledirecte` and the `ecoledirecte-setup` helper.

**Our own minimal client, no third-party ÉcoleDirecte library.** The maintained candidate
(`ecoledirecte` 0.4.x, the Home Assistant integration's client) is LGPL-3.0-or-later, needs Python
3.14 (this plugin: 3.12), is asynchronous, re-authenticates on its own after an error (the exact
retry behaviour the lockout guard forbids), answers a security question from a pre-supplied
`qcm_json` and has no trusted-device (`cn`/`cv`) persistence, and its newest release was two days
old (the project's dependency cooldown is seven). So this module speaks the community-documented
HTTP API directly: `requests` (already a dependency, through pronotepy), a `TimeoutSession` (an
explicit timeout on every request), **no redirect followed** (a 3xx is an API change), a fixed
host, a response size cap and a **route allow-list**. Nothing here is an official API:
risk horae-dev/horae#73.

**One poll** (`run`), in this order, each step before the next one can touch the network:

1. the parameters, the limits, the approved paths, the login (secret `ECOLEDIRECTE_CREDENTIALS`)
   and the trusted device (the device store);
2. the host check: `call.host_allowed(...)` (see `check_host`);
3. the device store is proven writable;
4. `guard.begin(login=True)`: the budget is reserved, or `Refused` ends the call;
5. inside the attempt: **one** login presenting the trusted device, then the device the server
   returned (it may rotate) is persisted **at once**, then the reads, then the device again if it
   changed. No step is retried.

**The security question (QCM).** ÉcoleDirecte answers a login from an unknown device with a
question (code 250). A scheduled source never answers one: `login` without an `ask` callback
raises `AuthFailure("quiz_required")`, which stops the account; the user runs
`ecoledirecte-setup` once. The helper is the only code that holds an `ask` callback: it shows the
question and the choices, takes the answer, and stores the `cn`/`cv` pair the server then issues.
The next logins present it and are not asked again.

**Read-only.** Every request of a poll goes through `Api.read`, which accepts only the routes of
`READ_ROUTES` and always sends `verbe=get`; `Client` has the public methods of `READ_METHODS`
only. The write verbs of the API (`put`, `post`, `delete`: marking homework done, replying to
or deleting a message) cannot be expressed. Messages are listed, never opened (opening one marks
it read).

**Failures.** Wrong credentials, a lockout, a quiz in a scheduled run (or a wrong answer) raise
`AuthFailure(code)` (the guard stops the account); a network error, a timeout, an HTTP error, a
non-JSON body or an API change raises `SourceUnavailable` (the guard backs off). Messages are
fixed codes: never a credential, a `cn`/`cv`, a URL, a name or a body.
"""

import argparse
import base64
import binascii
import getpass
import html.parser
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final, TextIO
from urllib.parse import quote

import requests
from horae_sdk import Call, Output, PluginError

from horae_school_fr._common import (
    Options,
    SourceUnavailable,
    chain,
    child_id,
    fold,
    grade_value,
    number,
    resolve_path,
    strings,
    wanted_child,
)
from horae_school_fr._http import TimeoutSession
from horae_school_fr.credentials import (
    DEVICE_NAME,
    ED_SECRET,
    Device,
    DeviceStore,
    Login,
    login_from_secret,
    parse_device,
)
from horae_school_fr.guard import LABEL, AuthFailure, Clock, Guard, Limits
from horae_school_fr.normalize import (
    MAX_CHILDREN,
    Child,
    Grade,
    Homework,
    Signature,
    normalize,
)

REF: Final = "school-fr.ecoledirecte"
HOST: Final = "api.ecoledirecte.com"
"""The only host this module talks to; declared in the manifest (`permissions.network`)."""
BASE: Final = f"https://{HOST}/v3"
API_VERSION: Final = "4.75.0"
"""The `v` query parameter every request carries (the community clients send the web app's)."""
HTTP_TIMEOUT_S: Final = 30.0
MAX_BODY_BYTES: Final = 4 * 1024 * 1024
MAX_DAYS_FETCHED: Final = 20
"""Most homework days fetched in one poll (one request each)."""
INFO_DAYS: Final = 30
"""Unread messages are listed since this many days."""
MAX_GRADES: Final = 200
MAX_TEXT: Final = 1000
USER_AGENT: Final = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
ORIGIN: Final = "https://www.ecoledirecte.com"

CODE_OK: Final = 200
CODE_QUIZ: Final = 250
"""Login answer: a security question must be answered first."""

_ID = re.compile(r"\d{1,12}")
_LOCKED = re.compile(r"bloqu|verrouill|trop de tentatives|suspendu", re.IGNORECASE)

# --- the allowed routes ------------------------------------------------------------------------

READ_ROUTES: Final = (
    re.compile(r"/eleves/\d{1,12}/cahierdetexte\.awp"),  # the homework calendar
    re.compile(r"/eleves/\d{1,12}/cahierdetexte/\d{4}-\d{2}-\d{2}\.awp"),  # one day's homework
    re.compile(r"/eleves/\d{1,12}/notes\.awp"),  # grades
    re.compile(r"/(?:familles|eleves)/\d{1,12}/messages\.awp"),  # the message list
)
"""The only routes a poll may request, always with `verbe=get`. (The API reads with POST: what
makes a request a read is its `verbe`.)"""
READ_METHODS: Final = frozenset({"homework", "grades", "unread_messages", "export_device"})
"""The public methods of `Client`: the only things the source may ask of it."""


class ReadOnlyViolation(Exception):
    """A route or a verb outside the read allow-list was asked of the client."""


# --- failures inside the client ---------------------------------------------------------------


class _QuizRequired(Exception):
    """ÉcoleDirecte asked a security question and nobody may answer it here."""


class _QuizFailed(Exception):
    """The answer to the security question was refused (or the question could not be read)."""


class _LoginRejected(Exception):
    """The login was answered with an error code: wrong user name or password."""


class _Locked(Exception):
    """The login was answered with a lockout message."""


class _ApiError(Exception):
    """A coded refusal on a read (an expired token, a missing right...)."""


class _ApiChanged(Exception):
    """A redirect, a non-JSON body, a body too large or a shape we do not know."""


class _Service(Exception):
    """An HTTP 5xx or 429."""


# --- the HTTP API ------------------------------------------------------------------------------

Ask = Callable[[str, Sequence[str]], str]
"""The interactive security-question callback of the setup helper: `(question, choices)` to the
chosen answer (one of `choices`)."""


def _b64(text: object) -> str:
    """A base64 field of the API as text (`""` when it is not valid)."""
    if not isinstance(text, str):
        return ""
    try:
        return base64.b64decode(text, validate=False).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return ""


def _int_id(value: object) -> str:
    text = str(value)
    if not _ID.fullmatch(text):
        raise _ApiChanged
    return text


class Api:
    """The HTTP layer: a fixed host, an explicit timeout, no redirect, a size cap, a route
    allow-list for reads. `ask` is the setup helper's quiz callback; without it, a security
    question is a `_QuizRequired`."""

    def __init__(self, session: requests.Session | None = None, ask: Ask | None = None) -> None:
        self.session: requests.Session = session or TimeoutSession(HTTP_TIMEOUT_S)
        self.ask = ask
        self.token: str | None = None
        self.gtk: str | None = None
        self.device: Device | None = None

    # -- transport

    def _send(
        self,
        method: str,
        path: str,
        query: Mapping[str, str],
        body: Mapping[str, Any] | None,
        *,
        json_body: bool = True,
    ) -> dict[str, Any]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Origin": ORIGIN,
            "Referer": ORIGIN + "/",
        }
        if self.token:
            headers["X-Token"] = self.token
        if self.gtk:
            headers["X-GTK"] = self.gtk
        response = self.session.request(
            method,
            BASE + path,
            params={**query, "v": API_VERSION},
            data=None if body is None else {"data": json.dumps(body, separators=(",", ":"))},
            headers=headers,
            allow_redirects=False,
            stream=True,
            timeout=HTTP_TIMEOUT_S,
        )
        try:
            status = response.status_code
            if 300 <= status < 400:
                raise _ApiChanged
            if status >= 500 or status == 429:
                raise _Service
            raw = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                raw += chunk
                if len(raw) > MAX_BODY_BYTES:
                    raise _ApiChanged
            token = response.headers.get("x-token")
            if token:
                self.token = token
        finally:
            response.close()
        if not json_body:
            return {}
        try:
            document = json.loads(raw)
        except (ValueError, RecursionError):
            raise _ApiChanged from None
        if not isinstance(document, dict) or not isinstance(document.get("code"), int):
            raise _ApiChanged
        if isinstance(document.get("token"), str) and document["token"]:
            self.token = document["token"]
        return document

    # -- the reads: the allow-list

    def read(
        self,
        path: str,
        query: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        """One read: `path` must be one of `READ_ROUTES`; the verb is always `get`. Returns the
        `data` of a code-200 answer; any other code is `_ApiError`."""
        extra = dict(query or {})
        if "verbe" in extra or not any(route.fullmatch(path) for route in READ_ROUTES):
            raise ReadOnlyViolation("not a read route")
        document = self._send("POST", path, {**extra, "verbe": "get"}, body or {})
        if document["code"] != CODE_OK:
            raise _ApiError
        return document.get("data")

    # -- the login

    def _login_request(self, login: Login, device: Device | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "identifiant": quote(login.username, safe=""),
            "motdepasse": quote(login.password, safe=""),
            "isReLogin": False,
            "uuid": "",
        }
        if device is not None:
            body["fa"] = [{"cn": device.cn, "cv": device.cv}]
        return self._send("POST", "/login.awp", {}, body)

    def login(self, login: Login, device: Device | None) -> dict[str, Any]:
        """One login; the answer's `data` (with `accounts`). A security question is answered
        only when `ask` is set (the setup helper). Raises the internal failures above."""
        self._send("GET", "/login.awp", {"gtk": "1"}, None, json_body=False)  # the GTK cookie
        self.gtk = self.session.cookies.get("GTK")
        if not self.gtk:
            raise _ApiChanged
        answer = self._login_request(login, device)
        if answer["code"] == CODE_QUIZ:
            if self.ask is None:
                raise _QuizRequired
            device = self._answer_quiz(self.ask)
            self.device = device
            answer = self._login_request(login, device)
        if answer["code"] != CODE_OK:
            message = str(answer.get("message", ""))
            raise _Locked if _LOCKED.search(message) else _LoginRejected
        data = answer.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
            raise _ApiChanged
        rotated = _device_in(answer)
        if rotated is not None:
            self.device = rotated
        elif device is not None:
            self.device = device
        return data

    def _answer_quiz(self, ask: Ask) -> Device:
        """The setup helper's only: read the question, ask the user, post the answer, return the
        `cn`/`cv` issued. Never reachable without `ask`."""
        question = self._send("POST", "/connexion/doubleauth.awp", {"verbe": "get"}, {})
        data = question.get("data")
        if question["code"] != CODE_OK or not isinstance(data, dict):
            raise _QuizFailed
        choices = [_b64(c) for c in data.get("propositions", []) if isinstance(c, str)]
        if not choices:
            raise _QuizFailed
        picked = ask(_b64(data.get("question")), choices)
        if picked not in choices:
            raise _QuizFailed
        encoded = base64.b64encode(picked.encode("utf-8")).decode("ascii")
        result = self._send(
            "POST", "/connexion/doubleauth.awp", {"verbe": "post"}, {"choix": encoded}
        )
        issued = result.get("data")
        if result["code"] != CODE_OK or not isinstance(issued, dict):
            raise _QuizFailed
        try:
            return parse_device({"cn": issued.get("cn"), "cv": issued.get("cv")})
        except PluginError:
            raise _QuizFailed from None


def _device_in(answer: Mapping[str, Any]) -> Device | None:
    """The trusted device a login answer carries, if any (the server may rotate it)."""
    data = answer.get("data")
    candidates: list[Any] = [answer, data]
    if isinstance(data, dict):
        candidates += [data.get("fa"), data.get("device")]
    for candidate in candidates:
        entry = candidate[0] if isinstance(candidate, list) and candidate else candidate
        if isinstance(entry, dict) and "cn" in entry and "cv" in entry:
            try:
                return parse_device({"cn": entry["cn"], "cv": entry["cv"]})
            except PluginError:
                return None
    return None


# --- the client --------------------------------------------------------------------------------


@dataclass(frozen=True)
class ChildInfo:
    """A child as the platform names it. `id` is the platform's, never published."""

    id: str
    name: str


def _text(html_text: str) -> str:
    class Collector(html.parser.HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.parts: list[str] = []
            self.skip = 0

        def handle_starttag(self, tag: str, attrs: Any) -> None:
            if tag in {"script", "style"}:
                self.skip += 1
            elif tag in {"br", "p", "li", "div"}:
                self.parts.append(" ")

        def handle_endtag(self, tag: str) -> None:
            if tag in {"script", "style"} and self.skip:
                self.skip -= 1

        def handle_data(self, data: str) -> None:
            if not self.skip:
                self.parts.append(data)

    collector = Collector()
    collector.feed(html_text)
    collector.close()
    return " ".join("".join(collector.parts).split())[:MAX_TEXT]


def _day(text: object) -> date | None:
    try:
        return date.fromisoformat(str(text)[:10])
    except ValueError:
        return None


def _rows(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise _ApiChanged
    return [row for row in value if isinstance(row, dict)]


class Client:
    """What a poll may do: the read methods of `READ_METHODS` and the `children` list."""

    def __init__(self, api: Api, data: Mapping[str, Any]) -> None:
        self._api = api
        accounts = _rows(data.get("accounts"))
        if not accounts:
            raise _ApiChanged
        account = accounts[0]
        self._account = _int_id(account.get("id"))
        kind = str(account.get("typeCompte"))
        if kind == "E":
            self._family = False
            self.children: tuple[ChildInfo, ...] = (
                ChildInfo(
                    self._account, f"{account.get('nom', '')} {account.get('prenom', '')}".strip()
                ),
            )
        elif kind == "1":
            self._family = True
            profile = account.get("profile")
            kids = _rows(profile.get("eleves") if isinstance(profile, dict) else None)
            self.children = tuple(
                ChildInfo(_int_id(k.get("id")), f"{k.get('nom', '')} {k.get('prenom', '')}".strip())
                for k in kids
            )
        else:
            raise _ApiChanged  # a teacher or staff account: not for this plugin

    def export_device(self) -> Device | None:
        """The trusted device after the login (it may have rotated); no request."""
        return self._api.device

    def homework(
        self, kid: str, start: date, end: date
    ) -> list[tuple[str, str, str | None, date, bool, int]]:
        """`(subject, text, teacher, due, done, attachments)` of the homework due in
        `start..end`, nearest days first (at most `MAX_DAYS_FETCHED` days)."""
        calendar = self._api.read(f"/eleves/{_int_id(kid)}/cahierdetexte.awp")
        if not isinstance(calendar, dict):
            raise _ApiChanged
        days = sorted(
            d for d in (_day(k) for k in calendar) if d is not None and start <= d <= end
        )[:MAX_DAYS_FETCHED]
        found: list[tuple[str, str, str | None, date, bool, int]] = []
        for due in days:
            data = self._api.read(f"/eleves/{_int_id(kid)}/cahierdetexte/{due.isoformat()}.awp")
            if not isinstance(data, dict):
                raise _ApiChanged
            for subject in _rows(data.get("matieres")):
                todo = subject.get("aFaire")
                if not isinstance(todo, dict):
                    continue
                documents = todo.get("documents")
                teacher = subject.get("nomProf")
                found.append(
                    (
                        str(subject.get("matiere") or "?"),
                        _text(_b64(todo.get("contenu"))),
                        str(teacher) if teacher else None,
                        due,
                        bool(todo.get("effectue")),
                        len(documents) if isinstance(documents, list) else 0,
                    )
                )
        return found

    def grades(
        self, kid: str, periods: tuple[str, ...] | None, today: date
    ) -> list[tuple[str, float | str | None, float, date, str]]:
        """`(subject, value, out_of, date, comment)` of the chosen periods (default: the one
        that contains `today`). Raises `LookupError("periods")` for an unknown name."""
        data = self._api.read(f"/eleves/{_int_id(kid)}/notes.awp", body={"anneeScolaire": ""})
        if not isinstance(data, dict):
            raise _ApiChanged
        listed = _rows(data.get("periodes"))
        if periods is None:
            current = [
                p
                for p in listed
                if (a := _day(p.get("dateDebut")))
                and (b := _day(p.get("dateFin")))
                and a <= today <= b
            ]
            codes = {str(p.get("codePeriode")) for p in current}
        else:
            wanted = {fold(p) for p in periods}
            chosen = [p for p in listed if fold(str(p.get("periode"))) in wanted]
            if len(chosen) < len(wanted):
                raise LookupError("periods")
            codes = {str(p.get("codePeriode")) for p in chosen}
        grades: list[tuple[str, float | str | None, float, date, str]] = []
        for mark in _rows(data.get("notes")):
            when = _day(mark.get("date"))
            if when is None or (listed and str(mark.get("codePeriode")) not in codes):
                continue
            grades.append(
                (
                    str(mark.get("libelleMatiere") or "?"),
                    grade_value(mark.get("valeur")),
                    number(mark.get("noteSur")) or 20.0,
                    when,
                    str(mark.get("commentaire") or ""),
                )
            )
        return grades

    def unread_messages(self, since: datetime) -> list[tuple[str, datetime]]:
        """`(subject, date)` of the unread received messages since `since`. The list only: a
        message is never opened (that would mark it read)."""
        scope = "familles" if self._family else "eleves"
        data = self._api.read(
            f"/{scope}/{self._account}/messages.awp",
            {
                "force": "false",
                "typeRecuperation": "received",
                "idClasseur": "0",
                "orderBy": "date",
                "order": "desc",
                "query": "",
                "onlyRead": "",
                "page": "0",
                "itemsPerPage": "100",
                "getAll": "0",
            },
            {"anneeMessages": ""},
        )
        if not isinstance(data, dict) or not isinstance(data.get("messages"), dict):
            raise _ApiChanged
        found: list[tuple[str, datetime]] = []
        for message in _rows(data["messages"].get("received")):
            if message.get("read") is not False:
                continue
            try:
                sent = datetime.fromisoformat(str(message.get("date")))
            except ValueError:
                continue
            if sent >= since:
                found.append((str(message.get("subject") or "")[:MAX_TEXT], sent))
        return found


def login(
    login_: Login, device: Device | None, *, session: requests.Session | None = None
) -> Client:
    """One ÉcoleDirecte login of a scheduled run (the real `connect`): no security question is
    ever answered (`_QuizRequired`)."""
    api = Api(session)
    return Client(api, api.login(login_, device))


Connect = Callable[[Login, "Device | None"], Client]


# --- failures ----------------------------------------------------------------------------------


def classify(error: BaseException, *, logging_in: bool) -> PluginError:
    """`AuthFailure` for a quiz in a scheduled run, a refused answer, wrong credentials or a
    lockout; `SourceUnavailable` for everything else (a refusal of a read included)."""
    errors = list(chain(error))
    for kind, code in (
        (_QuizRequired, "quiz_required"),
        (_QuizFailed, "quiz_failed"),
        (_Locked, "locked_out"),
        (_LoginRejected, "login_failed"),
    ):
        if any(isinstance(e, kind) for e in errors):
            return AuthFailure(code)
    if any(isinstance(e, OSError) for e in errors):
        return SourceUnavailable("network")
    if any(isinstance(e, _Service | _ApiError) for e in errors):
        return SourceUnavailable("service")
    return SourceUnavailable("api_changed")


# --- the host ----------------------------------------------------------------------------------


def check_host(call: Call) -> None:
    """Refuse, before any login, if the instance did not approve `api.ecoledirecte.com`.

    `call.host_allowed(url)`: `False` refuses (the host is not in `horae.lock`); `True` is
    allowed; `None` (an older core sent no list) falls back to the host this plugin declares in
    its manifest, which is the only one this module ever contacts."""
    if call.host_allowed(BASE) is False:
        raise PluginError(
            f"network_not_approved: {HOST} is not approved in horae.lock (permissions.network)"
        )


# --- reading -----------------------------------------------------------------------------------


def read(
    client: Any, options: Options, today: date
) -> tuple[list[Child], list[Homework], list[Grade], list[Signature]]:
    """Everything the source reads: homework from `today` to `days_ahead`, the grades of the
    chosen periods (newest `MAX_GRADES` per child) and the unread messages (attributed to the
    first selected child: they are addressed to the parent). Raises `LookupError` when no child
    matches or a period is unknown."""
    chosen: list[tuple[ChildInfo, Child]] = []
    for info in client.children:
        opaque = child_id(info.id)
        if wanted_child(info.name, opaque, options.children):
            chosen.append((info, Child(opaque, info.name)))
    if not chosen or len(chosen) > MAX_CHILDREN:
        raise LookupError("children")

    homework: list[Homework] = []
    grades: list[Grade] = []
    signatures: list[Signature] = []
    end = today + timedelta(days=options.days_ahead)
    for position, (info, child) in enumerate(chosen):
        for subject, text, teacher, due, done, attachments in client.homework(info.id, today, end):
            homework.append(Homework(child.id, subject, text, due, done, attachments, teacher))
        found = [
            Grade(child.id, subject, value, out_of, when, comment)
            for subject, value, out_of, when, comment in client.grades(
                info.id, options.periods, today
            )
        ]
        found.sort(key=lambda g: g.date, reverse=True)
        grades += found[:MAX_GRADES]
        if position == 0:
            since = datetime.combine(today - timedelta(days=INFO_DAYS), datetime.min.time())
            for subject, sent in client.unread_messages(since):
                signatures.append(Signature(child.id, subject or "(sans objet)", sent, "message"))
    return [child for _, child in chosen], homework, grades, signatures


# --- the source --------------------------------------------------------------------------------


def run(
    call: Call,
    *,
    account: object,
    state_path: object,
    credentials_path: str | None,
    limits: Limits,
    options: Options,
    guard: Guard,
    connect: Connect,
    clock: Clock,
    environ: Mapping[str, str],
) -> Output:
    """One poll of one account (see the module docstring). Raises `Refused` (a `PluginError`
    that the core marks degraded) when the budget says no; no network call happens then."""
    label = account if isinstance(account, str) else ""
    path = resolve_path(call, credentials_path, guard.budget.state_path, DEVICE_NAME)
    store = DeviceStore(path)
    login_ = login_from_secret(environ.get(ED_SECRET), label)
    if login_ is None:
        raise PluginError(
            f"credentials_missing: no user name and password for this account; put them in "
            f"the {ED_SECRET} secret, then run ecoledirecte-setup"
        )
    device = store.load(label)
    check_host(call)
    store.check_writable()
    today = clock().astimezone(guard.budget.zone).date()

    attempt = guard.begin(login=True)  # one attempt; raises Refused
    missing: str | None = None
    with attempt:
        logging_in = True
        try:
            client = connect(login_, device)
            current = client.export_device()
            _persist(store, label, current)
            logging_in = False
            try:
                result = read(client, options, today)
            except LookupError as error:
                missing = str(error)  # the login worked: not a failure of the account
                result = None
            after = client.export_device()
            if after != current:
                _persist(store, label, after)
        except PluginError:
            raise
        except Exception as error:
            raise classify(error, logging_in=logging_in) from None
    if result is None:
        raise PluginError(f"config_invalid: no {missing} matches the children or periods asked")
    children, homework, grades, signatures = result
    return normalize(
        label,
        children,
        homework=homework,
        grades=grades,
        signatures=signatures,
        tz=guard.budget.zone,
    )


def _persist(store: DeviceStore, account: str, device: Device | None) -> None:
    if device is None:
        return
    try:
        store.save(account, device)
    except PluginError:
        raise SourceUnavailable("credentials_unwritable") from None


# --- ecoledirecte-setup ------------------------------------------------------------------------

SETUP_USAGE = "usage: python -m horae_school_fr ecoledirecte-setup --account LABEL --store PATH"


def setup(
    args: Sequence[str],
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    ask_user: Callable[[], str] | None = None,
    ask_password: Callable[[], str] | None = None,
    ask_choice: Callable[[str, Sequence[str]], str] | None = None,
    session: requests.Session | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """`python -m horae_school_fr ecoledirecte-setup`, run **once** by the user.

    Takes the user name and password from the `ECOLEDIRECTE_CREDENTIALS` secret's entry for the
    account, or prompts for them. Logs in **once** (no retry). If ÉcoleDirecte asks its security
    question, shows it with its numbered choices and takes **one** answer; the `cn`/`cv` pair the
    server issues is written to the device store `--store PATH` (mode 0600). Prints nothing
    secret. A wrong answer or a bad password is one failed attempt: the helper stops, never loops.
    Exit status 0, or 1 with a fixed message."""
    out, err, source = stdout or sys.stdout, stderr or sys.stderr, stdin or sys.stdin
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="ecoledirecte-setup", add_help=False)
    parser.add_argument("--account", required=True)
    parser.add_argument("--store", required=True)
    try:
        parsed = parser.parse_args(list(args))
    except SystemExit:
        err.write(SETUP_USAGE + "\n")
        return 1

    def choose(question: str, choices: Sequence[str]) -> str:
        if ask_choice is not None:
            return ask_choice(question, choices)
        out.write(f"\nSecurity question: {question}\n")
        for number_, choice in enumerate(choices, 1):
            out.write(f"  {number_}. {choice}\n")
        out.write("Answer number: ")
        out.flush()
        line = source.readline().strip()
        if not line.isdigit() or not 1 <= int(line) <= len(choices):
            raise PluginError("quiz_failed: the answer is the number of one choice")
        return choices[int(line) - 1]

    try:
        if not LABEL.fullmatch(parsed.account):
            raise PluginError("config_invalid: --account is a label (A-Z a-z 0-9 . _ -)")
        login_ = login_from_secret(env.get(ED_SECRET), parsed.account)
        if login_ is None:
            username = ask_user() if ask_user else input("ÉcoleDirecte user name: ")
            password = ask_password() if ask_password else getpass.getpass("Password: ")
            if not username or not password:
                raise PluginError("credentials_invalid: a user name and a password are needed")
            login_ = Login(username.strip(), password)
        store = DeviceStore(parsed.store)
        store.check_writable()
        api = Api(session, choose)
        try:
            api.login(login_, None)
        except PluginError:
            raise
        except Exception as error:
            failure = classify(error, logging_in=True)
            kind = failure.code if isinstance(failure, AuthFailure) else "unavailable"
            raise PluginError(f"login_failed: the login did not work ({kind})") from None
        if api.device is None:
            err.write("No security question was asked: there is no trusted device to store.\n")
        else:
            store.save(parsed.account, api.device)
            err.write("Trusted device written to the store (mode 0600).\n")
    except (PluginError, OSError) as error:
        err.write(f"error: {error if isinstance(error, PluginError) else 'cannot read a file'}\n")
        return 1
    return 0


__all__ = [
    "BASE",
    "HOST",
    "READ_METHODS",
    "READ_ROUTES",
    "REF",
    "Api",
    "Client",
    "Options",
    "ReadOnlyViolation",
    "SourceUnavailable",
    "check_host",
    "classify",
    "login",
    "read",
    "run",
    "setup",
    "strings",
]

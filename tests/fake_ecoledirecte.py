# SPDX-License-Identifier: Apache-2.0
"""A fake ÉcoleDirecte server for the tests, at the level of the HTTP session: no network, the
real `Api` and `Client` code runs against it. Synthetic names, user names and tokens only.

It records every request (`Fake.requests`) so the tests can check what a poll sent."""

import base64
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from horae_school_fr import ecoledirecte
from horae_school_fr.credentials import Device, Login

LOGIN = Login("user-fixture", "pw-fixture")
QUESTION = "Quelle est la couleur du cheval blanc ?"
CHOICES = ["bleu", "blanc", "vert"]
RIGHT = "blanc"


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    body: dict[str, Any] | None
    headers: Mapping[str, str]
    allow_redirects: object
    timeout: object


class Response:
    def __init__(self, status: int, content: bytes, headers: Mapping[str, str] | None = None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size: int = 1) -> Iterator[bytes]:
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start : start + chunk_size]

    def close(self) -> None:
        self.closed = True


class Fake:
    """The server and the session in one: pass it as `session=`."""

    def __init__(self) -> None:
        self.requests: list[Request] = []
        self.cookies: dict[str, str] = {}
        self.trusted: set[tuple[str, str]] = set()
        self.issued = 0
        self.token = 0
        self.rotate = False
        """Every login answers with a new trusted device (and forgets the old one)."""
        self.fail: BaseException | None = None
        """Raised by every request."""
        self.fail_on: str | None = None
        """Restrict `fail` to the paths that end with this."""
        self.status: int | None = None
        self.raw: bytes | None = None
        """A fixed body for every answer."""
        self.login_answer: dict[str, Any] | None = None
        self.read_code = 200
        self.quiz_needed = True
        """A login without a trusted device is asked the question."""

    # -- what the tests look at

    def paths(self) -> list[str]:
        return [r.path for r in self.requests]

    def logins(self) -> list[Request]:
        return [r for r in self.requests if r.method == "POST" and r.path == "/login.awp"]

    def quizzes(self) -> list[Request]:
        return [r for r in self.requests if r.path == "/connexion/doubleauth.awp"]

    def connect(self, login: Login, device: Device | None) -> ecoledirecte.Client:
        return ecoledirecte.login(login, device, session=self)  # type: ignore[arg-type]

    # -- the session

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        parts = urlsplit(url)
        assert url.startswith(ecoledirecte.BASE), url
        path = parts.path.removeprefix("/v3")
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        query.update({k: str(v) for k, v in (kwargs.get("params") or {}).items()})
        data = kwargs.get("data")
        body = json.loads(data["data"]) if data else None
        request = Request(
            method,
            path,
            query,
            body,
            kwargs.get("headers") or {},
            kwargs.get("allow_redirects"),
            kwargs.get("timeout"),
        )
        self.requests.append(request)
        if self.fail is not None and (self.fail_on is None or path.endswith(self.fail_on)):
            raise self.fail
        if self.status is not None:
            return Response(self.status, b"{}")
        if self.raw is not None:
            return Response(200, self.raw)
        answer = self.handle(request)
        headers = {"x-token": f"token-{self.token}"} if self.token else {}
        return Response(200, json.dumps(answer).encode(), headers)

    # -- the server

    def handle(self, request: Request) -> dict[str, Any]:
        if request.path == "/login.awp" and request.method == "GET":
            self.cookies["GTK"] = "gtk-fixture"
            return {"code": 200}
        assert request.headers.get("X-GTK") == "gtk-fixture"
        if request.path == "/login.awp":
            return self.login(request)
        if request.headers.get("X-Token") != f"token-{self.token}":
            return {"code": 520, "message": "token invalide"}
        if request.path == "/connexion/doubleauth.awp":
            return self.quiz(request)
        if self.read_code != 200:
            return {"code": self.read_code, "message": "secret body"}
        return self.read(request)

    def issue(self) -> dict[str, str]:
        self.issued += 1
        pair = (f"cn-{self.issued}", f"cv-{self.issued}")
        self.trusted = {pair}
        return {"cn": pair[0], "cv": pair[1]}

    def login(self, request: Request) -> dict[str, Any]:
        assert request.body is not None
        assert request.query["v"] == ecoledirecte.API_VERSION
        user = unquote(request.body["identifiant"])
        password = unquote(request.body["motdepasse"])
        if self.login_answer is not None:
            return self.login_answer
        if (user, password) != (LOGIN.username, LOGIN.password):
            return {"code": 505, "message": "Identifiant et/ou mot de passe invalide !"}
        presented = {(f["cn"], f["cv"]) for f in request.body.get("fa", [])}
        if self.quiz_needed and not presented & self.trusted:
            self.token += 1
            return {"code": 250, "token": f"token-{self.token}", "data": {}}
        self.token += 1
        data: dict[str, Any] = {"accounts": [self.account()]}
        if self.rotate:
            data["fa"] = [self.issue()]
        return {"code": 200, "token": f"token-{self.token}", "data": data}

    def account(self) -> dict[str, Any]:
        return {
            "id": 9001,
            "typeCompte": "1",
            "nom": "LEROY",
            "prenom": "Parent",
            "profile": {
                "eleves": [
                    {"id": 7001, "nom": "LEROY", "prenom": "Zoé"},
                    {"id": 7002, "nom": "LEROY", "prenom": "Mathis"},
                ]
            },
        }

    def quiz(self, request: Request) -> dict[str, Any]:
        if request.query["verbe"] == "get":
            return {
                "code": 200,
                "data": {"question": b64(QUESTION), "propositions": [b64(c) for c in CHOICES]},
            }
        assert request.body is not None
        if request.body["choix"] != b64(RIGHT):
            return {"code": 505, "message": "Réponse incorrecte"}
        return {"code": 200, "data": self.issue()}

    def read(self, request: Request) -> dict[str, Any]:
        assert request.query["verbe"] == "get"
        path = request.path
        if path.endswith("/cahierdetexte.awp"):
            return {
                "code": 200,
                "data": {
                    "2026-11-10": [{"matiere": "MATHS", "aFaire": True, "effectue": True}],
                    "2026-11-12": [{"matiere": "FRANCAIS", "aFaire": True, "effectue": False}],
                    "2026-12-30": [{"matiere": "HIST", "aFaire": True, "effectue": False}],
                },
            }
        if "/cahierdetexte/" in path:
            day = path.rsplit("/", 1)[1].removesuffix(".awp")
            kid = "Zoé" if "/7001/" in path else "Mathis"
            if day == "2026-11-10":
                subject = {
                    "matiere": "MATHS",
                    "nomProf": "M. DUPONT",
                    "aFaire": {"contenu": b64("<p>Exercices 4 et 5</p>"), "effectue": True},
                }
            else:
                subject = {
                    "matiere": "FRANCAIS",
                    "nomProf": "Mme LEFEVRE",
                    "aFaire": {
                        "contenu": b64(f"<p>Réciter la poésie,<br>{kid} la présentera</p>"),
                        "effectue": False,
                        "documents": [{"id": 1}],
                    },
                }
            return {"code": 200, "data": {"date": day, "matieres": [subject]}}
        if path.endswith("/notes.awp"):
            return {
                "code": 200,
                "data": {
                    "periodes": [
                        {
                            "codePeriode": "A001",
                            "periode": "Trimestre 1",
                            "dateDebut": "2026-09-01",
                            "dateFin": "2026-12-01",
                        },
                        {
                            "codePeriode": "A002",
                            "periode": "Trimestre 2",
                            "dateDebut": "2026-12-02",
                            "dateFin": "2027-03-01",
                        },
                    ],
                    "notes": [
                        {
                            "codePeriode": "A001",
                            "libelleMatiere": "Maths",
                            "valeur": "14,5",
                            "noteSur": "20",
                            "date": "2026-11-05",
                            "commentaire": "Bravo Zoe",
                        },
                        {
                            "codePeriode": "A001",
                            "libelleMatiere": "Anglais",
                            "valeur": "Abs",
                            "noteSur": "10",
                            "date": "2026-10-02",
                        },
                        {
                            "codePeriode": "A002",
                            "libelleMatiere": "Sport",
                            "valeur": "17",
                            "noteSur": "20",
                            "date": "2027-01-10",
                        },
                    ],
                },
            }
        assert path.endswith("/messages.awp")
        return {
            "code": 200,
            "data": {
                "messages": {
                    "received": [
                        {
                            "read": False,
                            "subject": "Autorisation de sortie",
                            "date": "2026-11-08 09:00:00",
                        },
                        {"read": True, "subject": "Déjà lu", "date": "2026-11-08 08:00:00"},
                        {"read": False, "subject": "Trop ancien", "date": "2026-01-02 08:00:00"},
                    ]
                }
            },
        }

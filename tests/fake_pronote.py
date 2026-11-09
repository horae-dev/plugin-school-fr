# SPDX-License-Identifier: Apache-2.0
"""A fake PRONOTE client for the tests: no network, no pronotepy objects, synthetic names only.

It has the read surface of `pronotepy.ParentClient` the source uses, and the write methods the
source must never call (they record themselves in `Fake.writes`)."""

from datetime import date, datetime
from typing import Any

from horae_school_fr.credentials import Credentials

URL = "https://0000000a.index-education.net/pronote/mobile.parent.html?login=true"
FIRST = Credentials(URL, "user-fixture", "token-1", "uuid-fixture")


class Obj:
    id: str
    name: str
    grades: tuple["Obj", ...]

    def __init__(self, fake: "Fake", **attributes: Any) -> None:
        self._fake = fake
        self.__dict__.update(attributes)

    def set_done(self, status: bool) -> None:
        self._fake.writes.append("set_done")

    def mark_as_read(self, status: bool) -> None:
        self._fake.writes.append("mark_as_read")


class Fake:
    """The server side: what the client returns, and what was done to it."""

    def __init__(self) -> None:
        self.connects: list[str] = []
        """The token each login was given."""
        self.writes: list[str] = []
        self.selected: list[str] = []
        self.token = 1
        self.fail: BaseException | None = None
        """Raised by `connect`."""
        self.fail_read: BaseException | None = None
        self.rotate_on_read = False
        self.periods = ("Trimestre 1", "Trimestre 2")

    def connect(self, credentials: Credentials) -> "Client":
        self.connects.append(credentials.password)
        if self.fail is not None:
            raise self.fail
        self.token += 1
        return Client(self)


class Client:
    def __init__(self, fake: Fake) -> None:
        self.fake = fake
        self.password = f"token-{fake.token}"
        self.children = (
            Obj(fake, id="raw-id-1", name="LEROY Zoé"),
            Obj(fake, id="raw-id-2", name="LEROY Mathis"),
        )
        self.periods = tuple(Obj(fake, id=f"p{i}", name=n) for i, n in enumerate(fake.periods))
        self.current_period = self.periods[0]
        for period in self.periods:
            period.grades = (
                Obj(
                    fake,
                    grade="14,5",
                    out_of="20",
                    date=date(2026, 11, 5),
                    subject=Obj(fake, name="Maths"),
                    comment="Bravo Zoe",
                ),
                Obj(
                    fake,
                    grade="Abs",
                    out_of="10",
                    date=date(2026, 10, 2),
                    subject=Obj(fake, name="Anglais"),
                    comment=None,
                ),
            )
        self.current: Any = None

    def export_credentials(self) -> dict[str, Any]:
        return {
            "pronote_url": URL,
            "username": "user-fixture",
            "password": self.password,
            "client_identifier": "client-fixture",
            "uuid": "uuid-fixture",
        }

    def set_child(self, child: Any) -> None:
        assert isinstance(child, Obj), "set_child gets the raw child, not the proxy"
        self.current = child
        self.fake.selected.append(child.id)

    def homework(self, date_from: date, date_to: date | None = None) -> list[Obj]:
        if self.fake.fail_read is not None:
            raise self.fake.fail_read
        if self.fake.rotate_on_read:
            self.password = "token-rotated-during-read"
        assert date_to is not None
        assert date_from < date_to
        return [
            Obj(
                self.fake,
                subject=Obj(self.fake, name="Français"),
                description=f"Réciter la poésie, {self.current.name.split()[1]} la présentera",
                done=False,
                date=date(2026, 11, 12),
                files=[object()],
            ),
            Obj(
                self.fake,
                subject=Obj(self.fake, name="Maths"),
                description="Exercices 4 et 5",
                done=True,
                date=date(2026, 11, 10),
                files=[],
            ),
        ]

    def information_and_surveys(
        self,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        only_unread: bool = False,
    ) -> list[Obj]:
        assert only_unread
        assert date_from is not None
        return [
            Obj(
                self.fake,
                title="Autorisation de sortie",
                survey=False,
                read=False,
                start_date=datetime(2026, 11, 8, 9, 0),
            )
        ]

    # --- what must never be called -----------------------------------------------------------

    def post(self, *args: Any, **kwargs: Any) -> None:
        self.fake.writes.append("post")

    def new_discussion(self, *args: Any, **kwargs: Any) -> None:
        self.fake.writes.append("new_discussion")

    def refresh(self) -> None:
        self.fake.writes.append("refresh")

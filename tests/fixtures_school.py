# SPDX-License-Identifier: Apache-2.0
"""Synthetic school items shared by the normalizer and de-identification tests.

Every name here is invented. `Zoé` has an accent on purpose: parents often type `Zoe`."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from horae_school_fr import Child, Grade, Homework, Signature

PARIS = ZoneInfo("Europe/Paris")
ACCOUNT = "famille"
CHILDREN = [Child("c1", "Zoé"), Child("c2", "Mathis")]
TEACHER = "Mme Lefèvre"

HOMEWORK = [
    Homework(
        "c1",
        "Français",
        "Réciter la poésie, Zoe la présentera mardi. Voir avec ZOÉ et Lefevre.",
        date(2026, 11, 12),
        teacher=TEACHER,
    ),
    Homework("c1", "Maths", "Exercices 4 et 5 p. 78", date(2026, 11, 10), done=True),
    Homework(
        "c2",
        "Histoire",
        "Fiche sur Mathis le Grand",
        datetime(2026, 11, 12, 23, 30, tzinfo=ZoneInfo("UTC")),
    ),
]
GRADES = [
    Grade("c1", "Maths", 14.5, 20, date(2026, 11, 5), "Bravo Zoé, continue", teacher=TEACHER),
    Grade("c2", "Anglais", "Abs", 20, date(2026, 11, 6)),
]
SIGNATURES = [
    Signature("c1", "Sortie de zoe au musée", date(2026, 11, 14), "autorisation"),
    Signature("c2", "Carnet de correspondance", date(2026, 11, 9), "information"),
]

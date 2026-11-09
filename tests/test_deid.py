# SPDX-License-Identifier: Apache-2.0
"""Acceptance (story #4): child and teacher names never reach a cloud backend in clear.

The test runs this source's output through the core: the engine, the instance's de-identification
policy and `ModelBackends`, with a provider that records the exact bytes it is asked to send. The
protection comes from the `personal_fields` the plugin declares in its manifest (schemas decision
68, here the ones of `plugin.yaml`), which the core enforces whatever the skill declares; the
skill's own `input_view` (here `horae_school_fr.input_view()`, written to a skill folder and
loaded by the core's own loader, schemas decision 47) is guidance and adds the free-text classes."""

import asyncio
import json
import unicodedata
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from horae.contracts import Pipeline, RunRecord, Usage
from horae.contracts.instance import ModelAlias
from horae.engine import execute, make_context
from horae.engine.canonical import canonical_json
from horae.engine.ports import LlmBackendName
from horae.engine.testing import FixturePlugins
from horae.llm import UNVALIDATED, Completion, CompletionResult, ModelBackends
from horae.llm.deid import DEID_NOTE, Deidentifier, field_classes, pseudonymize
from horae.secrets import SecretResolver
from horae.skills import DirectorySkills, load_skill, skill_dir

from fixtures_school import (
    ACCOUNT,
    CHILDREN,
    GRADES,
    HOMEWORK,
    PARIS,
    SIGNATURES,
)
from horae_school_fr import DEIDENTIFIED_FIELDS, input_view, manifest, normalize

SKILL = "school-fr/brief"
START = datetime(2026, 11, 9, 5, 30, tzinfo=UTC)
CLOUD = {"backend": "anthropic", "model": "fixture-model", "secret": "ANTHROPIC_API_KEY"}
LOCAL = {"backend": "ollama", "model": "fixture-model", "base_url": "http://ollama:11434"}
NAMES = ("Zoé", "Mathis", "Lefèvre")
"""Every name of the fixtures (children and the teacher's surname)."""

OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["summary"],
    "properties": {"summary": {"type": "string", "maxLength": 500}},
}


def fold(text: str) -> str:
    """Case- and accent-folded: what a reader would call the same name."""
    decomposed = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text).casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def pack_dir(root: Path, view: Any = None) -> Path:
    """A plugin folder with a skill whose input view is the source's declaration."""
    folder = skill_dir(root, SKILL)
    folder.mkdir(parents=True)
    (folder / "skill.yaml").write_text(
        yaml.safe_dump(
            {
                "name": SKILL,
                "version": "0.1.0",
                "description": "Summarize the school items (test fixture).",
                "input_view": input_view() if view is None else view,
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (folder / "prompt.md").write_text("Summarize the school items in one sentence.\n")
    (folder / "output.schema.json").write_text(json.dumps(OUTPUT_SCHEMA))
    return root


class Provider:
    """A provider backend that records the bodies it is asked to send."""

    may_write_cache = False

    def __init__(self, name: LlmBackendName, output: Any) -> None:
        self.name: LlmBackendName = name
        self.output = output
        self.sent: list[bytes] = []

    def render(self, completion: Completion) -> dict[str, Any]:
        return {"model": completion.model, "system": completion.system, "input": completion.input}

    async def send(self, payload: Mapping[str, Any], timeout_s: float) -> CompletionResult:
        self.sent.append(canonical_json(dict(payload)))
        return CompletionResult(self.output, Usage(tokens_in=10, tokens_out=5, usd=0.0))

    async def aclose(self) -> None:
        return None


REF = "school-fr.pronote"


def run(
    tmp_path: Path,
    spec: Mapping[str, Any],
    answer: Any,
    view: Any = None,
    ref: str = REF,
    declare: bool = True,
) -> tuple[Provider, RunRecord]:
    """A pipeline `collect` (this source's output) then `llm` with the skill, through the engine
    and `ModelBackends`, the instance's policy being built as `horae run` builds it."""
    source = normalize(
        ACCOUNT, CHILDREN, homework=HOMEWORK, grades=GRADES, signatures=SIGNATURES, tz=PARIS
    )
    payload = source.data
    plugins = FixturePlugins()
    provides: dict[str, Any] = {"sources": [ref], "skills": [SKILL]}
    if declare:  # what `plugin.yaml` says, as the manifest the host loads
        provides["personal_fields"] = {ref: manifest()["provides"]["personal_fields"][ref]}
    plugins.add(
        {
            "name": "school-fr",
            "version": "0.4.1",
            "horae": ">=0",
            "provides": provides,
            "permissions": {},
        },
        {ref: lambda params, input: payload},
        commit="a" * 40,
    )
    provider = Provider(spec["backend"], answer)
    alias = ModelAlias.model_validate(spec)
    backends = ModelBackends(
        {"default": alias},
        SecretResolver({}, tmp_path),
        {provider.name: lambda alias, spec, secrets: provider},
        validate=UNVALIDATED,
    )
    pipeline = Pipeline.model_validate(
        {
            "name": "school",
            "steps": [
                {"collect": [ref]},
                {
                    "llm": {
                        "skill": SKILL,
                        "model": "default",
                        "budget": {"usd": 0.05, "tokens_out": 2000},
                    }
                },
            ],
        }
    )
    record = asyncio.run(
        execute(
            pipeline,
            run_id="20261109T053000Z-school-01",
            context=make_context(START, "Europe/Paris"),
            started_at=START,
            plugins=plugins,
            tools=plugins,
            llm=backends,
            skills=DirectorySkills({"school-fr": pack_dir(tmp_path / "school-fr", view)}),
            deid=Deidentifier({"default": alias}),
        )
    )
    RunRecord.model_validate_json(record.model_dump_json())
    return provider, record


def assert_no_name(text: str) -> None:
    """No child or teacher name in `text`, in any case, accent form or JSON escaping."""
    for form in (text, json.dumps(json.loads(text), ensure_ascii=False)):
        folded = fold(form)
        for name in NAMES:
            assert fold(name) not in folded, name
        for name in NAMES:
            assert name not in form
            assert unicodedata.normalize("NFD", name) not in form


def test_names_never_reach_a_cloud_backend_in_clear(tmp_path: Path) -> None:
    provider, record = run(tmp_path, CLOUD, {"summary": "Devoirs pour [PERSON_1] et [PERSON_2]."})
    assert record.status == "ok", record.steps[-1].error
    [body] = provider.sent
    assert_no_name(body.decode())
    payload = json.loads(body)
    assert payload["system"].endswith(DEID_NOTE)
    sent = json.dumps(payload["input"], ensure_ascii=False)
    # The declared fields are pseudonyms, and so are the names the parents typed in free text,
    # in every case and accent form (a description, a comment, a signature title).
    assert "[PERSON_" in sent
    assert payload["input"][0]["data"]["children"][0]["first_name"].startswith("[PERSON_")
    assert payload["input"][0]["data"]["homework"][0]["teacher"].startswith("[PERSON_")
    [call] = record.llm
    assert_no_name(json.dumps(call.request, ensure_ascii=False))
    assert_no_name(json.dumps(call.response, ensure_ascii=False))
    # The data the model did not see is untouched, and the answer is restored afterwards.
    assert "[PERSON_" in call.response["summary"]
    restored = record.steps[-1].output["summary"]
    assert "Zoé" in restored
    assert "[PERSON_" not in restored


def test_a_name_inside_free_text_in_every_form_is_covered(tmp_path: Path) -> None:
    provider, _ = run(tmp_path, CLOUD, {"summary": "ok"})
    text = json.dumps(json.loads(provider.sent[0])["input"], ensure_ascii=False)
    for plain in ("la poésie", "Exercices 4 et 5", "Fiche sur", "continue", "musée"):
        assert plain in text  # the rest of the text is sent as it was
    assert_no_name(provider.sent[0].decode())


def test_the_declared_fields_cover_every_name_the_source_outputs() -> None:
    view = field_classes({k: v for k, v in input_view().items() if v is not None})
    people = {path for path, name in view.items() if name == "person"}
    assert people == {"[].data." + path for path in DEIDENTIFIED_FIELDS}
    data = normalize(
        ACCOUNT, CHILDREN, homework=HOMEWORK, grades=GRADES, signatures=SIGNATURES, tz=PARIS
    ).data
    assert set(_leaf_paths(data)) <= {path.removeprefix("[].data.") for path in input_view()}
    # every name in the output sits under a declared path or in a scrubbed free-text field
    named = {p for p, v in _leaves(data) if any(fold(n) in fold(str(v)) for n in NAMES)}
    assert named - set(DEIDENTIFIED_FIELDS) <= {
        "homework[].description",
        "grades[].comment",
        "signatures[].title",
    }


def _leaves(value: Any, path: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        return [x for k, v in value.items() for x in _leaves(v, f"{path}.{k}" if path else k)]
    if isinstance(value, list):
        return [x for v in value for x in _leaves(v, f"{path}[]")]
    return [(path, value)]


def _leaf_paths(value: Any) -> list[str]:
    return [path for path, _ in _leaves(value)]


def test_the_skill_declaration_is_valid_for_the_core(tmp_path: Path) -> None:
    root = pack_dir(tmp_path / "school-fr")
    skill = load_skill(root, SKILL, allowed_intents=[])
    classes = field_classes(skill.view_options)
    assert classes["[].data.children[].first_name"] == "person"
    assert classes["[].data.homework[].description"] == "free_text"


def test_a_local_model_is_sent_the_names_unchanged(tmp_path: Path) -> None:
    """The control: the protection comes from the declaration and a cloud alias."""
    provider, record = run(tmp_path, LOCAL, {"summary": "ok"})
    assert record.status == "ok", record.steps[-1].error
    body = provider.sent[0].decode()
    assert "Zoé" in body
    assert DEID_NOTE not in body


@pytest.mark.parametrize("ref", ["school-fr.pronote", "school-fr.ecoledirecte"])
def test_a_skill_without_classes_sends_no_name_to_a_cloud_alias(tmp_path: Path, ref: str) -> None:
    """The plugin's own declaration (`personal_fields`, decision 68) protects the names: the
    skill's view is a plain list of paths, with no `class` at all."""
    paths = list(input_view())
    assert all(isinstance(path, str) for path in paths)
    provider, record = run(tmp_path, CLOUD, {"summary": "ok"}, view=paths, ref=ref)
    assert record.status == "ok", record.steps[-1].error
    [body] = provider.sent
    assert_no_name(body.decode())
    payload = json.loads(body)
    assert payload["system"].endswith(DEID_NOTE)
    assert payload["input"][0]["data"]["children"][0]["first_name"].startswith("[PERSON_")
    assert payload["input"][0]["data"]["homework"][0]["teacher"].startswith("[PERSON_")


def test_without_the_declaration_the_names_would_leak(tmp_path: Path) -> None:
    """The control for the control: with neither the manifest declaration nor a class in the
    skill, every name is sent, so the protection is the declaration and nothing else."""
    provider, record = run(
        tmp_path, CLOUD, {"summary": "ok"}, view=list(input_view()), declare=False
    )
    assert record.status == "ok", record.steps[-1].error
    body = provider.sent[0].decode()
    assert "Zoé" in body
    assert DEID_NOTE not in body


def test_both_sources_declare_the_fields_the_normalizer_outputs() -> None:
    declared = manifest()["provides"]["personal_fields"]
    assert declared == {
        "school-fr.pronote": list(DEIDENTIFIED_FIELDS),
        "school-fr.ecoledirecte": list(DEIDENTIFIED_FIELDS),
    }
    data = normalize(
        ACCOUNT, CHILDREN, homework=HOMEWORK, grades=GRADES, signatures=SIGNATURES, tz=PARIS
    ).data
    present = {path for path, _ in _leaves(data)}
    assert set(DEIDENTIFIED_FIELDS) <= present


def test_the_core_folds_accents_so_zoe_is_caught_when_zoe_with_accent_is_declared() -> None:
    """Was the documented core gap (decision 47 C); decision 68 A closes it. `NameScrubber`
    stays as extra protection."""
    view = {"who[]": {"class": "person"}}
    raw = {"who": ["Zoé"], "note": "Zoe, ZOE, ZOÉ and Zoé"}
    sent = pseudonymize(raw, view).data["note"]
    assert sent == "[PERSON_1], [PERSON_1], [PERSON_1] and [PERSON_1]"
    scrubbed = normalize(ACCOUNT, CHILDREN, homework=HOMEWORK, tz=PARIS).data
    assert "Zoe " not in scrubbed["homework"][0]["description"]

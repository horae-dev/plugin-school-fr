# SPDX-License-Identifier: Apache-2.0
"""`plugin.yaml` is valid for the SDK's manifest helpers and matches the plugin's code."""

from pathlib import Path
from typing import Any

import pytest
import yaml
from horae_sdk import Permissions, dump_manifest
from horae_sdk import manifest as build_manifest

from horae_school_fr import manifest
from horae_school_fr.__main__ import HEADER, main

PLUGIN_YAML = Path(__file__).parent.parent / "plugin.yaml"


def on_disk() -> dict[str, Any]:
    document = yaml.safe_load(PLUGIN_YAML.read_text("utf-8"))
    assert isinstance(document, dict)
    return document


def test_plugin_yaml_passes_the_sdk_checks() -> None:
    """Rebuilt from the file alone, so a hand edit is checked too (`ManifestError`)."""
    document = on_disk()
    paths = document["permissions"].get("paths", {})
    permissions = Permissions(
        intents=document["permissions"].get("intents", []),
        network=document["permissions"].get("network", []),
        network_local=document["permissions"].get("network_local", []),
        secrets=document["permissions"].get("secrets", []),
        optional_secrets=document["permissions"].get("optional_secrets", []),
        read=paths.get("read", []),
        write=paths.get("write", []),
    )
    rebuilt = build_manifest(
        name=document["name"],
        version=document["version"],
        provides=document["provides"],
        permissions=permissions,
        command=document["runtime"]["command"],
        horae=document["horae"],
        description=document.get("description"),
        license=document.get("license"),
        homepage=document.get("homepage"),
    )
    assert rebuilt == document


def test_plugin_yaml_is_what_the_code_declares() -> None:
    """`provides` and `permissions` follow the registered tools and `PERMISSIONS`: regenerate
    with `uv run python -m horae_school_fr --manifest > plugin.yaml` after changing them."""
    assert on_disk() == manifest()
    assert PLUGIN_YAML.read_text("utf-8") == HEADER + dump_manifest(manifest())


def test_main_prints_the_manifest(capsys: pytest.CaptureFixture[str]) -> None:
    main(["--manifest"])
    assert capsys.readouterr().out == PLUGIN_YAML.read_text("utf-8")


def test_main_refuses_unknown_arguments() -> None:
    with pytest.raises(SystemExit, match="usage"):
        main(["--nope"])

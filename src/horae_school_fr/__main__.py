# SPDX-License-Identifier: Apache-2.0
"""`python -m horae_school_fr` serves the plugin over stdio; `--manifest` prints `plugin.yaml`."""

import sys

from horae_sdk import dump_manifest

from horae_school_fr import pronote
from horae_school_fr.plugin import build, manifest

HEADER = """\
# Written by `uv run python -m horae_school_fr --manifest > plugin.yaml`; tests/test_manifest.py
# checks it is what the SDK writes. When installing, pin runtime.command to an absolute
# interpreter that has this package (horae-sdk README, "The command").
"""


def main(argv: list[str]) -> None:
    if argv == ["--manifest"]:
        sys.stdout.write(HEADER + dump_manifest(manifest()))
        return
    if argv[:1] == ["pronote-setup"]:
        sys.exit(pronote.setup(argv[1:]))
    if argv:
        sys.exit("usage: python -m horae_school_fr [--manifest | pronote-setup ...]")
    # Never print while serving: stdout is the protocol channel.
    build().run()


if __name__ == "__main__":
    main(sys.argv[1:])

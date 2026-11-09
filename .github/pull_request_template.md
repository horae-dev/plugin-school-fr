Closes #<issue>

## Summary

<!-- What changed and why, in a few lines. -->

## Acceptance criteria

<!-- Copy them from the issue; each maps to at least one test. -->
- [ ]

## Definition of done (see AGENTS.md)

- [ ] CI green: the **CI OK** check (lockfile, lint, types, tests + coverage, gitleaks)
- [ ] `uv run pre-commit run --all-files` passes locally
- [ ] `plugin.yaml` regenerated (`uv run python -m horae_school_fr --manifest > plugin.yaml`) if tools or permissions changed
- [ ] Recordings in `tests/recordings/` are synthetic (no personal data, no secret) and their diff reviewed
- [ ] Commits carry `Graft-Agent`, `Graft-Model` and `Graft-Task` trailers

## Plugin authoring checklist

<!-- Tick what applies. The link below is relative to the pull request page, so it also works in repositories made from this template. -->
- [ ] Permissions: least privilege in `plugin.yaml`; every host, secret, path and intent justified below
- [ ] Secrets: resolved from the declared environment, never logged or put in errors, intents or recordings
- [ ] Untrusted input: titles only through `intents.task_create`, no free-form URLs in intents, sizes bounded
- [ ] Fixtures: synthetic only, recorded locally (never in CI), with a hostile case
- [ ] Degraded handling: expected failures raise `PluginError`, no failure hidden as empty data
- [ ] Docs: README sections and permissions table, changelog
- [ ] Release (if releasing): version bumped, `plugin.yaml` and `uv.lock` current

Checklist: `docs/authoring-checklist.md` ([link](../blob/HEAD/docs/authoring-checklist.md))

## Declared additions

<!-- Write "none" or list each with a justification (they appear in plugin.yaml permissions). -->
- Network hosts:
- Secrets:
- Paths:
- Intents:
- LLM calls:

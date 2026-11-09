# Plugin authoring checklist

Run through this before you open a pull request, and again before a release. It points to the
sources instead of repeating them; when they disagree with this page, they win. The pull
request template links here.

Sources: the core's [security model](https://github.com/horae-dev/horae/blob/main/docs/security.md)
(the threat rows T01, T02, T03, T04, T05, T06, T07, T08, T09), the
[plan](https://github.com/horae-dev/horae/blob/main/docs/PLAN.md) (sections 3.3 and 4), the
[SDK README](https://github.com/horae-dev/horae/blob/main/sdk/README.md) and the
[manifest schema](https://github.com/horae-dev/horae/blob/main/schemas/plugin-manifest/v1.json).

## Permissions

A plugin gets only what `plugin.yaml` declares, approved when it is installed. Undeclared
access is a bug, and from milestone M5 the isolated tier blocks undeclared network and
filesystem access (T04, T05, PLAN §3.3). Until then the core checks the intents and injects only the declared secrets.

- [ ] `PERMISSIONS` in `plugin.py` lists the least you need, and `plugin.yaml` was
  regenerated from it (`uv run python -m horae_school_fr --manifest > plugin.yaml`).
- [ ] **Intents**: only the types the plugin proposes, from the closed set `note.create`,
  `note.append_section`, `note.replace_owned_block`, `task.create`, `task.update_status`,
  `notify.push`, `http.post`. A pure source declares none. The core rejects the whole step
  for an undeclared intent.
- [ ] **Network**: one entry per host the plugin connects to, justified in the PR ("Declared
  additions"). A host is a lowercase DNS name, with no port, scheme or wildcard; a single-label
  name (a Docker service name) is allowed; `localhost` and IP literals are refused (manifest
  schema, schemas README). From M5 the egress proxy refuses names that resolve to private or
  link-local addresses (T07, planned #50), so a service on your LAN will be blocked later. An
  `http.post` URL is `https` only, with no user info and no fragment, and redirects are never
  followed.
- [ ] **Secrets**: one entry per environment variable the plugin reads, justified. The host
  injects only these, in a clean environment. Names starting with `HORAE_`, `LD_`, `DYLD_` or
  `PYTHON`, `PATH` and `HOME` are refused.
- [ ] **Paths**: absolute, normalised, `read` and `write` apart; `write` only where the plugin
  must. A sink writes a note intent exactly as the core computed it, never elsewhere.
- [ ] No permission is declared "just in case", and nothing is used that is not declared
  (no extra host, no environment variable read outside `secrets`, no LLM call).

## Secrets

- [ ] Read a secret from its declared environment variable, once, at call time. Never put one
  in the manifest, in `params`, in a fixture, in source or in the repository.
- [ ] Never log, print or put a secret (or a header, URL or response that holds it) in an
  error message, an intent, returned data or a recording. The host redacts what it knows, but
  that is the last line of defence (T09), not a licence. Stdout is the protocol channel: never
  `print`.
- [ ] Errors you raise name the secret, never its value.
- [ ] A secret shorter than 8 bytes is refused by the core when it is resolved; use real
  credentials in the instance, fake ones only in tests.
- [ ] `uv run pre-commit run --all-files` passes (gitleaks), and CI's gitleaks check is green.

## Untrusted input

Everything that comes from a service (a subject, a note, an event title, an API field) is
untrusted data, including a transform's `call.input`.

- [ ] A task title goes through `intents.task_create` and nothing else. It escapes Obsidian
  Tasks signifiers, Markdown and bare links (T02, schemas decision 50); do not pass
  `escape=False` for a title that reaches a vault.
- [ ] Other text that reaches a note or a notification is data in a field the core checks; it
  never chooses an action, an intent type, a recipient or a path.
- [ ] No free-form URL in an intent: an `http.post` URL or a notification link comes from
  configuration or a fixed template, never from fetched content (T05, T06). Declare the host.
- [ ] A path to read or write comes from the trusted `call.paths` configuration and is checked
  with `call.paths.allowed(path)` for reading and `call.paths.allowed(path, write=True)` for
  writing, never from untrusted data. The default (`write=False`) accepts a `read` or a `write`
  entry, and a `write` entry implies read. It returns the resolved real path, or `None`.
- [ ] Use `intents.fingerprint(...)` for stable identities; do not build them by hand from raw
  text. Build them from the service's stable id (never a title, timestamp, index or random
  value). `task.update_status` only accepts a fingerprint present in the collected input (T01,
  schemas decision 30).
- [ ] Be deterministic: two runs on the same input must give identical data and intents in the
  same order (no `now()`, no random, no unordered iteration). Replay and the reuse of an
  unchanged input depend on it.
- [ ] Bound the work: cap the number of items, the size of each field and of the whole result.
  The SDK refuses more than 256 intents and a message over 8 MiB, and the core bounds stderr,
  intent payloads and the run time (T08). A message over 8 MiB kills the process
  (`output_too_large`); in a `collect` with other sources that is only a degraded step. More
  than 256 intents fails the step.
- [ ] One malformed or hostile item is skipped or neutralised, not allowed to abort the whole
  call.

## Recorded fixtures

- [ ] `tests/fixtures/` and `tests/recordings/` hold **synthetic data only**: no real message,
  name, address, hostname, token or account. Use reserved domains (`example.org`).
- [ ] The service client is a fake in tests; no test touches the network.
- [ ] Every tool has a recording for the normal case, the empty case and a **hostile** case:
  for each untrusted field, Tasks signifiers (a date, a recurrence), Markdown, wikilinks, a
  URL and control characters. The recording shows them neutralised.
- [ ] A failing case is recorded as `{"error": "..."}` when the plugin should raise.
- [ ] Recordings are rewritten **locally** with `HORAE_SDK_RECORD=1 uv run pytest` and the
  diff is reviewed before committing. Record mode is refused when `CI` or `GITHUB_ACTIONS` is
  set, so CI only replays what you committed (schemas decision 50).
- [ ] By the template's convention, fingerprints start with the plugin name; re-record after any rename.
- [ ] `uv run pytest --cov` passes the 85% coverage gate.

## Degraded handling

The core, not the plugin, marks a step degraded. What the plugin controls is what it raises
and when (`horae.engine.executor`, step policy table).

- [ ] Raise `PluginError("message")` for an expected failure (service down, bad credentials,
  rate limit, malformed answer). The host reports it as a `tool_error`. Any other exception
  is logged to stderr and reported by type only. Do not `sys.exit`, hang or crash the process.
- [ ] A `PluginError` message is recorded verbatim (only redacted) in the step error and the run
  record. Keep it short and fixed; never paste an upstream body, a URL with a query or fetched
  text.
- [ ] Do not hide a failure behind an empty result: an empty `data` is a successful
  result: the step is `ok`, nothing is flagged, and downstream steps run on nothing. (`noop`
  only comes from a reused LLM output with no intents.) Raise instead, so the failure is
  recorded.
- [ ] Know what happens next. A `collect` step that calls several sources: if some raise and
  at least one succeeds, the step and the run are `degraded` (error kind `source_failed`), the
  pipeline continues on the partial data, every `act` step is skipped (`partial_input`) so
  nothing is written from partial data, and `notify` still runs. If every source fails, the
  step and the run are `failed`. A failing `transform` fails the run.
- [ ] Make each tool one unit that either returns complete data or raises. Do not return a
  half-filled result as if it were complete: the core cannot tell.
- [ ] Set timeouts on every outgoing request and retry a bounded number of times. Never loop
  forever and never retry in a tight loop against a service with rate limits. Past
  `runs.timeout_s` (default 900 s, T08) the run is cancelled and marked failed (`timeout`). What
  kills the plugin process is the per-call `plugin_defaults.call_timeout_s` (default 30 s;
  startup 10 s). After a timeout, a crash or an oversized message the host restarts the plugin
  once per run, then reports `plugin_unavailable`.
- [ ] Proposing an intent that is undeclared, invalid or over the caps is not degradation: the
  core fails the run. Keep intents valid (the SDK checks the cheap rules before sending).
- [ ] Never swallow `PluginError` in a helper and carry on with stale or default data.

## Docs

- [ ] `horae:` in the manifest is a real compatibility range (for example `>=0.1,<0.2`), not
  the template's `>=0`. Add `homepage` (an `https` URL) if you have one.
- [ ] README title and description are the plugin's own (`scripts/rename.py` does this); no
  template text is left.
- [ ] README has: what the plugin does, its tools and parameters, how to configure and install
  it (the absolute `runtime.command`), and what it needs.
- [ ] A **permissions table** in the README: each intent, network host, secret and path with
  the reason, matching `plugin.yaml` exactly.
- [ ] Failure behaviour is documented: what raises, what the user sees when the service is
  down, what is rate limited.
- [ ] A changelog (a `CHANGELOG.md` or the release notes) records every change to tools,
  parameters and permissions. A new permission is called out, since users must approve it.
- [ ] Examples, docstrings and `AGENTS.md` still match the behaviour.

## Release

- [ ] `VERSION` in `plugin.py` is bumped, `plugin.yaml` regenerated and `pyproject.toml`
  agrees (semantic versioning: new permissions or a changed tool contract are not a patch).
- [ ] Changing the licence means changing `LICENSE`, the `license` of `pyproject.toml`, the
  `manifest(license=...)` call in `plugin.py` and regenerating `plugin.yaml`, together
  (`scripts/rename.py` skips `LICENSE`).
- [ ] Until M5, `horae plugin add` installs only `tier: first-party` entries while the runtime
  is `subprocess` (T04, PLAN §3.3), so a third-party plugin made from this template is refused
  until M5.
- [ ] `uv.lock` is current: `UV_LOCKED=1 uv lock --check` passes.
- [ ] The CI checks are green: `uv run ruff check && uv run ruff format --check`, `uv run
  mypy`, `uv run pytest --cov`, gitleaks. **CI OK** is the aggregate.
- [ ] Renovate is enabled on the repository, and its pull requests are reviewed like any change
  (a `horae-sdk` bump through lock file maintenance moves the pinned SDK commit).
- [ ] Tag the commit, and give installers the commit to pin (`horae plugin add https://github.com/<owner>/<repo>@<sha>`).

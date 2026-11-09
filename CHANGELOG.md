# Changelog

## 0.2.0

Normalization and de-identification (story #4).

- `horae_school_fr.normalize`: a library for the PRONOTE and ÉcoleDirecte sources. A small model
  (`Child`, `Homework`, `Grade`, `Signature`) becomes the source data and `task.create` intents:
  undone homework and signatures are tasks, notes and grades are data only. Fingerprints
  `school-fr:<account>:<kind>:<hash of stable fields>`, due dates in the instance time zone, titles
  escaped with the SDK and capped.
- De-identification: the core's contract is a skill's `input_view` classes (schemas decision 47).
  `input_view()` and `DEIDENTIFIED_FIELDS` declare child and teacher names as `class: person`, for
  the skills built on this source. `tests/test_deid.py` runs the output through the core (engine,
  de-identification policy, `ModelBackends`, a recording provider) and checks that no name reaches
  a cloud backend in clear.
- Defence in depth: `NameScrubber` spells known names in free text as declared, because the core's
  matcher is not accent-folded (`Zoe` is not `Zoé`).
- The core (`horae`) is a dev dependency, for the tests.
- **Permissions:** unchanged (none). No tool is added.

## 0.1.0

First release: the plugin scaffold and the lockout guard (story #5).

- `horae_school_fr.guard`: a persistent, process-safe poll and login budget per account that the
  PRONOTE and ÉcoleDirecte sources must go through. Defaults: 4 polls a day (1 to 12), 2 h between
  polls, optional local-time windows, 2 logins a day (counted apart; only when the cached session
  or token is gone). Exponential backoff after a failure (1 h doubling to 24 h). An
  authentication failure stops the account until a human deletes its state entry. A refusal makes
  no network call and reads `degraded: budget_exhausted | outside_window | backoff_until <time> |
  auth_stopped`.
- State: JSON at a declared `state_path` (checked with `call.paths.allowed(write=True)`), atomic
  replace, mode 0600, `fcntl` lock. A corrupt or unknown file fails closed (every account stopped).
- `school-fr.status`: a diagnostic source that reports the guard state without logging in.
- Requires `horae-sdk>=0.6,<0.7`.
- **Permissions:** none declared in `plugin.yaml`. Per instance, the lock approves the state
  file's directory as a `paths.write` entry.

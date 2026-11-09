# Changelog

## 0.3.0

The PRONOTE source (story #2).

- `school-fr.pronote`: read-only homework, grades and unread informations (to sign or acknowledge)
  of a parent's children, through **pronotepy 2.15.7** (MIT; unofficial, reverse-engineered,
  risk horae#73), pinned exactly and imported only by `horae_school_fr.pronote`. Everything goes
  through the lockout guard (`guard.begin(login=True)`: a plugin process has no cached session),
  one attempt per call, no retry (pronotepy's own re-login is disabled), a 30 s socket timeout.
- Rotating credentials: PRONOTE issues a new token at each login. They are saved in a credential
  store (`credentials.py`, a JSON file next to the guard state, atomic, mode 0600, `flock`) right
  after the login and before any read, and again if they changed. The first credentials come from
  the optional secret `PRONOTE_CREDENTIALS` or the store.
- `python -m horae_school_fr pronote-setup`: the one-time QR code login; prints the credentials
  (or `--store`s them).
- Failures: wrong or expired credentials, a second factor or PRONOTE's lockout message stop the
  account (`AuthFailure`); network errors, timeouts and API changes back off
  (`degraded: source_unavailable (...)`). Messages are fixed codes.
- Read-only by construction: a proxy answers only a read allow-list; a test checks every public
  name of pronotepy's classes against it.
- **Permissions (new, to approve):** intent `task.create`; optional secret `PRONOTE_CREDENTIALS`.
  No network host is declared (a manifest has no wildcard): approve the school's host in
  `horae.lock`; the source checks `call.host_allowed` before the login. pronotepy's HTTP client
  cannot be restricted, so M5 isolation is what enforces it on the wire.
- New dependency: `pronotepy==2.15.7` (with `requests`, `beautifulsoup4`, `pycryptodome`).

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

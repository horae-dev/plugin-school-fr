<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# plugin-school-fr

Horae plugin: read-only PRONOTE and ÉcoleDirecte homework, notes and deadlines as tasks.

Version 0.4.0 · requires `horae-sdk` 0.6 · Apache-2.0

> 🚧 **Early.** This release ships the lockout guard, the normalizer (with the de-identification of child names), the **PRONOTE source** ([#2](../../issues/2)) and the **ÉcoleDirecte source** ([#3](../../issues/3)). Both sources are unofficial and have only been tested against recorded or synthetic data.

## The tool

`school-fr.status` (a source) reports the guard state of the accounts in a state file. It never logs in, never touches the network, writes nothing, proposes no intent and declares no permission.

| Parameter | Default | Meaning |
|---|---|---|
| `state_path` | required | absolute path of the guard's JSON state file; its directory must be approved for writing (`paths.write`) in `horae.lock` |
| `accounts` | all in the file | up to 16 account labels |
| `max_polls_per_day`, `min_interval`, `windows`, `max_logins_per_day` | the defaults below | the limits to judge `would_refuse` against |

For each account it returns the polls and logins used today, the last poll, the earliest next poll, the backoff, the auth stop and `would_refuse` (the reason a poll would be refused right now, or `null`). A state file that cannot be trusted is reported as `state: invalid`.

## PRONOTE

`school-fr.pronote` (a source) reads a parent's PRONOTE account **read-only**, through the unofficial [pronotepy](https://github.com/bain3/pronotepy) library (MIT, pinned to an exact version). **That library is reverse-engineered and unofficial**: PRONOTE may change under it without notice, and repeated or failed logins can lock the account (risk [horae#73](https://github.com/horae-dev/horae/issues/73)). So the source goes through the [lockout guard](#limits) before every login, makes **one attempt per call, never retries**, and degrades instead.

### What it reads

For the chosen children (all by default), and nothing else:

| PRONOTE | Becomes |
|---|---|
| Homework due from today to `days_ahead` | `homework` (a task while not done) |
| Grades of the current period, or of the `periods` asked (the newest 200 per child) | `grades` (data only) |
| **Unread** informations and surveys (30 days; the list only, never their content, so nothing is marked read) | `signatures` (a task each) |

Informations are addressed to the parent, not to a child: they are listed once, under the first selected child. Teachers' names are not exposed by pronotepy. Children are published under an opaque id (a hash), with the platform's name in `children[].first_name`, which is declared for de-identification (see below). It never writes: no homework marked done, no information marked read, no message; a proxy refuses every client method that is not on a read allow-list, and a test checks it against pronotepy's own classes.

### Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `account` | required | the account label (also the key of its credentials) |
| `state_path` | required | the guard's state file, in a directory approved for writing (`paths.write`) |
| `credentials_path` | `pronote-credentials.json` next to `state_path` | the credential store (see below); must be approved for writing too |
| `max_polls_per_day`, `min_interval`, `windows` | 4, `2h`, none | the [limits](#limits) |
| `max_logins_per_day` | `max_polls_per_day` (at most 6) | see below |
| `children` | all | names (first name, surname or both, any case or accent) or ids to read |
| `periods` | the current period | names of the periods whose grades are read |
| `days_ahead` | 14 | homework due in the next 1 to 60 days |
| `hosts` | none | extra exact host names, **only** when the core sent no network list (see Network) |

**Polls and logins.** A plugin process lives for one run, so there is never a cached session: **every poll is one token login**, and a login happens at most once per poll. The login budget therefore caps the polls too. Keep `max_logins_per_day` at least `max_polls_per_day`, or the lower number wins. The day's budget is reserved before the login, so even a crash counts. Nothing is ever retried inside a call (pronotepy's own re-login after an error is switched off; it only repeats a harmless GET of the login page, up to three times, when it does not parse).

### First setup: the QR code

Do this once, on your own machine, with the plugin's Python environment.

1. In PRONOTE (the website, parent account): *Mon compte → Application mobile → Générer un QR code*. Choose a **4-digit code** and keep the QR code on screen. It is valid for **10 minutes**; the source also needs the QR code's content, so save it as JSON (the QR code reader of your phone shows it, or scan it with any QR tool).
2. Run, with the QR JSON in a file (or on stdin with `--qr -`):

   ```sh
   python3 -m horae_school_fr pronote-setup --account famille --qr qr.json
   ```

   It asks for the PIN (or reads `PRONOTE_PIN`), does the QR login **once**, and prints `{"famille": {url, username, password, uuid, client_identifier}}`: the `password` is PRONOTE's *token*, not your password. Delete `qr.json`. A wrong PIN or an expired code is **one** failed attempt; do not loop: generate a new QR code.
   - A school with a custom domain: add `--host pronote.my-school.example` (the helper only accepts `*.index-education.net` otherwise).
3. Give the output to the plugin, either as the value of the **secret `PRONOTE_CREDENTIALS`** (an optional secret the instance resolves from its environment), or with `--store /approved/dir/pronote-credentials.json` instead of printing (the file is written with mode 0600).

### Where the rotating credentials live

PRONOTE returns a **new token on every login** and the old one stops working. A secret is read-only for a plugin, so it cannot hold it. The source therefore persists the credentials in the **credential store**, a JSON file `credentials_path` (default: next to the guard state, so in a directory the instance already approved for writing, `call.paths.allowed(write=True)`). It is written:

- **right after each login, before anything is read**, and again if the token changed while reading (a failed read must not lose the token);
- atomically (a temporary sibling, flushed, then `os.replace`), mode 0600, under a `flock`;
- and it is proven writable **before** the login (a store that cannot be written stops the poll with no budget spent).

The first credentials come from the secret `PRONOTE_CREDENTIALS` (or from `pronote-setup --store`). Once the store has an entry for the account, **it wins**: the secret then holds a spent token. If you delete the store, the stale secret is tried, PRONOTE refuses it, and the account stops (below); run the setup again. A store that cannot be parsed is refused and left untouched, never overwritten. Treat the file like a password: it is not logged, not returned and not in any error.

### Failures: degraded, never a retry loop

| Case | Result |
|---|---|
| Wrong or expired token, QR code or PIN, a second factor, PRONOTE's "too many requests" lockout message | `AuthFailure(code)`: `degraded: auth_stopped (login_failed \| mfa_failed \| locked_out)`; the account is **stopped** until a human resets it ([Resetting an auth stop](#limits)) |
| Network error, timeout (30 s per request, login and reads), HTTP error, an API change, the store not writable after a login | `degraded: source_unavailable (network \| service \| api_changed \| credentials_unwritable)`; the guard backs off 1 h, 2 h ... 24 h |
| Budget, window, backoff or stop | `degraded: budget_exhausted \| outside_window \| backoff_until <time> \| auth_stopped`, with no login |
| `children` or `periods` that match nothing | `config_invalid`, after a good login that counts as a success |

Messages are fixed codes: no credential, URL, name or response body is ever logged or returned (pronotepy's own logger, which logs them, is silenced). While logging in, any error PRONOTE itself codes counts as a refusal, not as an outage, because trying again is what locks accounts.

### Network

A school's server is `<school>.index-education.net` or a custom domain, and a manifest host is an exact DNS name (no wildcard), so **`plugin.yaml` declares no network host**. Approve your school's host in `horae.lock` (`permissions.network`, for example `0000000a.index-education.net`). Before the budget is touched, the source checks the credentials' URL with `call.host_allowed(url)`: `False` refuses (`network_not_approved`); `True` is allowed; `None` (an older core that sent no list) falls back to the plugin's own rule: a plain `https` host under `index-education.net`, or one named in `hosts`.

**Limit:** pronotepy opens its own HTTP session, so this check happens *before the login* and cannot constrain the requests pronotepy then makes (nor a redirect it follows). The M5 isolation tier is what will enforce the allow-list at the network level.

### Permissions

| Kind | Entry | Why |
|---|---|---|
| intent | `task.create` | undone homework and things to sign (`normalize`) |
| optional secret | `PRONOTE_CREDENTIALS` | the first credentials, from `pronote-setup` |
| path (per instance, `paths.write`) | the directory of `state_path` and `credentials_path` | the guard state and the credential store |
| network | none declared; approve the school's host in the lock | no wildcard in a manifest |

## ÉcoleDirecte

`school-fr.ecoledirecte` (a source) reads a parent's (or a student's) ÉcoleDirecte account **read-only**. **ÉcoleDirecte has no public API**: the plugin speaks the web app's reverse-engineered HTTP API (`https://api.ecoledirecte.com/v3`), which may change without notice, and repeated or failed logins and wrong security answers can lock the account (risk [horae#73](https://github.com/horae-dev/horae/issues/73)). So it goes through the [lockout guard](#limits) before every login, makes **one attempt per call, never retries**, and degrades instead. It has been written against the community documentation and synthetic data, **not** against a real account: try it on yours before trusting it (see "First check").

**Why our own client.** The maintained Python library (`ecoledirecte` on PyPI, the Home Assistant integration's client) is LGPL-3.0-or-later, needs Python 3.14 (this plugin: 3.12), is asynchronous, re-authenticates on its own after an error (which the guard forbids) and has no trusted-device persistence (a security question is answered from a pre-supplied list). The plugin therefore has a small read-only client (`ecoledirecte.py`: `requests`, a timeout on every request, a fixed host, no redirect followed, a 4 MiB response cap, and an allow-list of four read routes).

### What it reads

For the chosen children (all by default), and nothing else:

| ÉcoleDirecte | Becomes |
|---|---|
| Homework (*cahier de texte*) due from today to `days_ahead` (HTML turned to text; at most 20 days are fetched) | `homework` (a task while not done) |
| Grades of the current period, or of the `periods` asked (the newest 200 per child) | `grades` (data only) |
| **Unread** received messages (30 days; the list only: a message is never opened, because opening marks it read) | `signatures` (a task each) |

Messages are addressed to the parent: they are listed once, under the first selected child. Children are published under an opaque id (a hash), with the platform's name in `children[].first_name`, and the homework's teacher in `homework[].teacher`, both declared for de-identification (see below). It never writes: the client has no method to mark homework done, to reply to or delete a message; the only routes it can request are on a read allow-list, always with the read verb, and a test checks every request of a poll.

### Parameters

The same as PRONOTE's (`account`, `state_path`, `credentials_path`, `max_polls_per_day`, `min_interval`, `windows`, `max_logins_per_day`, `children`, `periods`, `days_ahead`), without `hosts`. `credentials_path` defaults to `ecoledirecte-device.json` next to `state_path` and must be in an approved directory too. Every poll is one login (a plugin process has no cached session), so `max_logins_per_day` defaults to `max_polls_per_day` (at most 6).

### First setup: the security question

ÉcoleDirecte asks a **security question** (the QCM) when a login comes from a device it does not know. A scheduled source **never answers it**: a bot guessing at it is how accounts get locked. You answer it once, with the helper, and ÉcoleDirecte then gives the plugin a **trusted device** (a `cn`/`cv` pair) that later logins present, so the question is not asked again.

1. Put your user name and password in the **secret `ECOLEDIRECTE_CREDENTIALS`** (an optional secret the instance resolves from its environment): `{"famille": {"username": "...", "password": "..."}}`, the key being the account label. (They are never written to a file by the plugin.)
2. Run the helper once, on your own machine, with the plugin's Python environment, and a store in a directory approved for writing (the same one as the guard state):

   ```sh
   python3 -m horae_school_fr ecoledirecte-setup --account famille --store /approved/dir/ecoledirecte-device.json
   ```

   It takes the user name and password from the secret if it is set in the environment, else prompts for them (the password is not echoed; prefer that to typing the password in a command line). It logs in **once**, shows the question and its numbered choices, takes **one** answer, and writes the resulting `cn`/`cv` to the store (mode 0600, atomic, under a lock). It prints nothing secret. A wrong answer or password is **one** failed attempt: the helper stops with a fixed message and never loops. Do not retry blindly: check the answer, and log in once in the ÉcoleDirecte app or site first.
3. Schedule the source. It presents the stored device at every login, and **saves the one the server returns after each login, before reading anything** (ÉcoleDirecte may rotate it), and again if it changed.

If a scheduled run is asked the question anyway (the device expired, was revoked, or the store was lost), the source does **not** answer: it raises `AuthFailure("quiz_required")` and the account is **stopped** (`degraded: auth_stopped (quiz_required)`). Rerun the setup, then [reset the stop](#limits). An account that ÉcoleDirecte never asks a question has no device to store; the setup then says so and the source simply logs in.

### Failures: degraded, never a retry loop

| Case | Result |
|---|---|
| Wrong user name or password, a lockout message, a security question in a scheduled run, a refused answer | `AuthFailure(code)`: `degraded: auth_stopped (login_failed \| locked_out \| quiz_required \| quiz_failed)`; the account is **stopped** until a human resets it |
| Network error, timeout (30 s per request, login and reads), HTTP 5xx or 429, a redirect, a body that is not the expected JSON or too large, a refused read, the store not writable after a login | `degraded: source_unavailable (network \| service \| api_changed \| credentials_unwritable)`; the guard backs off 1 h, 2 h ... 24 h |
| Budget, window, backoff or stop | `degraded: budget_exhausted \| outside_window \| backoff_until <time> \| auth_stopped`, with no request |
| `children` or `periods` that match nothing | `config_invalid`, after a good login that counts as a success |

Messages are fixed codes: no credential, `cn`/`cv`, URL, name or response body is ever logged or returned. Any error code ÉcoleDirecte gives to a **login** counts as a refusal, not as an outage, because trying again is what locks accounts.

### Network

`plugin.yaml` declares the one host, **`api.ecoledirecte.com`** (`permissions.network`), which is the only host the client ever contacts (a request to another host, or a redirect, is impossible by construction). Before the budget is touched, the source asks `call.host_allowed`: `False` (the host is not approved in `horae.lock`) refuses with `network_not_approved`; `True`, or `None` (an older core that sent no list), proceeds with that declared host.

### First check

With your real account, in a directory approved for writing: run the setup, answer the question, then call `school-fr.ecoledirecte` twice, two hours apart (or with `min_interval: 1m`): the second run must not ask a question, the device file must exist with mode 0600, and the result must list your children's homework, grades and unread messages. If a field is empty or the status is `source_unavailable (api_changed)`, ÉcoleDirecte probably changed: open an issue without pasting any response.

### Permissions

| Kind | Entry | Why |
|---|---|---|
| intent | `task.create` | undone homework and things to sign (`normalize`) |
| network | `api.ecoledirecte.com` | the ÉcoleDirecte API |
| optional secret | `ECOLEDIRECTE_CREDENTIALS` | the user name and password |
| path (per instance, `paths.write`) | the directory of `state_path` and `credentials_path` | the guard state and the trusted-device store |

## Normalization and de-identification

`horae_school_fr.normalize` is a library the sources call; it adds no tool and no permission. A source maps what the platform returns to `Child`, `Homework`, `Grade` and `Signature` and returns `normalize(account, children, homework=..., grades=..., signatures=...)`: the source data and the `task.create` intents.

| Item | Becomes |
|---|---|
| Homework not done | data and a task (due date, in the instance time zone) |
| Homework done | data only |
| Grade / note | data only, never a task |
| Thing to sign or acknowledge | data and a task |

A task's fingerprint is `school-fr:<account>:<homework|signature>:<hash>`, the hash covering the stable fields only (child id, subject or title, date, text): the done flag, the attachments count and the teacher never change it. Titles are escaped with the SDK (`escape_title`) and capped at 160 characters. Free text in the data is capped; the data holds child ids per item, and names only in `children[].first_name` and the optional `teacher`.

**De-identification.** In Horae, a *skill* declares de-identified fields in its `input_view` (`skill.yaml`, `{class: person}`); a plugin's manifest or a source's output carries no such annotation (schemas decision 47). So this plugin publishes the declaration as data for the skills built on it: `horae_school_fr.input_view()` (the whole view, to copy from) and `DEIDENTIFIED_FIELDS` (`children[].first_name`, `homework[].teacher`, `grades[].teacher`). With a cloud model alias the core then replaces those values, and every other string that contains one (a homework description that mentions the child), with `[PERSON_n]` before the request leaves, and restores them in the answer. `tests/test_deid.py` runs this output through the core's engine and `ModelBackends` and checks the exact bytes sent.

Limits, in the core and here:

- The core compares letters case-folded but **not accent-folded**: `Zoe` is not `Zoé` to it. `normalize` therefore spells every whole-word occurrence of a declared name in free text as declared (`zoe`, `ZOÉ`, a decomposed `é` become `Zoé`) so the core then catches it; `tests/test_deid.py` documents the gap.
- Nicknames, initials, typos and names of fewer than 3 letters are not matched, by the core or here.
- A skill that does not declare the classes gets no protection: copy every `person` line of `input_view()`. Local model aliases are sent the data unchanged, by design.

## Limits

PRONOTE and ÉcoleDirecte are reverse-engineered, unofficial APIs. Logging in again and again, or failing a login or a security question several times, can **lock a parent's account**, and nothing in the plugin can undo that. So every source goes through one persistent budget before it touches the network, and that budget is deliberately conservative. This is **best effort**: it limits what this plugin does; it cannot know what the school, the platform or another client of the same account does, and the platforms may change their rules without notice.

Per account (a label, the same one the source uses), counted per **local day in the instance time zone** (`TZ` of the plugin host, from `horae.yaml` `timezone`; midnight to midnight, DST included):

| Setting | Default | Range | Meaning |
|---|---|---|---|
| `max_polls_per_day` | 4 | 1 to 12 | polls (attempts that may touch the network) per day |
| `min_interval` | `2h` | `1m` to `24h` | least time between two polls (`90m`, `2h`) |
| `windows` | none (any time) | up to 8 | allowed local-time windows, `["07:00-08:00", "17:00-20:00"]`; start inclusive, end exclusive; `22:00-06:00` crosses midnight |
| `max_logins_per_day` | 2 | 1 to 6 | logins per day, counted apart from polls. A login happens **only when the cached session or token is gone** |

**Backoff.** After a failure that is not an authentication failure (network, an API change, a parse error) the account waits 1 h, then 2 h, 4 h, 8 h, 16 h and at most 24 h after each further failure in a row. One success resets the streak.

**Auth stop.** After an authentication failure (wrong credentials, a failed security question, a lockout message) the account is **stopped completely**: no attempt at all, however much time passes, until a human resets it. Nothing retries inside a call; a call makes at most one attempt.

**What a refused call looks like.** The call returns without any network call as a tool error whose message is one of these fixed reasons, so the core marks the step `degraded`:

| Message | Meaning |
|---|---|
| `degraded: budget_exhausted` (+ `polls_per_day`, `min_interval`, `logins_per_day`, `state_busy`) | the day's polls or logins are used, `min_interval` has not passed, or another run held the state lock for 10 s |
| `degraded: outside_window` | the local time is outside every window |
| `degraded: backoff_until <time>` | backing off until that local time (ISO 8601 with offset) |
| `degraded: auth_stopped` (+ the stop code, or `state_invalid`) | stopped after an auth failure, or the state file cannot be trusted |

**Resetting an auth stop.** There is no tool for it, on purpose: a pipeline must not be able to lift the stop that protects an account. First fix the cause by hand (check the password and log in once in the school's own app or site), then **delete that account's entry from the state file**, with no run in progress:

```sh
jq 'del(.accounts["anna"])' state.json > state.json.new && mv state.json.new state.json
```

(or edit the file and remove the `"anna": {...}` object). The account then starts with a clean slate. Deleting the whole file resets every account. An entry's `auth_stopped.code` tells what stopped it (`login_failed`, `quiz_failed`, `locked_out`...).

**The state file.** A JSON file at `state_path`, written atomically (a temporary sibling, then an atomic replace, mode 0600) under an exclusive `flock` on `<state_path>.lock`, so two overlapping runs cannot both log in: the budget is reserved before the attempt, and a crash during an attempt still counts. It holds timestamps, counters and short fixed codes only: no credential, token, name or message from the service. **A file that cannot be read, parsed or recognised fails closed**: every account is treated as stopped (`degraded: auth_stopped (state_invalid)`) and the file is left untouched for you to inspect; repair it, or delete it (after checking the accounts) to start over.

## For source authors

The PRONOTE and ÉcoleDirecte sources go through the guard (`horae_school_fr.guard`, re-exported by `horae_school_fr`):

```python
limits = Limits.from_params(
    max_polls_per_day=..., min_interval=..., windows=..., max_logins_per_day=...
)
guard = guard_for(call, state_path=state_path, account=account, limits=limits)  # checks the path
attempt = guard.begin(login=session_is_gone)  # raises Refused (a PluginError): let it propagate
with attempt:
    ...  # one network round, at most one login, no retry
    raise AuthFailure("quiz_failed")  # wrong credentials, quiz, lockout text: a fixed code
```

Leaving the block normally records a success; `AuthFailure` stops the account; any other exception records a transient failure (backoff) and propagates. Use `login=True` only when the cached session or token is gone. In tests inject `clock=` (an aware `datetime` factory) and `environ={"TZ": ...}` through `build` and `guard_for`.

## Planned work

| Milestone | Epic |
|---|---|
| M7 Mail, school, home | **French school platforms** — Read-only PRONOTE and ÉcoleDirecte sources. Done so far: lockout-safe scheduling ([#5](../../issues/5)), normalization and de-identification ([#4](../../issues/4)), the PRONOTE source ([#2](../../issues/2)), the ÉcoleDirecte source ([#3](../../issues/3)). |

Stories, estimates and acceptance criteria are in this repository's [issues](../../issues) and on the [roadmap project](https://github.com/orgs/horae-dev/projects/1).

## Development

`uv sync`, then `uv run ruff check`, `uv run ruff format --check`, `uv run mypy` and `uv run pytest --cov` (the gate is 85%). After changing tools or permissions: `uv run python -m horae_school_fr --manifest > plugin.yaml`.

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)

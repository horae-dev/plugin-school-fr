<p align="center"><img src="https://raw.githubusercontent.com/horae-dev/.github/main/brand/horae-logo-500.png" width="96" alt="Horae logo"></p>

# plugin-school-fr

Horae plugin: read-only PRONOTE and ÉcoleDirecte homework, notes and deadlines as tasks.

Version 0.1.0 · requires `horae-sdk` 0.6 · Apache-2.0

> 🚧 **Early.** This release ships the lockout guard only. The PRONOTE and ÉcoleDirecte sources ([#2](../../issues/2), [#3](../../issues/3)) and normalization ([#4](../../issues/4)) come next and sit on top of it.

## The tool

`school-fr.status` (a source) reports the guard state of the accounts in a state file. It never logs in, never touches the network, writes nothing, proposes no intent and declares no permission.

| Parameter | Default | Meaning |
|---|---|---|
| `state_path` | required | absolute path of the guard's JSON state file; its directory must be approved for writing (`paths.write`) in `horae.lock` |
| `accounts` | all in the file | up to 16 account labels |
| `max_polls_per_day`, `min_interval`, `windows`, `max_logins_per_day` | the defaults below | the limits to judge `would_refuse` against |

For each account it returns the polls and logins used today, the last poll, the earliest next poll, the backoff, the auth stop and `would_refuse` (the reason a poll would be refused right now, or `null`). A state file that cannot be trusted is reported as `state: invalid`.

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

PRONOTE and ÉcoleDirecte sources must go through the guard (`horae_school_fr.guard`, re-exported by `horae_school_fr`):

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
| M7 Mail, school, home | **French school platforms** — Read-only PRONOTE and ÉcoleDirecte sources. Done so far: lockout-safe scheduling ([#5](../../issues/5)). |

Stories, estimates and acceptance criteria are in this repository's [issues](../../issues) and on the [roadmap project](https://github.com/orgs/horae-dev/projects/1).

## Development

`uv sync`, then `uv run ruff check`, `uv run ruff format --check`, `uv run mypy` and `uv run pytest --cov` (the gate is 85%). After changing tools or permissions: `uv run python -m horae_school_fr --manifest > plugin.yaml`.

## Contributing

This project is developed mostly by AI coding agents supervised by a human maintainer. Read [AGENTS.md](AGENTS.md) for the workflow (it applies to humans too), and the organization's [contributing guide](https://github.com/horae-dev/.github/blob/main/CONTRIBUTING.md) and [security policy](https://github.com/horae-dev/.github/blob/main/SECURITY.md).

## License

[Apache-2.0](LICENSE)

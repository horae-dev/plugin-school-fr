# Changelog

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

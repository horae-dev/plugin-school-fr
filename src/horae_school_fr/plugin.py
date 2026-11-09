# SPDX-License-Identifier: Apache-2.0
"""The plugin: the lockout guard and one diagnostic source, `school-fr.status`.

PRONOTE and ÉcoleDirecte are reverse-engineered APIs; repeated logins can lock a parent's
account. This plugin therefore puts a persistent poll and login budget (`horae_school_fr.guard`)
between every source and the network. The PRONOTE and ÉcoleDirecte sources
call `guard_for(...)` and `Guard.begin(...)` before any network use.

`school-fr.status` reports the guard state of the accounts in a state file. It never logs in and
never touches the network, proposes no intent and declares no permission.

The resetting of an auth stop is deliberately **not** a tool: a pipeline must not be able to
clear the stop that protects an account. See the README, "Limits".
"""

import os
from collections.abc import Mapping
from typing import Any

from horae_sdk import Call, Output, Permissions, Plugin, PluginError

from horae_school_fr import credentials, ecoledirecte, pronote
from horae_school_fr.guard import (
    Budget,
    Clock,
    Guard,
    Limits,
    instance_timezone,
    resolve_state_path,
    utc_now,
)

NAME = "school-fr"
"""The plugin's name, also the namespace of its refs (`school-fr.status`)."""
VERSION = "0.4.0"
HORAE = ">=0,<0.2"
COMMAND = ["python3", "-m", "horae_school_fr"]
"""`runtime.command` of `plugin.yaml`. When installing, pin an absolute interpreter that has
this package (the host's `PATH` is fixed and its working directory empty)."""
PERMISSIONS = Permissions(
    intents=("task.create",),
    network=(ecoledirecte.HOST,),
    optional_secrets=(credentials.SECRET, credentials.ED_SECRET),
)
"""`task.create` (homework and things to sign); the host `api.ecoledirecte.com` of the
ÉcoleDirecte source; the optional secrets `PRONOTE_CREDENTIALS` (the first credentials of the
PRONOTE accounts, from `pronote-setup`) and `ECOLEDIRECTE_CREDENTIALS` (the user name and password
of the ÉcoleDirecte accounts).

**No PRONOTE host is declared**: a school's PRONOTE server is `<school>.index-education.net` or
a custom domain, and a manifest host is an exact DNS name (no wildcard, schemas decision 63). The
user approves their school's host in `horae.lock` (`permissions.network`) and the source checks
it with `call.host_allowed`. The directory of the guard state and of the credential store is
granted per instance in `horae.lock` (`paths.write`) and checked with `call.paths.allowed`."""
MAX_ACCOUNTS = 16


def guard_for(
    call: Call,
    *,
    state_path: object,
    account: object,
    limits: Limits,
    clock: Clock = utc_now,
    environ: Mapping[str, str] | None = None,
) -> Guard:
    """The guard a source uses, after it checked its other parameters: `state_path` is verified
    against `call.paths.allowed(write=True)` and the instance time zone (`TZ`) is read. Nothing
    touches the network and nothing is reserved until `Guard.begin`."""
    if not isinstance(account, str):
        raise PluginError("config_invalid: account is a label (A-Z a-z 0-9 . _ -, at most 32)")
    budget = Budget(resolve_state_path(call, state_path), instance_timezone(environ), clock)
    return Guard(budget, limits, account)


def build(
    *,
    clock: Clock = utc_now,
    environ: Mapping[str, str] | None = None,
    connect: pronote.Connect = pronote.login,
    ed_connect: ecoledirecte.Connect = ecoledirecte.login,
) -> Plugin:
    """The plugin. `clock`, `environ` (the `TZ` of the instance, the secrets), `connect` (the
    PRONOTE login: `Credentials` to a client) and `ed_connect` (the ÉcoleDirecte login: a `Login`
    and a `Device` to a client) are replaceable for tests."""
    plugin = Plugin(
        NAME,
        version=VERSION,
        permissions=PERMISSIONS,
        description="Read-only PRONOTE and ÉcoleDirecte sources behind a lockout-safe budget.",
    )

    @plugin.source(f"{NAME}.status")
    def status_source(
        call: Call,
        state_path: str | None = None,
        accounts: list[str] | None = None,
        max_polls_per_day: int = 4,
        min_interval: str = "2h",
        windows: list[str] | None = None,
        max_logins_per_day: int = 2,
    ) -> Output:
        """The guard state of `accounts` (default: those in the state file) in the state file
        `state_path`, against the given limits: polls and logins today, the next allowed poll,
        a backoff or an auth stop. Diagnostic only: no login, no network, nothing written."""
        limits = Limits.from_params(
            max_polls_per_day=max_polls_per_day,
            min_interval=min_interval,
            windows=windows,
            max_logins_per_day=max_logins_per_day,
        )
        budget = Budget(
            resolve_state_path(call, state_path),
            instance_timezone(os.environ if environ is None else environ),
            clock,
        )
        labels = budget.accounts() if accounts is None else accounts
        if len(labels) > MAX_ACCOUNTS or len(set(labels)) != len(labels):
            raise PluginError(f"config_invalid: accounts lists 1 to {MAX_ACCOUNTS} distinct labels")
        return Output({"accounts": [budget.peek(label, limits) for label in labels]})

    @plugin.source(pronote.REF)
    def pronote_source(
        call: Call,
        account: str,
        state_path: str,
        credentials_path: str | None = None,
        max_polls_per_day: int = 4,
        min_interval: str = "2h",
        windows: list[str] | None = None,
        max_logins_per_day: int | None = None,
        children: list[str] | None = None,
        periods: list[str] | None = None,
        days_ahead: int = 14,
        hosts: list[str] | None = None,
    ) -> Output:
        """Read-only PRONOTE: homework due in the next `days_ahead` days, the grades of the
        current period (or `periods`) and the unread informations of `account`'s children
        (all, or `children`). One token login per poll, through the lockout guard; the rotated
        credentials are saved at once in the credential store next to `state_path`."""
        limits = Limits.from_params(
            max_polls_per_day=max_polls_per_day,
            min_interval=min_interval,
            windows=windows,
            max_logins_per_day=(
                min(max_polls_per_day, 6) if max_logins_per_day is None else max_logins_per_day
            ),
        )
        env = os.environ if environ is None else environ
        guard = guard_for(
            call, state_path=state_path, account=account, limits=limits, clock=clock, environ=env
        )
        return pronote.run(
            call,
            account=account,
            state_path=state_path,
            credentials_path=credentials_path,
            limits=limits,
            options=pronote.Options(
                pronote.strings("children", children),
                pronote.strings("periods", periods, 8),
                days_ahead,
            ),
            hosts=hosts,
            guard=guard,
            connect=connect,
            clock=clock,
            environ=env,
        )

    @plugin.source(ecoledirecte.REF)
    def ecoledirecte_source(
        call: Call,
        account: str,
        state_path: str,
        credentials_path: str | None = None,
        max_polls_per_day: int = 4,
        min_interval: str = "2h",
        windows: list[str] | None = None,
        max_logins_per_day: int | None = None,
        children: list[str] | None = None,
        periods: list[str] | None = None,
        days_ahead: int = 14,
    ) -> Output:
        """Read-only ÉcoleDirecte: homework due in the next `days_ahead` days, the grades of
        the current period (or `periods`) and the unread messages of `account`'s children (all,
        or `children`). One login per poll, through the lockout guard, presenting the trusted
        device stored next to `state_path` (set up once with `ecoledirecte-setup`); a security
        question in a scheduled run stops the account (`quiz_required`)."""
        limits = Limits.from_params(
            max_polls_per_day=max_polls_per_day,
            min_interval=min_interval,
            windows=windows,
            max_logins_per_day=(
                min(max_polls_per_day, 6) if max_logins_per_day is None else max_logins_per_day
            ),
        )
        env = os.environ if environ is None else environ
        guard = guard_for(
            call, state_path=state_path, account=account, limits=limits, clock=clock, environ=env
        )
        return ecoledirecte.run(
            call,
            account=account,
            state_path=state_path,
            credentials_path=credentials_path,
            limits=limits,
            options=ecoledirecte.Options(
                ecoledirecte.strings("children", children),
                ecoledirecte.strings("periods", periods, 8),
                days_ahead,
            ),
            guard=guard,
            connect=ed_connect,
            clock=clock,
            environ=env,
        )

    return plugin


def manifest() -> dict[str, Any]:
    """The `plugin.yaml` document of this plugin (`python -m horae_school_fr --manifest`)."""
    return build().manifest(
        command=COMMAND,
        horae=HORAE,
        license="Apache-2.0",
        homepage="https://github.com/horae-dev/plugin-school-fr",
    )

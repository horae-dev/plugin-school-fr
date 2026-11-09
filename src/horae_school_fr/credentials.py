# SPDX-License-Identifier: Apache-2.0
"""The rotating credentials of the PRONOTE accounts.

A PRONOTE token login returns a **new** token and the old one stops working, so the credentials a
source starts with are stale after the first login. They therefore live in a small JSON file, the
*credential store*, which the source rewrites right after every login, **before** it reads
anything. Secrets are read-only for a plugin, so the store sits in a directory the core approved
for writing (`paths.write` in `horae.lock`; the source checks it with `call.paths.allowed`),
next to the guard state.

The file is written like the guard state: a temporary sibling (mode 0600), flushed to disk, then
an atomic `os.replace`, under an exclusive `flock` on `<path>.lock`. It holds the url, the user
name, the token and the application UUID of each account, and nothing else. A file that cannot
be read or recognised is **refused**, never repaired or replaced: the token it held is the only
copy.

The same file format and the same care serve the ÉcoleDirecte *trusted device* (`Device`: the
`cn`/`cv` pair that spares a security question at every login): `RecordStore` is the generic
store, `CredentialStore` and `DeviceStore` its two uses. ÉcoleDirecte's user name and password are
never stored here: they stay in the secret `ECOLEDIRECTE_CREDENTIALS`.

The first credentials come from the store (after `pronote-setup --store`) or, when the store has
none for the account, from the optional secret `PRONOTE_CREDENTIALS` (the `pronote-setup`
output). Once the store has an entry, it wins: the secret holds a token that was spent by the
first login.
"""

import contextlib
import fcntl
import json
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from horae_sdk import PluginError

from horae_school_fr.guard import LABEL

VERSION: Final = 1
MAX_BYTES: Final = 256 * 1024
MAX_ACCOUNTS: Final = 32
MAX_FIELD: Final = 2048
LOCK_TIMEOUT_S: Final = 10.0
DEFAULT_NAME: Final = "pronote-credentials.json"
SECRET: Final = "PRONOTE_CREDENTIALS"  # noqa: S105  (the name of an environment variable)
"""The optional secret: a JSON object `{account label: credentials}`, as `pronote-setup`
prints it."""

_FIELDS: Final = ("url", "username", "password", "uuid")
_OPTIONAL: Final = ("client_identifier",)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Credentials:
    """What a token login needs. `password` is PRONOTE's rotating token. Never printed:
    `repr` shows nothing but the class."""

    url: str = field(repr=False)
    username: str = field(repr=False)
    password: str = field(repr=False)
    uuid: str = field(repr=False)
    client_identifier: str | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, str]:
        data = {name: getattr(self, name) for name in _FIELDS}
        if self.client_identifier:
            data["client_identifier"] = self.client_identifier
        return data


def parse(entry: object) -> Credentials:
    """A credentials mapping, or `PluginError("credentials_invalid: ...")` (no value is echoed)."""
    if not isinstance(entry, Mapping) or not set(_FIELDS) <= set(entry):
        raise PluginError("credentials_invalid: needs url, username, password and uuid")
    if not set(entry) <= {*_FIELDS, *_OPTIONAL}:
        raise PluginError("credentials_invalid: unknown field")
    for name in (*_FIELDS, *(n for n in _OPTIONAL if n in entry)):
        value = entry[name]
        if (
            not isinstance(value, str)
            or not (name in _OPTIONAL or value)
            or len(value) > MAX_FIELD
            or _CONTROL.search(value)
        ):
            raise PluginError(f"credentials_invalid: {name} is not a usable text")
    identifier = entry.get("client_identifier")
    return Credentials(
        entry["url"], entry["username"], entry["password"], entry["uuid"], identifier or None
    )


def from_secret(raw: str | None, account: str) -> Credentials | None:
    """The account's entry of the `PRONOTE_CREDENTIALS` secret, `None` when absent."""
    if not raw:
        return None
    try:
        document = json.loads(raw)
    except (ValueError, RecursionError):
        raise PluginError(f"credentials_invalid: {SECRET} is not JSON") from None
    if not isinstance(document, dict) or len(document) > MAX_ACCOUNTS:
        raise PluginError(f"credentials_invalid: {SECRET} is an object keyed by account")
    entry = document.get(account)
    return None if entry is None else parse(entry)


class RecordStore[T]:
    """A file of records keyed by account label, at `path` (already checked against the approved
    paths). `parse` turns a stored entry into a record (raising `PluginError` for a bad one) and
    `dump` turns a record into the entry written."""

    def __init__(
        self,
        path: str,
        parse: Callable[[object], T],
        dump: Callable[[T], dict[str, str]],
        *,
        lock_timeout: float = LOCK_TIMEOUT_S,
    ) -> None:
        self.path = path
        self.lock_timeout = lock_timeout
        self._parse = parse
        self._dump = dump

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        try:
            descriptor = os.open(
                f"{self.path}.lock",
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
        except OSError:
            raise PluginError("credentials_unwritable: cannot open the store lock") from None
        try:
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise PluginError("credentials_unwritable: the store is busy") from None
                    time.sleep(0.02)
            yield
        finally:
            os.close(descriptor)

    def _read(self) -> dict[str, T]:
        try:
            with open(self.path, "rb") as handle:
                raw = handle.read(MAX_BYTES + 1)
        except FileNotFoundError:
            return {}
        except OSError:
            raise PluginError("credentials_invalid: the store cannot be read") from None
        try:
            if len(raw) > MAX_BYTES:
                raise ValueError("too large")
            document = json.loads(raw)
            if (
                not isinstance(document, dict)
                or set(document) != {"version", "accounts"}
                or document["version"] != VERSION
                or not isinstance(document["accounts"], dict)
                or len(document["accounts"]) > MAX_ACCOUNTS
            ):
                raise ValueError("shape")
            result: dict[str, T] = {}
            for label, entry in document["accounts"].items():
                if not LABEL.fullmatch(label):
                    raise ValueError("label")
                result[label] = self._parse(entry)
            return result
        except (ValueError, KeyError, TypeError, RecursionError, PluginError):
            raise PluginError(
                "credentials_invalid: the store is not a valid credential file "
                "(left untouched; repair it, do not delete it blindly)"
            ) from None

    def _write(self, accounts: Mapping[str, T]) -> None:
        document: dict[str, Any] = {
            "version": VERSION,
            "accounts": {label: self._dump(c) for label, c in sorted(accounts.items())},
        }
        temporary = ""
        try:
            descriptor, temporary = tempfile.mkstemp(
                dir=os.path.dirname(self.path), prefix=".school-fr-credentials-"
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError:
            if temporary:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
            raise PluginError("credentials_unwritable: cannot write the store") from None

    def load(self, account: str) -> T | None:
        """The stored credentials of `account`, `None` when it has none (or no file)."""
        with self._locked():
            return self._read().get(account)

    def save(self, account: str, record: T) -> None:
        """Replace the account's entry, keeping the others. Atomic, mode 0600."""
        with self._locked():
            accounts = self._read()
            accounts[account] = record
            self._write(accounts)

    def check_writable(self) -> None:
        """Fail now, before a login spends the token, if the store cannot be written: rewrites
        the file as it is (and checks that it is valid)."""
        with self._locked():
            self._write(self._read())


class CredentialStore(RecordStore[Credentials]):
    """The PRONOTE credential file at `path`."""

    def __init__(self, path: str, *, lock_timeout: float = LOCK_TIMEOUT_S) -> None:
        super().__init__(path, parse, lambda c: c.to_dict(), lock_timeout=lock_timeout)


# --- ÉcoleDirecte ------------------------------------------------------------------------------

DEVICE_NAME: Final = "ecoledirecte-device.json"
ED_SECRET: Final = "ECOLEDIRECTE_CREDENTIALS"  # noqa: S105  (the name of an environment variable)
"""The ÉcoleDirecte secret: a JSON object `{account label: {"username": ..., "password": ...}}`."""


@dataclass(frozen=True)
class Login:
    """An ÉcoleDirecte user name and password (from the secret). Never printed."""

    username: str = field(repr=False)
    password: str = field(repr=False)


@dataclass(frozen=True)
class Device:
    """ÉcoleDirecte's trusted-device pair: after a security question is answered once, the server
    issues `cn` and `cv`, and a login that presents them is not asked the question again.
    Never printed."""

    cn: str = field(repr=False)
    cv: str = field(repr=False)

    def to_dict(self) -> dict[str, str]:
        return {"cn": self.cn, "cv": self.cv}


def _texts(entry: object, names: tuple[str, ...], what: str) -> list[str]:
    if not isinstance(entry, Mapping) or set(entry) != set(names):
        raise PluginError(f"credentials_invalid: {what} needs {' and '.join(names)}")
    values: list[str] = []
    for name in names:
        value = entry[name]
        if (
            not isinstance(value, str)
            or not value
            or len(value) > MAX_FIELD
            or _CONTROL.search(value)
        ):
            raise PluginError(f"credentials_invalid: {name} is not a usable text")
        values.append(value)
    return values


def parse_login(entry: object) -> Login:
    """`PluginError("credentials_invalid: ...")` for a bad entry (no value is echoed)."""
    username, password = _texts(entry, ("username", "password"), "an account")
    return Login(username, password)


def parse_device(entry: object) -> Device:
    cn, cv = _texts(entry, ("cn", "cv"), "a device")
    return Device(cn, cv)


def login_from_secret(raw: str | None, account: str) -> Login | None:
    """The account's entry of the `ECOLEDIRECTE_CREDENTIALS` secret, `None` when absent."""
    if not raw:
        return None
    try:
        document = json.loads(raw)
    except (ValueError, RecursionError):
        raise PluginError(f"credentials_invalid: {ED_SECRET} is not JSON") from None
    if not isinstance(document, dict) or len(document) > MAX_ACCOUNTS:
        raise PluginError(f"credentials_invalid: {ED_SECRET} is an object keyed by account")
    entry = document.get(account)
    return None if entry is None else parse_login(entry)


class DeviceStore(RecordStore[Device]):
    """The trusted-device file of the ÉcoleDirecte accounts at `path`."""

    def __init__(self, path: str, *, lock_timeout: float = LOCK_TIMEOUT_S) -> None:
        super().__init__(path, parse_device, lambda d: d.to_dict(), lock_timeout=lock_timeout)


__all__ = [
    "DEFAULT_NAME",
    "DEVICE_NAME",
    "ED_SECRET",
    "SECRET",
    "CredentialStore",
    "Credentials",
    "Device",
    "DeviceStore",
    "Login",
    "RecordStore",
    "from_secret",
    "login_from_secret",
    "parse",
    "parse_device",
    "parse_login",
]

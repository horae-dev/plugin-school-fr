# SPDX-License-Identifier: Apache-2.0
"""One HTTP session type with an explicit timeout on every request.

`socket.setdefaulttimeout` is not a reliable bound for `requests` (urllib3 sets its own socket
timeout from the request's `timeout`, and `None` means "wait forever"), and it does not cover
anything done after the login. `TimeoutSession` injects the timeout into **every** request that
does not set one itself, whatever the verb and whichever code issues it (a library that owns the
session, such as pronotepy, included). A timeout is a `requests.Timeout`, an `OSError`: the
sources classify it as a transient network failure.
"""

from typing import Any

import requests


class TimeoutSession(requests.Session):
    """A `requests.Session` whose requests always carry a timeout (seconds, connect and read)."""

    def __init__(self, timeout: float) -> None:
        super().__init__()
        self.default_timeout = timeout

    def request(  # type: ignore[override]
        self, method: str, url: str | bytes, **kwargs: Any
    ) -> requests.Response:
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = self.default_timeout
        return super().request(method, url, **kwargs)


__all__ = ["TimeoutSession"]

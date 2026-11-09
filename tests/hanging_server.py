# SPDX-License-Identifier: Apache-2.0
"""A local TCP server that accepts connections and never answers: for the timeout tests."""

import contextlib
import socket
import threading
from collections.abc import Iterator


@contextlib.contextmanager
def hanging_server() -> Iterator[int]:
    """Yield the port of a 127.0.0.1 server that accepts and reads nothing back, ever."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.1)
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        while not stop.is_set():
            try:
                held.append(listener.accept()[0])
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield int(listener.getsockname()[1])
    finally:
        stop.set()
        thread.join(2)
        for connection in held:
            connection.close()
        listener.close()

"""Shared test fixtures.

The suite is meant to run entirely offline. That is not just a nicety: when a
refactor moved the seam these tests stub, they silently started making real
requests and the run went from 6 seconds to 8 minutes while still reporting
failures that looked like logic errors. The guard below turns that failure mode
into an immediate, obvious error instead.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

_LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _host_of(address: Any) -> str:
    """Extract the host part of a socket address."""
    if isinstance(address, tuple) and address:
        return str(address[0])
    return str(address)


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens a connection off the loopback interface.

    Loopback stays open because asyncio builds its event-loop self-pipe from a
    socket pair on Windows.
    """

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        host = _host_of(address)
        if host not in _LOOPBACK:
            raise RuntimeError(
                f"test attempted a network connection to {host!r}; "
                "the suite must run offline -- stub the collaborator instead"
            )
        return _real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> Any:
        host = _host_of(address)
        if host not in _LOOPBACK:
            raise RuntimeError(
                f"test attempted a network connection to {host!r}; "
                "the suite must run offline -- stub the collaborator instead"
            )
        return _real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)

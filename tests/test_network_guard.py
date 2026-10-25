"""The test suite must not be able to reach the network."""

import socket

import pytest


def test_outgoing_connections_are_blocked() -> None:
    with pytest.raises(RuntimeError, match="Network access is forbidden"):
        socket.create_connection(("example.org", 80))


def test_dns_resolution_is_blocked() -> None:
    with pytest.raises(RuntimeError, match="Network access is forbidden"):
        socket.getaddrinfo("example.org", 80)

"""Lifecycle tests for the high-level Hapbeat client (no real sockets).

Regression focus: ``with hapbeat.connect(...) as hb:`` calls open() twice
(connect() opens, __enter__ opens again). open() must be idempotent so the
double call does not register a duplicate pong listener or leak a second
keepalive thread whose orphaned stop Event keeps it alive after close().
"""

from __future__ import annotations

import threading

import pytest

from hapbeat import hapbeat as hapbeat_mod


class FakeUdpClient:
    """Stands in for UdpClient: records listeners/sends, no sockets."""

    def __init__(self, port=7700, broadcast_addr="255.255.255.255"):
        self.port = port
        self.broadcast_addr = broadcast_addr
        self.listeners = []
        self.open_calls = 0
        self.sent = []

    def open(self):
        self.open_calls += 1

    def close(self):
        pass

    def add_pong_listener(self, cb):
        self.listeners.append(cb)

    def remove_pong_listener(self, cb):
        try:
            self.listeners.remove(cb)
        except ValueError:
            pass

    def send(self, packet, addr=None):
        self.sent.append(packet)
        return True


def keepalive_threads():
    return [
        t for t in threading.enumerate()
        if t.name == "hapbeat-keepalive" and t.is_alive()
    ]


@pytest.fixture
def fake_client(monkeypatch):
    monkeypatch.setattr(hapbeat_mod, "UdpClient", FakeUdpClient)


def test_open_twice_is_idempotent(fake_client):
    base = len(keepalive_threads())
    hb = hapbeat_mod.Hapbeat(app_name="Test")
    hb.open()
    first_thread = hb._keepalive_thread
    hb.open()
    assert len(hb._client.listeners) == 1
    assert hb._keepalive_thread is first_thread
    assert len(keepalive_threads()) == base + 1
    hb.close()
    assert len(keepalive_threads()) == base


def test_connect_as_context_manager_single_open(fake_client):
    base = len(keepalive_threads())
    with hapbeat_mod.connect(app_name="Test") as hb:
        assert len(hb._client.listeners) == 1
        assert len(keepalive_threads()) == base + 1
        thread = hb._keepalive_thread
    assert not thread.is_alive()  # joined, not orphaned
    assert hb._keepalive_thread is None
    assert hb._client.listeners == []
    assert len(keepalive_threads()) == base


def test_close_is_idempotent_and_reopen_works(fake_client):
    hb = hapbeat_mod.Hapbeat(app_name="Test")
    hb.close()  # never opened: no-op, no connect_status sent
    assert hb._client.sent == []
    hb.open()
    hb.close()
    hb.close()
    hb.open()
    assert len(hb._client.listeners) == 1
    hb.close()
    assert hb._client.listeners == []

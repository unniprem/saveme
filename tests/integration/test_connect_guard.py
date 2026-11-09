"""The connect-time guard is process-wide, so every test installs it in a fresh
interpreter and never in the pytest process."""
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PRE = (
    "import asyncio, socket, ssl, urllib.request\n"
    "from app.netguard import BlockedAddressError, install_connect_guard\n"
)
INSTALL = "install_connect_guard()\n"
# Like the worker: no proxy variables, so urllib connects to the target itself.
ENV = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}


def run(body: str, install: str = INSTALL) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", PRE + install + body],
        capture_output=True, text=True, timeout=20, cwd=ROOT, env=ENV,
    )


def expect_blocked(call: str) -> str:
    return f"try:\n    {call}\nexcept BlockedAddressError:\n    print('BLOCKED')\n"


class Listener:
    def __init__(self) -> None:
        self.sock = socket.create_server(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self._accepted = 0

    def accepted(self) -> int:
        """Accept every connection that reached the queue so far and count it."""
        self.sock.setblocking(False)
        while True:
            try:
                conn, _ = self.sock.accept()
            except BlockingIOError:
                return self._accepted
            conn.close()
            self._accepted += 1


@pytest.fixture
def listener():
    lst = Listener()
    yield lst
    lst.sock.close()


def test_blocks_loopback_ip(listener):
    r = run(expect_blocked(f"socket.create_connection(('127.0.0.1', {listener.port}), timeout=2)"))
    assert r.stdout.split() == ["BLOCKED"], r.stdout + r.stderr
    assert listener.accepted() == 0


def test_blocks_hostname_resolving_private(listener):
    r = run(expect_blocked(f"socket.create_connection(('localhost', {listener.port}), timeout=2)"))
    assert r.stdout.split() == ["BLOCKED"], r.stdout + r.stderr
    assert listener.accepted() == 0


def test_rebinding_blocked():
    install = (
        "install_connect_guard(resolve=lambda *a, **k: "
        "[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 80))])\n"
    )
    body = expect_blocked("socket.socket().connect(('rebind.test', 80))")
    body += expect_blocked("socket.create_connection(('rebind.test', 80), timeout=2)")
    r = run(body, install=install)
    assert r.stdout.split() == ["BLOCKED", "BLOCKED"], r.stdout + r.stderr


def test_urllib_error_carries_marker(listener):
    body = (
        "try:\n"
        f"    urllib.request.urlopen('http://127.0.0.1:{listener.port}/', timeout=5)\n"
        "except Exception as e:\n"
        "    print(e)\n"
    )
    r = run(body)
    assert "[saveme-blocked]" in r.stdout, r.stdout + r.stderr
    assert listener.accepted() == 0


def test_connect_ex_and_unix_blocked(listener):
    body = expect_blocked(f"socket.socket().connect_ex(('127.0.0.1', {listener.port}))")
    body += expect_blocked("socket.socket(socket.AF_UNIX).connect('/tmp/x')")
    r = run(body)
    assert r.stdout.split() == ["BLOCKED", "BLOCKED"], r.stdout + r.stderr
    assert listener.accepted() == 0


def test_custom_policy_allows(listener):
    body = (
        f"s = socket.create_connection(('127.0.0.1', {listener.port}), timeout=2)\n"
        "print('CONNECTED')\n"
        "s.close()\n"
    )
    r = run(body, install="install_connect_guard(policy=lambda ip: True)\n")
    assert r.stdout.split() == ["CONNECTED"], r.stdout + r.stderr
    assert listener.accepted() == 1


# The other ways yt-dlp's stack reaches the network, plus literal forms.
OTHER_PATHS = {
    "ssl_socket": "s = ssl.create_default_context().wrap_socket(socket.socket(), "
                  "server_hostname='localhost'); s.settimeout(2); s.connect(('127.0.0.1', PORT))",
    "asyncio": "asyncio.run(asyncio.open_connection('127.0.0.1', PORT))",
    "getaddrinfo": "socket.getaddrinfo('localhost', PORT)",
    "bytes_host": "socket.socket().connect((b'127.0.0.1', PORT))",
    "zone_literal": "socket.socket(socket.AF_INET6).connect(('fe80::1%lo0', PORT, 0, 0))",
}


@pytest.mark.parametrize("call", OTHER_PATHS.values(), ids=OTHER_PATHS.keys())
def test_other_paths_blocked(listener, call):
    r = run(expect_blocked(call.replace("PORT", str(listener.port))))
    assert r.stdout.split() == ["BLOCKED"], r.stdout + r.stderr
    assert listener.accepted() == 0


def test_install_is_idempotent(listener):
    body = (
        "first = (socket.socket.connect, socket.getaddrinfo, socket.create_connection)\n"
        "install_connect_guard(policy=lambda ip: True)\n"
        "now = (socket.socket.connect, socket.getaddrinfo, socket.create_connection)\n"
        "print('SAME' if all(a is b for a, b in zip(first, now)) else 'CHANGED')\n"
    )
    body += expect_blocked(f"socket.create_connection(('127.0.0.1', {listener.port}), timeout=2)")
    r = run(body)
    assert r.stdout.split() == ["SAME", "BLOCKED"], r.stdout + r.stderr
    assert listener.accepted() == 0

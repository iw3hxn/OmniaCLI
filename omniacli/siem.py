# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Each entry of the access log, sent to a SIEM as it is written: syslog
RFC 5424 over UDP, TCP or TCP with TLS (RFC 5425 octet counting).

Sending is a thread of its own with a queue: an action never waits for the
SIEM. When it cannot send, the entries wait in memory (at most QUEUE_MAX,
the oldest dropped past it) and it tries again after a pause; the failure is
recorded in the access log once, and so is the recovery, with how many
entries were sent late or dropped.
"""

from __future__ import annotations

import collections
import json
import os
import socket
import ssl
import threading
import time
from typing import Callable

PROTOCOLS = ("udp", "tcp", "tls")
QUEUE_MAX = 10000
CONNECT_TIMEOUT = 5  # seconds
RETRY_FIRST, RETRY_MAX = 2, 60  # seconds
# facility authpriv (10): notice (5) for the failures, info (6) for the rest
FACILITY = 10
APP_NAME = "omniacli"
UDP_MAX = 8192


def _printable(text: str, limit: int) -> str:
    """An RFC 5424 header field: printable US-ASCII, no spaces; "-" when empty."""
    value = "".join(c if 33 <= ord(c) <= 126 else "_" for c in str(text))[:limit]
    return value or "-"


def format_message(entry: dict, hostname: str | None = None) -> bytes:
    """The syslog message of an entry: header, no structured data, the
    entry as JSON (UTF-8, after the BOM)."""
    severity = 5 if entry.get("event") in ("failed", "siem") else 6
    stamp = entry.get("utc") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    header = (f"<{FACILITY * 8 + severity}>1 {stamp} {_printable(hostname or socket.gethostname(), 255)} "
              f"{APP_NAME} {os.getpid()} {_printable(entry.get('event', ''), 32)} -")
    return header.encode("ascii") + b" \xef\xbb\xbf" + json.dumps(entry, ensure_ascii=False).encode("utf-8")


class Forwarder:
    """Sends the entries given to send() to the configured target."""

    def __init__(self, report: Callable[[str], None]):
        # report(text): a failure or a recovery, to the access log
        self.report = report
        self.target: tuple[str, int, str, str] | None = None  # host, port, protocol, ca_file
        self._queue: collections.deque = collections.deque()
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self._sock: socket.socket | None = None
        self.dropped = 0
        self.failing = ""  # why the last try failed, empty while it sends
        self.sent = 0
        self.last_sent = 0.0

    def configure(self, host: str, port: int, protocol: str, ca_file: str = "") -> None:
        """A new target (host empty: none); the entries waiting go there."""
        target = (host, int(port), protocol, ca_file) if host else None
        with self._cond:
            if target == self.target:
                return
            self.target = target
            self._close()
            if target is None:
                self._queue.clear()
                self.failing = ""
            elif self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="omniacli-siem", daemon=True)
                self._thread.start()
            self._cond.notify_all()

    def send(self, entry: dict) -> None:
        """Queue entry; never waits, never raises."""
        if self.target is None:
            return
        with self._cond:
            if len(self._queue) >= QUEUE_MAX:
                self._queue.popleft()
                self.dropped += 1
            self._queue.append(entry)
            self._cond.notify()

    def status(self) -> dict:
        host, port, protocol, _ = self.target or ("", 0, "", "")
        return {"target": f"{protocol}://{host}:{port}" if host else "", "failing": self.failing,
                "waiting": len(self._queue), "dropped": self.dropped, "sent": self.sent,
                "last_sent": self.last_sent}

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def _connect(self, host: str, port: int, protocol: str, ca_file: str) -> socket.socket:
        if protocol == "udp":
            family = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0]
            sock = socket.socket(family[0], socket.SOCK_DGRAM)
            sock.connect(family[4])
            return sock
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
        if protocol == "tls":
            context = ssl.create_default_context(cafile=ca_file or None)
            sock = context.wrap_socket(sock, server_hostname=host)
        sock.settimeout(CONNECT_TIMEOUT)
        return sock

    def _deliver(self, entry: dict) -> None:
        host, port, protocol, ca_file = self.target
        if self._sock is None:
            self._sock = self._connect(host, port, protocol, ca_file)
        message = format_message(entry)
        if protocol == "udp":
            self._sock.send(message[:UDP_MAX])
        else:
            self._sock.sendall(str(len(message)).encode() + b" " + message)

    def _run(self) -> None:
        pause = RETRY_FIRST
        while True:
            with self._cond:
                while self.target is None or not self._queue:
                    self._cond.wait()
                entry = self._queue[0]
                target = self.target
            try:
                self._deliver(entry)
            except (OSError, ValueError, ssl.SSLError) as exc:
                self._close()
                why = f"{type(exc).__name__}: {exc}"[:200]
                if not self.failing:
                    self.failing = why
                    self._report(f"syslog to {target[2]}://{target[0]}:{target[1]} failed: {why}; the entries "
                                 f"wait here (at most {QUEUE_MAX}) and are sent again")
                with self._cond:
                    self._cond.wait(pause)
                pause = min(pause * 2, RETRY_MAX)
                continue
            pause = RETRY_FIRST
            with self._cond:
                if self._queue and self._queue[0] is entry:
                    self._queue.popleft()
            self.sent += 1
            self.last_sent = time.time()
            if self.failing:
                self.failing = ""
                late = len(self._queue)
                self._report(f"syslog to {target[2]}://{target[0]}:{target[1]} works again: {late} entries "
                             f"still to send, {self.dropped} dropped while it failed")

    def _report(self, text: str) -> None:
        try:
            self.report(text)
        except Exception:  # noqa: BLE001 - recording the failure must not stop the thread
            pass

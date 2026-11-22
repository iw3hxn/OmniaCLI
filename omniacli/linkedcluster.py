# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A database on the cluster of a linked OmniaCLI (AD-003,
docs/adr/postgresql-della-macchina.md), this machine's side: what the
application needs before it asks the other machine for the database and
after it got it. No root, no helper: only what OmniaCLI's own user does.

- The role's password is made here and stays here: the other machine gets
  its SCRAM verifier (scram_verifier), which PostgreSQL keeps as it is. The
  password is on no link, in no log of either machine.
- The link must be protected (unprotected): what it answers (the port, the
  CA the database is then verified against) decides where Odoo connects;
  over plain HTTP anyone on the way could change it.
- host_of: the name this machine reaches the other one by, the one its
  certificate is made for (verify-full checks it).
- measure: the round trip to that machine, the time of a TCP connection
  (one packet there, one back), several times, the median; and the address
  this machine goes out with to reach it, the one proposed for pg_hba.
- check: the database's port as Odoo will reach it: TLS with the
  certificate verified against the CA and the name (verify-full), then the
  first message of PostgreSQL's protocol for that role and database; it
  answers "authenticate" when pg_hba lets this machine's address in, or
  says the address it saw. The password is not sent."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import re
import socket
import ssl
import statistics
import struct
import time
from urllib.parse import urlsplit

from .config import LinkTarget

# above this round trip the dialog warns. Measured between two machines (the ADR, AD-003
# "Latenza"): Odoo 19 pays about 2.5 round trips for each query it counts (130 for the 53 of a
# first page, 15 for the 9 of the web client), so a page of 200 queries makes 500: at 1 ms it
# waits half a second more for the network alone
WARN_MS = 1.0
# what a page of Odoo asks of its database, for the sentence of the warning
PAGE_QUERIES = 200
TRIPS_PER_QUERY = 2.5
TRIES = 7
CONNECT_TIMEOUT = 3.0
CHECK_TIMEOUT = 8.0
SCRAM_ITERATIONS = 4096
_SSL_REQUEST = struct.pack("!II", 8, 80877103)
_PROTOCOL_3 = 196608
_SEEN_FROM = re.compile(r'for host "([0-9a-fA-F.:]+)"')


class CheckFailed(Exception):
    """Why the database's port cannot be used as it is: said to the admin."""


def scram_verifier(password: str, salt: bytes | None = None, iterations: int = SCRAM_ITERATIONS) -> str:
    """The SCRAM-SHA-256 verifier of password as PostgreSQL keeps it
    (SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>, RFC 5802):
    a role made with it logs in with the password, which it does not hold."""
    salt = salt if salt is not None else os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    stored = hashlib.sha256(hmac.new(salted, b"Client Key", hashlib.sha256).digest()).digest()
    server = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    text = lambda raw: base64.b64encode(raw).decode("ascii")  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{text(salt)}${text(stored)}:{text(server)}"


def unprotected(link: LinkTarget) -> str | None:
    """Why the link may not carry the making of a database, None when it
    may: an SSH tunnel or HTTPS protect what it answers; plain HTTP does not."""
    if link.ssh is not None or urlsplit(link.url).scheme == "https":
        return None
    return (f"The link {link.name} is plain HTTP: what it answers (the port and the CA the database is verified "
            "against) could be changed on the way. Make it HTTPS or through SSH (Links page), then try again")


def host_of(link: LinkTarget) -> str:
    """How this machine reaches the machine of the link: the SSH server of a
    link through SSH, else the host of its URL."""
    return (link.ssh.host if link.ssh is not None else urlsplit(link.url).hostname or "").lower()


def link_port(link: LinkTarget) -> int:
    """A port of that machine this one reaches already: the link's own."""
    if link.ssh is not None:
        return int(link.ssh.port or 22)
    parts = urlsplit(link.url)
    return parts.port or (443 if parts.scheme == "https" else 80)


def _own_address(host: str, port: int) -> str:
    """The address this machine goes out with to reach host (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect((host, port))
            return probe.getsockname()[0]
    except OSError:
        return ""


def _round_trip(host: str, port: int, timeout: float) -> float | None:
    """The milliseconds of one TCP connection to host:port: its answer, an
    acceptance or a refusal, is one packet there and one back. None: no answer."""
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except ConnectionRefusedError:
        pass
    except OSError:
        return None
    return (time.perf_counter() - started) * 1000


def measure(host: str, ports: list[int], tries: int = TRIES, timeout: float = CONNECT_TIMEOUT) -> dict:
    """{"ms", "port", "tries", "address"}: the median round trip to host on
    the first of ports that answers (ms None: none did), and the address
    this machine goes out with to reach it ("" when there is no way)."""
    address = _own_address(host, ports[0] if ports else 5432)
    for port in ports:
        first = _round_trip(host, port, timeout)
        if first is None:
            continue
        times = [t for t in (_round_trip(host, port, timeout) for _ in range(tries)) if t is not None]
        if times:
            return {"ms": round(statistics.median(times), 2), "port": port, "tries": len(times), "address": address}
    return {"ms": None, "port": None, "tries": 0, "address": address}


def page_seconds(ms: float, queries: int = PAGE_QUERIES) -> float:
    """What a page of queries waits for the network alone, in seconds
    (TRIPS_PER_QUERY round trips for each query)."""
    return round(ms * queries * TRIPS_PER_QUERY / 1000, 2)


def _read(sock, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise CheckFailed("the database closed the connection before answering")
        data += chunk
    return data


def _error_text(payload: bytes) -> str:
    """The message of an ErrorResponse of PostgreSQL's protocol."""
    for field in payload.split(b"\0"):
        if field[:1] == b"M":
            return field[1:].decode("utf-8", "replace")
    return "an error without a message"


def check(host: str, port: int, ca: str, user: str, database: str, address: str = "",
          timeout: float = CHECK_TIMEOUT) -> dict:
    """The database's port reached as Odoo will: TCP, TLS with the
    certificate verified against ca and host (verify-full), the first
    message for user and database. {"tls"}: the protocol agreed, when
    PostgreSQL asks to authenticate (pg_hba.conf lets this machine in for
    them). CheckFailed says what is wrong; when the database refused the
    address it saw, which one that is and that it is not address, the one
    it was asked to let in."""
    where = f"{host}:{port}"
    try:
        context = ssl.create_default_context(cadata=ca)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
    except (ssl.SSLError, ValueError) as exc:
        raise CheckFailed(f"the CA of {where} is not a certificate ({exc})") from None
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
    except socket.timeout:
        raise CheckFailed(f"no answer from {where} within {timeout:.0f} s: its firewall does not see this machine "
                          "come from the address given, or the port is closed on the way") from None
    except OSError as exc:
        raise CheckFailed(f"{where} cannot be reached: {exc}") from None
    try:
        raw.settimeout(timeout)
        raw.sendall(_SSL_REQUEST)
        if _read(raw, 1) != b"S":
            raise CheckFailed(f"{where} does not speak TLS: the database is not reached in clear")
        try:
            # an address is checked against the certificate's addresses, a name against its names
            tls = context.wrap_socket(raw, server_hostname=host)
        except ssl.SSLCertVerificationError as exc:
            raise CheckFailed(f"the certificate of {where} is not the one of its CA, or not made for {host}: "
                              f"{exc.verify_message}") from None
        except (ssl.SSLError, OSError) as exc:
            raise CheckFailed(f"TLS with {where} failed: {exc}") from None
        raw = tls
        body = struct.pack("!I", _PROTOCOL_3) + b"".join(
            part.encode("utf-8") + b"\0" for part in ("user", user, "database", database,
                                                       "application_name", "omniacli-check")) + b"\0"
        tls.sendall(struct.pack("!I", len(body) + 4) + body)
        kind, length = struct.unpack("!cI", _read(tls, 5))
        payload = _read(tls, min(max(length - 4, 0), 8192))
        if kind == b"R":
            return {"tls": tls.version() or "TLS"}
        said = _error_text(payload) if kind == b"E" else f"an answer {kind!r} it should not give"
        seen = _SEEN_FROM.search(said)
        if seen and "pg_hba.conf" in said and address and seen.group(1) != address:
            raise CheckFailed(f"{where} sees this machine come from {seen.group(1)}, not from {address}: give "
                              "that address, or remove what changes it on the way")
        raise CheckFailed(f"{where} refused {user} on {database}: {said}")
    except socket.timeout:
        raise CheckFailed(f"{where} stopped answering within {timeout:.0f} s") from None
    except OSError as exc:
        raise CheckFailed(f"the connection to {where} broke: {exc}") from None
    finally:
        try:
            raw.close()
        except OSError:
            pass


def public_address(value: str) -> bool:
    """value is an address of the internet (not private, not of this machine)."""
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False

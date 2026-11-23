# Copyright (C) 2026 OmniaSolutions S.n.c di Boscolo Matteo & C
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A software authenticator for the tests of the passkeys: what a browser
and its authenticator answer to navigator.credentials.create() and get()
(WebAuthn Level 2, attestation "none", ES256), as the JSON the pages send."""
import base64
import hashlib
import json
import os
import struct

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SoftKey:
    """One passkey: its key pair, its credential id, its counter and the
    user handle it was made for (a discoverable credential keeps it)."""

    def __init__(self, uv: bool = True):
        self.private = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.count = 0
        self.handle = b""
        self.uv = uv

    def _flags(self, attested: bool) -> int:
        return 0x01 | (0x04 if self.uv else 0) | (0x40 if attested else 0)

    def _cose(self) -> bytes:
        numbers = self.private.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: numbers.x.to_bytes(32, "big"), -3: numbers.y.to_bytes(32, "big")})

    @staticmethod
    def client_data(kind: str, challenge: str, origin: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}).encode()

    def create(self, options: dict, origin: str, rp_id: str | None = None) -> dict:
        """The answer to create(options), at origin (rp_id: the one the authenticator uses, the options' by default)."""
        rp_id = rp_id or options["rp"]["id"]
        self.handle = unb64(options["user"]["id"])
        auth = (hashlib.sha256(rp_id.encode()).digest() + bytes([self._flags(True)]) + struct.pack(">I", self.count)
                + bytes(16) + struct.pack(">H", len(self.credential_id)) + self.credential_id + self._cose())
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        return {"id": b64(self.credential_id), "rawId": b64(self.credential_id), "type": "public-key",
                "authenticatorAttachment": "platform", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64(self.client_data("webauthn.create", options["challenge"], origin)),
                             "attestationObject": b64(attestation), "transports": ["internal"]}}

    def get(self, options: dict, origin: str, rp_id: str | None = None, count: int | None = None) -> dict:
        """The answer to get(options): the counter grows by one (count: the one to say instead)."""
        rp_id = rp_id or options["rpId"]
        self.count = self.count + 1 if count is None else count
        auth = hashlib.sha256(rp_id.encode()).digest() + bytes([self._flags(False)]) + struct.pack(">I", self.count)
        data = self.client_data("webauthn.get", options["challenge"], origin)
        signature = self.private.sign(auth + hashlib.sha256(data).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": b64(self.credential_id), "rawId": b64(self.credential_id), "type": "public-key",
                "authenticatorAttachment": "platform", "clientExtensionResults": {},
                "response": {"clientDataJSON": b64(data), "authenticatorData": b64(auth), "signature": b64(signature),
                             "userHandle": b64(self.handle) if self.handle else None}}

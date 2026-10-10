"""Discord interaction signature verification (DESIGN.md, ACK steps 2-3).

Discord signs `X-Signature-Timestamp + raw body` with the application's
Ed25519 key. The raw bytes are verified before any JSON parsing.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

SIGNATURE_HEADER = "X-Signature-Ed25519"
TIMESTAMP_HEADER = "X-Signature-Timestamp"

_SIGNATURE_BYTES = 64


class SignatureVerifier:
    def __init__(
        self,
        public_key: bytes,
        max_skew_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._key = VerifyKey(public_key)
        self._max_skew = max_skew_seconds
        self._clock = clock

    def verify(self, signature_hex: str | None, timestamp: str | None, body: bytes) -> bool:
        """True only for a valid signature over `timestamp + body` inside the skew window."""
        if not signature_hex or not timestamp:
            return False
        # The timestamp must be a plain unsigned integer. Checking before
        # verification also guarantees it encodes to ASCII bytes unchanged.
        if not timestamp.isascii() or not timestamp.isdigit():
            return False
        try:
            signature = bytes.fromhex(signature_hex)
        except ValueError:
            return False
        if len(signature) != _SIGNATURE_BYTES:
            return False

        try:
            self._key.verify(timestamp.encode("ascii") + body, signature)
        except BadSignatureError:
            return False

        return abs(self._clock() - int(timestamp)) <= self._max_skew

from __future__ import annotations

import pytest

from outer_steppes.signatures import SignatureVerifier

from .conftest import NOW, Signer

BODY = b'{"type":1}'


def make_verifier(signer: Signer, now: float = NOW, skew: int = 300) -> SignatureVerifier:
    return SignatureVerifier(signer.public_key, skew, clock=lambda: now)


def test_valid_signature_over_timestamp_then_body(signer):
    ts = str(NOW)
    assert make_verifier(signer).verify(signer.signature(ts, BODY), ts, BODY)


def test_signature_over_body_alone_is_rejected(signer):
    # Proves the signed message is timestamp + body, in that order.
    ts = str(NOW)
    sig_body_only = signer.signature("", BODY)
    sig_reversed = signer._key.sign(BODY + ts.encode()).signature.hex()
    verifier = make_verifier(signer)
    assert not verifier.verify(sig_body_only, ts, BODY)
    assert not verifier.verify(sig_reversed, ts, BODY)


def test_mutated_body_is_rejected(signer):
    ts = str(NOW)
    sig = signer.signature(ts, BODY)
    assert not make_verifier(signer).verify(sig, ts, b'{"type":2}')


def test_mutated_timestamp_is_rejected(signer):
    ts = str(NOW)
    sig = signer.signature(ts, BODY)
    assert not make_verifier(signer).verify(sig, str(NOW + 1), BODY)


def test_other_key_is_rejected(signer):
    other = Signer()
    ts = str(NOW)
    assert not make_verifier(signer).verify(other.signature(ts, BODY), ts, BODY)


@pytest.mark.parametrize("offset", [-301, 301, -10_000])
def test_timestamp_outside_skew_is_rejected(signer, offset):
    ts = str(NOW + offset)
    assert not make_verifier(signer).verify(signer.signature(ts, BODY), ts, BODY)


@pytest.mark.parametrize("offset", [-300, 0, 300])
def test_timestamp_at_skew_boundary_is_accepted(signer, offset):
    ts = str(NOW + offset)
    assert make_verifier(signer).verify(signer.signature(ts, BODY), ts, BODY)


@pytest.mark.parametrize("ts", ["", "-1", "+1700000000", "1700000000.0", " 1700000000", "١٧٠٠٠٠٠٠٠٠"])
def test_non_integer_timestamp_is_rejected(signer, ts):
    # Even a correct signature over the odd timestamp must not pass.
    sig = signer._key.sign(ts.encode("utf-8") + BODY).signature.hex()
    assert not make_verifier(signer).verify(sig, ts, BODY)


@pytest.mark.parametrize("sig", [None, "", "zz" * 64, "ab" * 63, "ab" * 65])
def test_malformed_signature_is_rejected(signer, sig):
    assert not make_verifier(signer).verify(sig, str(NOW), BODY)


def test_missing_timestamp_is_rejected(signer):
    sig = signer.signature(str(NOW), BODY)
    assert not make_verifier(signer).verify(sig, None, BODY)

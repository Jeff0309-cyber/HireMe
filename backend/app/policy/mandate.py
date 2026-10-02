"""Signing and verifying the user's mandate (contracts.Mandate -> contracts.SignedMandate).

The user signs the canonical JSON of the whole Mandate (task, category and policy). The gate
only trusts a SignedMandate whose signature verifies against the user public key the caller
passes in. The signer_pubkey stored inside is informational and never used for the decision.
"""
from __future__ import annotations

import hashlib
import json

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

from app.contracts import Mandate, SignedMandate


def mandate_signing_bytes(mandate: Mandate) -> bytes:
    """Canonical JSON: sorted keys, no spaces, timestamps as ISO strings."""
    return json.dumps(mandate.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()


def mandate_digest(mandate: Mandate) -> str:
    return hashlib.sha256(mandate_signing_bytes(mandate)).hexdigest()


def sign_mandate(mandate: Mandate, key: SigningKey) -> SignedMandate:
    return SignedMandate(
        mandate=mandate,
        signer_pubkey=key.verify_key.encode().hex(),
        signature=key.sign(mandate_signing_bytes(mandate)).signature.hex(),
    )


def mandate_signature_ok(signed: SignedMandate, trusted_pubkey_hex: str) -> bool:
    """True only if the signature verifies against the trusted key. Any error -> False."""
    try:
        VerifyKey(bytes.fromhex(trusted_pubkey_hex)).verify(
            mandate_signing_bytes(signed.mandate), bytes.fromhex(signed.signature)
        )
        return True
    except (BadSignatureError, ValueError, TypeError):
        return False

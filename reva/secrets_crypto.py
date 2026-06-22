"""Symmetric encryption for secrets stored in the DB (Fernet).

Used for Odoo instances' outbound callback API keys: REVA needs the plaintext
at call time, so the key is encrypted at rest under REVA_SECRET_KEY rather than
hashed. REVA_SECRET_KEY is a Fernet key (generate with Fernet.generate_key()).
"""

from __future__ import annotations

from cryptography.fernet import Fernet

from reva.config import env_or_file


def _fernet() -> Fernet:
    key = env_or_file("REVA_SECRET_KEY")
    if not key:
        raise RuntimeError(
            "REVA_SECRET_KEY is not set — cannot encrypt/decrypt Odoo callback "
            "keys. Generate one with `python -c \"from cryptography.fernet "
            "import Fernet; print(Fernet.generate_key().decode())\"`."
        )
    return Fernet(key.encode())


def encrypt(plaintext: str) -> str:
    """Fernet-encrypt `plaintext`. Empty string passes through unchanged."""
    if plaintext == "":
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a token from `encrypt`. Empty string passes through unchanged."""
    if token == "":
        return ""
    return _fernet().decrypt(token.encode()).decode()

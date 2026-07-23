"""
Signer key custody (T2).

Chosen over a plaintext env-var key (unsafe default) and over relying entirely on
relayer meta-tx signing (not yet confirmed the relayer supports it): a standard eth
keystore JSON file (the same format `geth`/`eth-account` produce), password-encrypted
at rest, with the passphrase supplied via an env var and the key decrypted only
in-process at startup. The decrypted key must never be written to logs — this module
never logs, prints, or otherwise serializes the private key or passphrase, and
LocalAccount's default repr is deliberately not relied upon (overridden below) so
accidental `print(key_custody)`/logging of the object can't leak it either.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from eth_account import Account
from eth_account.signers.local import LocalAccount

DEFAULT_PASSPHRASE_ENV_VAR = "CRSH_SIGNER_PASSPHRASE"


class KeyCustodyError(Exception):
    pass


class KeyCustody:
    def __init__(
        self,
        keystore_path: str | Path,
        passphrase_env_var: str = DEFAULT_PASSPHRASE_ENV_VAR,
    ):
        self._keystore_path = Path(keystore_path)
        self._passphrase_env_var = passphrase_env_var
        self._account: LocalAccount | None = None

    def load(self) -> None:
        """Decrypt the keystore into memory. Call once at daemon startup."""
        if not self._keystore_path.exists():
            raise KeyCustodyError(f"keystore file not found: {self._keystore_path}")

        passphrase = os.environ.get(self._passphrase_env_var)
        if not passphrase:
            raise KeyCustodyError(
                f"passphrase env var {self._passphrase_env_var!r} is not set"
            )

        keyfile_json = json.loads(self._keystore_path.read_text())
        try:
            private_key = Account.decrypt(keyfile_json, passphrase)
        except ValueError as exc:
            # eth-account raises ValueError (e.g. "MAC mismatch") on wrong passphrase
            # or a malformed keystore — never echo the passphrase or the raw error
            # payload, both of which could contain sensitive material.
            raise KeyCustodyError("failed to decrypt keystore (bad passphrase or corrupt file)") from exc

        self._account = Account.from_key(private_key)
        # private_key is a HexBytes/bytes object with no external references once
        # this method returns; it is not retained anywhere by this class beyond the
        # LocalAccount eth-account itself constructs and holds.

    @property
    def address(self) -> str:
        if self._account is None:
            raise KeyCustodyError("keystore not loaded — call load() first")
        return self._account.address

    def sign_transaction(self, tx: dict):
        if self._account is None:
            raise KeyCustodyError("keystore not loaded — call load() first")
        return self._account.sign_transaction(tx)

    def __repr__(self) -> str:
        addr = self._account.address if self._account else "<not loaded>"
        return f"KeyCustody(keystore_path={self._keystore_path!s}, address={addr})"

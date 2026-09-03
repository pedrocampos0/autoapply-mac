from __future__ import annotations

import uuid


KEYRING_PREFIX = "keyring:v1:"
SERVICE_NAME = "AutoApply"


class CredentialStoreError(RuntimeError):
    """Raised when the operating system credential store cannot be accessed."""


def _keyring():
    try:
        import keyring
    except ImportError as exc:
        raise CredentialStoreError("Install the project requirements to enable secure credential storage.") from exc
    return keyring


def protect_text(value: str, reference: str | None = None) -> str:
    """Store a secret in the OS credential store and return an opaque database reference."""
    key = reference or uuid.uuid4().hex
    try:
        _keyring().set_password(SERVICE_NAME, key, value)
    except Exception as exc:
        raise CredentialStoreError(f"Could not save a credential in the system keychain: {exc}") from exc
    return f"{KEYRING_PREFIX}{key}"


def unprotect_text(value: str) -> str:
    """Resolve an opaque system-keyring reference."""
    if value.startswith(KEYRING_PREFIX):
        key = value.removeprefix(KEYRING_PREFIX)
        try:
            secret = _keyring().get_password(SERVICE_NAME, key)
        except Exception as exc:
            raise CredentialStoreError(f"Could not read a credential from the system keychain: {exc}") from exc
        if secret is None:
            raise CredentialStoreError("The credential is no longer available in the system keychain.")
        return secret

    raise CredentialStoreError("The database contains an unsupported credential reference. Save the credential again.")


def delete_protected_text(value: str | None) -> None:
    if not value or not value.startswith(KEYRING_PREFIX):
        return
    key = value.removeprefix(KEYRING_PREFIX)
    try:
        _keyring().delete_password(SERVICE_NAME, key)
    except Exception:
        # A missing keyring item is already the desired end state.
        pass

from __future__ import annotations

import unittest
from unittest.mock import patch

from backend import credential_service
from backend.system_metrics import cpu_metrics, disk_metrics, memory_metrics


class FakeKeyring:
    def __init__(self) -> None:
        self.secrets: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, key: str, value: str) -> None:
        self.secrets[(service, key)] = value

    def get_password(self, service: str, key: str) -> str | None:
        return self.secrets.get((service, key))

    def delete_password(self, service: str, key: str) -> None:
        self.secrets.pop((service, key), None)


class CredentialServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.keyring = FakeKeyring()
        self.keyring_patch = patch.object(credential_service, "_keyring", return_value=self.keyring)
        self.keyring_patch.start()

    def tearDown(self) -> None:
        self.keyring_patch.stop()

    def test_round_trip_uses_opaque_reference(self) -> None:
        reference = credential_service.protect_text("secret", "site:7:password")

        self.assertEqual("keyring:v1:site:7:password", reference)
        self.assertEqual("secret", credential_service.unprotect_text(reference))
        self.assertNotIn("secret", reference)

    def test_delete_removes_keychain_item(self) -> None:
        reference = credential_service.protect_text("secret", "site:7:password")

        credential_service.delete_protected_text(reference)

        with self.assertRaises(credential_service.CredentialStoreError):
            credential_service.unprotect_text(reference)


class SystemMetricsTests(unittest.TestCase):
    def test_cross_platform_metrics_have_expected_shape(self) -> None:
        self.assertIn("usage_percent", cpu_metrics())
        self.assertGreater(memory_metrics()["total_gb"], 0)
        self.assertGreater(disk_metrics()["total_gb"], 0)


if __name__ == "__main__":
    unittest.main()

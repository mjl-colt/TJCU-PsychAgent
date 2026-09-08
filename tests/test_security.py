import hashlib
import unittest

from app.core.security import hash_password, password_hash_needs_upgrade, verify_password


class PasswordSecurityTests(unittest.TestCase):
    def test_password_hash_is_salted_and_verifiable(self):
        first = hash_password("student123", iterations=10_000)
        second = hash_password("student123", iterations=10_000)
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("student123", first))
        self.assertFalse(verify_password("wrong", first))

    def test_legacy_sha256_is_accepted_for_login_upgrade(self):
        legacy = hashlib.sha256(b"student123").hexdigest()
        self.assertTrue(verify_password("student123", legacy))
        self.assertTrue(password_hash_needs_upgrade(legacy))

    def test_excessively_long_password_is_rejected(self):
        self.assertFalse(verify_password("x" * 2000, "not-a-hash"))


if __name__ == "__main__":
    unittest.main()

import unittest
from types import SimpleNamespace

from app.services.rate_limit import DistributedRateLimiter


class RateLimitTests(unittest.TestCase):
    def test_local_fallback_enforces_limit(self):
        settings = SimpleNamespace(
            redis_url="redis://127.0.0.1:1/0",
            redis_socket_timeout_seconds=0.01,
        )
        limiter = DistributedRateLimiter(settings)
        subject = f"rate-limit-test-{id(self)}"
        self.assertTrue(limiter.allow("test", subject, 2))
        self.assertTrue(limiter.allow("test", subject, 2))
        self.assertFalse(limiter.allow("test", subject, 2))


if __name__ == "__main__":
    unittest.main()

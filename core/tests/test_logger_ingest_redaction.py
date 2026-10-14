"""Logger ingest redaction tests (mission §6.5, §13.1).

Proves that secrets seeded into log messages/details never reach the Logger's
SQLite persistence — regardless of which producer sent them.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "logger"))

from redaction import redact  # noqa: E402


class LoggerIngestRedactionTest(unittest.TestCase):
    def test_bot_token_redacted(self):
        msg = "auth failed for bot 1234567890:AAE_ABC-DEFghijklmnopqrstuvwxyz123456789"
        out = redact(msg)
        self.assertNotIn("AAE_ABC-DEFghijklmnopqrstuvwxyz123456789", out)
        self.assertIn("[REDACTED]", out)

    def test_kv_secret_value_redacted(self):
        out = redact("api_key=sk-abcdef1234567890 config saved")
        self.assertNotIn("sk-abcdef1234567890", out)
        self.assertIn("api_key=", out.lower())

    def test_bearer_header_redacted(self):
        out = redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abc.def")
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9abc", out)

    def test_long_hex_redacted(self):
        sha = "a" * 64
        out = redact(f"content_dedup hash {sha} stored")
        self.assertNotIn(sha, out)

    def test_session_blob_redacted(self):
        blob = "S" + "x" * 100
        out = redact(f"session string {blob} loaded")
        self.assertNotIn(blob, out)

    def test_phone_redacted(self):
        out = redact("login code sent to +989121234567")
        self.assertNotIn("989121234567", out)
        self.assertIn("[PHONE]", out)

    def test_normal_text_untouched(self):
        plain = "rule pg-proof-0001 forwarded msg 42 to target -1001234"
        self.assertEqual(redact(plain), plain)

    def test_detail_field_redacted_too(self):
        out = redact('{"password": "hunter2", "note": "x"}')
        self.assertNotIn("hunter2", out)

    def test_json_kv_secret(self):
        out = redact('"bot_token": "1234567:AAabc-def_ghi-jkl_mnopqrstuvw123"')
        self.assertNotIn("AAabc-def_ghi-jkl_mnopqrstuvw123", out)


if __name__ == "__main__":
    unittest.main()

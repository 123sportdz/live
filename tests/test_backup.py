from __future__ import annotations

import unittest

from live_relay.backup import build_export, validate_import

CONFIG = ("BRAND_NAME", "LIVE_PLATFORMS", "SEO_REWRITE")
SECRETS = ("GEMINI_API_KEY", "YOUTUBE_STREAM_KEY")


class ExportTests(unittest.TestCase):
    def test_default_export_has_no_secrets(self):
        payload = build_export(
            {
                "BRAND_NAME": "Oussama News",
                "LIVE_PLATFORMS": "youtube",
                "SEO_REWRITE": "true",
                "GEMINI_API_KEY": "very-secret-key",
            },
            config_keys=CONFIG,
            secret_keys=SECRETS,
            version="4.14.0",
            now=1000,
        )
        self.assertEqual(payload["app"], "live-relay-config")
        self.assertEqual(payload["schema"], 1)
        self.assertFalse(payload["secrets_included"])
        self.assertNotIn("secrets", payload)
        self.assertEqual(payload["config"]["BRAND_NAME"], "Oussama News")
        self.assertNotIn("very-secret-key", str(payload))

    def test_secrets_are_opt_in(self):
        payload = build_export(
            {"BRAND_NAME": "x", "GEMINI_API_KEY": "very-secret-key"},
            config_keys=CONFIG,
            secret_keys=SECRETS,
            version="4.14.0",
            include_secrets=True,
        )
        self.assertTrue(payload["secrets_included"])
        self.assertEqual(payload["secrets"]["GEMINI_API_KEY"], "very-secret-key")

    def test_missing_values_become_empty_strings(self):
        payload = build_export(
            {}, config_keys=CONFIG, secret_keys=SECRETS, version="4.14.0"
        )
        self.assertEqual(payload["config"]["BRAND_NAME"], "")


class ImportTests(unittest.TestCase):
    def test_valid_payload_is_cleaned(self):
        config, secrets = validate_import(
            {
                "app": "live-relay-config",
                "config": {"BRAND_NAME": "  Oussama  ", "SEO_REWRITE": True},
                "secrets": {"GEMINI_API_KEY": "long-enough-key"},
            },
            config_keys=CONFIG,
            secret_keys=SECRETS,
        )
        self.assertEqual(config["BRAND_NAME"], "Oussama")
        self.assertEqual(config["SEO_REWRITE"], "true")
        self.assertEqual(secrets["GEMINI_API_KEY"], "long-enough-key")

    def test_unknown_keys_reject_the_whole_file(self):
        with self.assertRaisesRegex(ValueError, "غير معروفة"):
            validate_import(
                {"config": {"BRAND_NAME": "x", "EVIL_KEY": "y"}},
                config_keys=CONFIG,
                secret_keys=SECRETS,
            )

    def test_secret_keys_are_not_accepted_in_config(self):
        with self.assertRaisesRegex(ValueError, "غير معروفة"):
            validate_import(
                {"config": {"GEMINI_API_KEY": "long-enough-key"}},
                config_keys=CONFIG,
                secret_keys=SECRETS,
            )

    def test_newlines_and_short_secrets_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "سطر"):
            validate_import(
                {"config": {"BRAND_NAME": "a\nb"}},
                config_keys=CONFIG,
                secret_keys=SECRETS,
            )
        with self.assertRaisesRegex(ValueError, "قصيرة"):
            validate_import(
                {"secrets": {"GEMINI_API_KEY": "abc"}},
                config_keys=CONFIG,
                secret_keys=SECRETS,
            )

    def test_wrong_or_empty_documents_are_rejected(self):
        for payload in ([], "text", {}, {"config": {}, "secrets": {}}, {"app": "other", "config": {"BRAND_NAME": "x"}}):
            with self.assertRaises(ValueError):
                validate_import(
                    payload, config_keys=CONFIG, secret_keys=SECRETS
                )


if __name__ == "__main__":
    unittest.main()

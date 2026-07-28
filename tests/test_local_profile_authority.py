from __future__ import annotations

import unittest

from bbw_prod.services import (
    WEB_LOCAL_PROFILE_FIELDS_KEY,
    WEB_LOCAL_PROFILE_UPDATED_AT_KEY,
    merge_provider_profile_preserving_local,
    sanitize_provider_profile,
)


class LocalProfileAuthorityTests(unittest.TestCase):
    def test_provider_refreshes_fields_not_owned_by_web(self) -> None:
        merged = merge_provider_profile_preserving_local(
            {"nickname": "旧昵称", "age": 20},
            {"nickname": "上游昵称", "age": 21, "property": "B"},
        )

        self.assertEqual(merged["nickname"], "上游昵称")
        self.assertEqual(merged["age"], 21)
        self.assertEqual(merged["property"], "B")

    def test_provider_cannot_overwrite_local_owned_fields(self) -> None:
        merged = merge_provider_profile_preserving_local(
            {
                "nickname": "网页昵称",
                "city": "上海",
                "age": 20,
                WEB_LOCAL_PROFILE_FIELDS_KEY: ["nickname", "city"],
                WEB_LOCAL_PROFILE_UPDATED_AT_KEY: "2026-07-25T00:00:00+00:00",
            },
            {"nickname": "旧上游昵称", "city": "北京", "age": 21},
        )

        self.assertEqual(merged["nickname"], "网页昵称")
        self.assertEqual(merged["city"], "上海")
        self.assertEqual(merged["age"], 21)
        self.assertEqual(
            merged[WEB_LOCAL_PROFILE_FIELDS_KEY], ["city", "nickname"]
        )
        self.assertEqual(
            merged[WEB_LOCAL_PROFILE_UPDATED_AT_KEY],
            "2026-07-25T00:00:00+00:00",
        )

    def test_cleared_local_field_stays_cleared(self) -> None:
        merged = merge_provider_profile_preserving_local(
            {
                "signature": "",
                WEB_LOCAL_PROFILE_FIELDS_KEY: ["signature"],
            },
            {"signature": "上游旧签名"},
        )

        self.assertEqual(merged["signature"], "")

    def test_sensitive_profile_keys_are_omitted_recursively(self) -> None:
        sanitized = sanitize_provider_profile(
            {
                "nickname": "保留",
                "phone": "[REDACTED]",
                "uniqueLoginToken_local": "secret",
                "client_ip": "203.0.113.10",
                "device-id": "device-secret",
                "nested": {
                    "accessToken": "token-secret",
                    "signature": "保留签名",
                },
            }
        )

        self.assertEqual(
            sanitized,
            {"nickname": "保留", "nested": {"signature": "保留签名"}},
        )

    def test_location_profile_fields_remain_available_to_product_features(self) -> None:
        sanitized = sanitize_provider_profile(
            {"latitude": 26.08, "longitude": 119.30, "city": "福州"}
        )

        self.assertEqual(sanitized["latitude"], 26.08)
        self.assertEqual(sanitized["longitude"], 119.30)
        self.assertEqual(sanitized["city"], "福州")

    def test_provider_refresh_cannot_restore_sensitive_profile_keys(self) -> None:
        merged = merge_provider_profile_preserving_local(
            {"nickname": "旧昵称", "token": "[REDACTED]"},
            {
                "nickname": "新昵称",
                "password": "[REDACTED]",
                "pushregid": "push-secret",
            },
        )

        self.assertEqual(merged, {"nickname": "新昵称"})


if __name__ == "__main__":
    unittest.main()

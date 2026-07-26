from __future__ import annotations

import unittest

from bbw_prod.services import (
    WEB_LOCAL_PROFILE_FIELDS_KEY,
    WEB_LOCAL_PROFILE_UPDATED_AT_KEY,
    merge_provider_profile_preserving_local,
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


if __name__ == "__main__":
    unittest.main()

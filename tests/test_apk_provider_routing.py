from __future__ import annotations

import unittest

from bbw_web import api
from bbw_web.native_discovery_api import DISCOVERY_NATIVE_PATHS
from bbw_web.native_social_api import (
    HANDLED_PATHS as SOCIAL_NATIVE_PATHS,
    SOCIAL_NATIVE_WRITE_PATHS,
)


class ApkProviderRoutingTests(unittest.TestCase):
    def test_all_discovery_and_match_routes_use_apk_compatible_bff(self) -> None:
        methods = {
            "/api/match/status": "GET",
            "/api/match/online-users": "GET",
            "/api/match/nearby-users": "GET",
            "/api/match/online": "POST",
            "/api/match/local": "POST",
        }
        self.assertEqual(set(methods), set(DISCOVERY_NATIVE_PATHS))
        for path, method in methods.items():
            with self.subTest(method=method, path=path):
                self.assertFalse(
                    api._use_native_discovery_route(
                        path,
                        method,
                        DISCOVERY_NATIVE_PATHS,
                    )
                )

    def test_apk_backed_profile_and_relationship_routes_bypass_native(self) -> None:
        self.assertTrue(set(api.APK_SOCIAL_ROUTE_PATHS) <= set(SOCIAL_NATIVE_PATHS))
        for path in sorted(SOCIAL_NATIVE_PATHS):
            method = "POST" if path in SOCIAL_NATIVE_WRITE_PATHS else "GET"
            with self.subTest(method=method, path=path):
                self.assertFalse(
                    api._use_native_social_route(
                        path,
                        method,
                        SOCIAL_NATIVE_PATHS,
                        {},
                    )
                )

    def test_r2_avatar_payload_does_not_reenable_native_social_authority(self) -> None:
        self.assertFalse(
            api._use_native_social_route(
                "/api/profile/reset",
                "POST",
                SOCIAL_NATIVE_PATHS,
                {"type": "头像设置", "avatar_asset_id": "asset-1"},
            )
        )
        self.assertFalse(
            api._use_native_social_route(
                "/api/profile/reset",
                "POST",
                SOCIAL_NATIVE_PATHS,
                {"type": "昵称设置", "value": "新昵称"},
            )
        )


if __name__ == "__main__":
    unittest.main()

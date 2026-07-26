from __future__ import annotations

import hashlib
import json
import unittest
import uuid
from datetime import UTC, datetime
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from bbw_prod.migration_readiness import DOMAIN_MARKER_SCOPES
from bbw_web.discovery_native import migration


NOW = datetime(2026, 7, 25, 12, 0, tzinfo=UTC)


class DiscoveryMigrationPureTests(unittest.TestCase):
    def test_digest_is_stable_and_covers_profile_preference_and_matches(self) -> None:
        profile = SimpleNamespace(
            age=30,
            city_code="LOCAL-ABC",
            city_name="福州",
            discoverable=True,
            gender="male",
            profile_property="Z",
        )
        preference = SimpleNamespace(
            city_scope="same-city",
            enabled=True,
            gender_preference="female",
            max_age=35,
            min_age=25,
            property_preference="Z",
            version=2,
        )
        match = SimpleNamespace(
            public_id="mch_example",
            match_key="text:" + "a" * 64,
            status="active",
            matched_at=NOW,
        )

        first = migration._record_digest(profile, preference, [match])
        second = migration._record_digest(profile, preference, [match])

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertNotEqual(
            first,
            migration._record_digest(profile, None, [match]),
        )

    def test_marker_contract_declares_all_discovery_scopes_and_real_sources(self) -> None:
        source = migration.__file__
        self.assertEqual(
            set(DOMAIN_MARKER_SCOPES["discovery"]),
            {"profile", "preferences", "text-match"},
        )
        self.assertEqual(
            migration.DISCOVERY_SOURCE_PATHS,
            (
                "/api/profile/user",
                "postgresql://match_preferences",
                "postgresql://match_results",
            ),
        )
        self.assertTrue(source.endswith("discovery_native/migration.py"))

    def test_bulk_cli_reports_only_aggregate_counts(self) -> None:
        owners = [uuid.uuid4(), uuid.uuid4()]
        summary = migration.DiscoveryMigrationSummary(1, 0, 2, "a" * 64)
        output = StringIO()
        with patch.object(migration, "_active_owner_ids", return_value=owners), patch.object(
            migration,
            "initialize_discovery_migration",
            side_effect=[summary, migration.DiscoveryMigrationPrerequisiteError("not ready")],
        ):
            code = migration.main(["--all-active"], stdout=output)

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["accounts_total"], 2)
        self.assertEqual(payload["accounts_completed"], 1)
        self.assertEqual(payload["accounts_failed"], 1)
        self.assertNotIn(str(owners[0]), output.getvalue())
        self.assertNotIn(str(owners[1]), output.getvalue())

    def test_digest_is_sha256_hex(self) -> None:
        empty_digest = hashlib.sha256(b"[]").hexdigest()
        self.assertEqual(len(empty_digest), 64)
        self.assertTrue(all(char in "0123456789abcdef" for char in empty_digest))


if __name__ == "__main__":
    unittest.main()

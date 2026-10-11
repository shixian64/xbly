"""Offline behavior regressions for the production browser functions."""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path


class FrontendRuntimeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend runtime tests")
    def test_browser_session_list_receipt_and_csv_regressions(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [shutil.which("node"), "--test", str(root / "tests" / "frontend_runtime.test.cjs")],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

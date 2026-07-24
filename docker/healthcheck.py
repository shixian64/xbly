"""Small stdlib-only health probe used by the app container."""

from __future__ import annotations

import http.client
import json
import os
import sys
from pathlib import Path


def request(
    path: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=3)
    try:
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.read(4096)
    except OSError:
        return 0, b""
    finally:
        connection.close()


def deployment_control_token() -> str:
    direct = str(os.getenv("BBW_DEPLOYMENT_CONTROL_TOKEN") or "").strip()
    token_file = str(os.getenv("BBW_DEPLOYMENT_CONTROL_TOKEN_FILE") or "").strip()
    if direct and token_file:
        return ""
    if token_file:
        try:
            return Path(token_file).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return direct


def main() -> int:
    action = str(sys.argv[1] if len(sys.argv) > 1 else "check").strip().lower()
    if action in {"drain", "resume"}:
        token = deployment_control_token()
        if not token:
            print("deployment control token unavailable", file=sys.stderr)
            return 1
        status, body = request(
            f"/internal/{action}",
            method="POST",
            headers={"Authorization": f"Bearer {token}"},
        )
        if body:
            try:
                print(json.dumps(json.loads(body), ensure_ascii=False))
            except (UnicodeDecodeError, json.JSONDecodeError):
                print(body.decode("utf-8", errors="replace"))
        return 0 if 200 <= status < 300 else 1
    status, _body = request("/readyz")
    return 0 if 200 <= status < 300 else 1


if __name__ == "__main__":
    sys.exit(main())

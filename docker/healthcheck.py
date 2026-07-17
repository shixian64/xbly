"""Small stdlib-only health probe used by the app container."""

from __future__ import annotations

import http.client
import sys


def main() -> int:
    connection = http.client.HTTPConnection("127.0.0.1", 8000, timeout=3)
    try:
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        response.read(4096)
        return 0 if 200 <= response.status < 300 else 1
    except OSError:
        return 1
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())

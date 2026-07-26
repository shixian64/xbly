"""受控建立 Web 用户本地密码摘要。

默认只演练。只有显式传入 ``--apply`` 才写入 ``user_credentials``；命令永远
不会删除或改写 ``external_accounts.password_encrypted``。
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Sequence, TextIO

from .config import get_settings
from .crypto import CredentialCipher
from .db import session_scope
from .services import CredentialBackfillReport, CredentialBackfillService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从现有上游密码密文建立独立 Web 用户密码摘要（默认 dry-run）"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="显式解密并写入 user_credentials；未提供时只统计候选",
    )
    parser.add_argument(
        "--provider",
        default="beibeiwu",
        help="只处理指定上游 provider（默认 beibeiwu）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="本次最多扫描的上游账号数",
    )
    parser.add_argument(
        "--approved-account-ids-file",
        default=None,
        help=(
            "仅处理审核通过的 ExternalAccount UUID 清单；每行一个 UUID。"
            "使用 - 可从标准输入读取。--apply 时必填"
        ),
    )
    return parser


def _approved_account_ids(
    source: str | None,
    *,
    stdin: TextIO,
) -> set[uuid.UUID] | None:
    if source is None:
        return None
    source_name = str(source or "").strip()
    if not source_name:
        raise SystemExit("--approved-account-ids-file must not be empty")
    if source_name == "-":
        raw = stdin.read(1024 * 1024 + 1)
    else:
        path = Path(source_name)
        try:
            if path.stat().st_size > 1024 * 1024:
                raise SystemExit("approved account ID file must not exceed 1 MiB")
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SystemExit("approved account ID file could not be read") from exc
    if len(raw.encode("utf-8")) > 1024 * 1024:
        raise SystemExit("approved account ID file must not exceed 1 MiB")

    approved: set[uuid.UUID] = set()
    for line_number, line in enumerate(raw.splitlines(), start=1):
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        try:
            approved.add(uuid.UUID(value))
        except ValueError as exc:
            raise SystemExit(
                f"approved account ID file contains an invalid UUID at line {line_number}"
            ) from exc
    if not approved:
        raise SystemExit("approved account ID file must contain at least one UUID")
    return approved


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stdin: TextIO | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be a positive integer")
    if args.apply and args.limit is None:
        raise SystemExit("--apply requires an explicit --limit")
    if args.apply and args.limit > CredentialBackfillService.MAX_APPLY_LIMIT:
        raise SystemExit(
            "--apply --limit must not exceed "
            f"{CredentialBackfillService.MAX_APPLY_LIMIT}"
        )
    if args.apply and not args.approved_account_ids_file:
        raise SystemExit("--apply requires --approved-account-ids-file")
    provider = str(args.provider or "").strip()
    if not provider or len(provider) > 40:
        raise SystemExit("--provider must contain 1 to 40 characters")
    approved_account_ids = _approved_account_ids(
        args.approved_account_ids_file,
        stdin=stdin or sys.stdin,
    )

    settings = get_settings()
    cipher = CredentialCipher.from_settings(settings) if args.apply else None
    if args.apply:
        assert approved_account_ids is not None
        reports: list[CredentialBackfillReport] = []
        for account_id in sorted(approved_account_ids, key=str)[: args.limit]:
            # Each Argon2 enrollment owns a short transaction. Row locks are
            # released before the next approved account is processed.
            with session_scope() as db:
                reports.append(
                    CredentialBackfillService(db, cipher).run(
                        apply=True,
                        provider=provider,
                        limit=1,
                        approved_account_ids={account_id},
                    )
                )
        report = CredentialBackfillReport(
            dry_run=False,
            scanned=sum(item.scanned for item in reports),
            eligible=sum(item.eligible for item in reports),
            created=sum(item.created for item in reports),
            skipped_existing=sum(item.skipped_existing for item in reports),
            failed=sum(item.failed for item in reports),
        )
    else:
        with session_scope() as db:
            report = CredentialBackfillService(db, cipher).run(
                apply=False,
                provider=provider,
                limit=args.limit,
                approved_account_ids=approved_account_ids,
            )

    payload = {
        **asdict(report),
        "provider": provider,
        "upstream_password_ciphertext_retained": True,
    }
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        file=stdout or sys.stdout,
    )
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

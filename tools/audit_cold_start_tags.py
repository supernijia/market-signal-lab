#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit cold-start tag persistence across pending, positions, and transactions."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime
from typing import Any


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.portfolio import PortfolioManager  # noqa: E402


COLD_TAG_PREFIXES = (
    "COLD_START",
    "ENTRY_SCENARIO",
    "ENTRY_CONFIRM",
)


def _parse_json_payload(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple)):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return text


def _extract_tags(value: Any) -> list[str]:
    parsed = _parse_json_payload(value)
    tags: list[str] = []

    def add(raw: Any) -> None:
        text = str(raw or "").strip()
        if text:
            tags.append(text[:120])

    def walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, str):
            text = node.strip()
            if not text:
                return
            if text.startswith("[") or text.startswith("{"):
                reparsed = _parse_json_payload(text)
                if reparsed is not text:
                    walk(reparsed)
                    return
            add(text)
            return
        if isinstance(node, dict):
            for key in ("tag", "name", "type", "label"):
                if node.get(key):
                    add(node.get(key))
                    break
            for key in (
                "tags",
                "risk_tags",
                "gate_tags",
                "confirmations",
                "warnings",
                "cold_start_model_tags",
                "entry_confirmations",
            ):
                if key in node:
                    walk(node.get(key))
            for key in ("base_tags_json", "tags_json", "signal_tags_json", "entry_tags_json"):
                if key in node:
                    walk(node.get(key))
            return
        if isinstance(node, (list, tuple, set)):
            for item in node:
                walk(item)

    walk(parsed)
    return sorted(set(tags))


def _cold_tags(value: Any) -> list[str]:
    tags = _extract_tags(value)
    return [tag for tag in tags if tag.startswith(COLD_TAG_PREFIXES)]


def _fetch_rows(pm: PortfolioManager, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
    conn = pm._get_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall() or []
    finally:
        conn.close()


def _audit_pending(pm: PortfolioManager, trade_date: str) -> dict[str, Any]:
    rows = _fetch_rows(
        pm,
        """
        SELECT id, trade_date, code, name, source_strategy, status, payload_json, last_reason, updated_at
        FROM pending_entry_signals
        WHERE trade_date=%s
        ORDER BY updated_at DESC, id DESC
        """,
        (trade_date,),
    )
    audited = []
    tag_counter: Counter[str] = Counter()
    with_base = 0
    cold_rows = 0
    for row in rows:
        payload = _parse_json_payload(row.get("payload_json")) or {}
        tags = _cold_tags(payload)
        tag_counter.update(tags)
        if isinstance(payload, dict) and payload.get("base_tags_json"):
            with_base += 1
        if tags or str(row.get("source_strategy") or "") == "冷启动":
            cold_rows += 1
        audited.append(
            {
                "id": row.get("id"),
                "code": row.get("code"),
                "name": row.get("name"),
                "strategy": row.get("source_strategy"),
                "status": row.get("status"),
                "has_base_tags_json": bool(isinstance(payload, dict) and payload.get("base_tags_json")),
                "cold_tags": tags,
                "last_reason": row.get("last_reason"),
            }
        )
    return {
        "total": len(rows),
        "cold_rows": cold_rows,
        "with_base_tags_json": with_base,
        "tag_counts": dict(tag_counter),
        "rows": audited,
    }


def _audit_positions(pm: PortfolioManager) -> dict[str, Any]:
    rows = _fetch_rows(
        pm,
        """
        SELECT code, account, name, entry_strategy, entry_tags_json, created_at, update_time
        FROM positions
        ORDER BY update_time DESC
        """,
        (),
    )
    audited = []
    tag_counter: Counter[str] = Counter()
    cold_rows = 0
    for row in rows:
        tags = _cold_tags(row.get("entry_tags_json"))
        tag_counter.update(tags)
        if tags or str(row.get("entry_strategy") or "") == "冷启动":
            cold_rows += 1
        audited.append(
            {
                "code": row.get("code"),
                "account": row.get("account"),
                "name": row.get("name"),
                "entry_strategy": row.get("entry_strategy"),
                "cold_tags": tags,
                "update_time": str(row.get("update_time") or ""),
            }
        )
    return {
        "total": len(rows),
        "cold_rows": cold_rows,
        "tag_counts": dict(tag_counter),
        "rows": audited,
    }


def _audit_transactions(pm: PortfolioManager, trade_date: str) -> dict[str, Any]:
    rows = _fetch_rows(
        pm,
        """
        SELECT id, date, account, type, code, name, source_strategy, signal_tags_json, reason
        FROM transactions
        WHERE DATE(date)=%s
        ORDER BY date DESC, id DESC
        """,
        (trade_date,),
    )
    audited = []
    tag_counter: Counter[str] = Counter()
    cold_rows = 0
    for row in rows:
        tags = _cold_tags(row.get("signal_tags_json"))
        tag_counter.update(tags)
        if tags or str(row.get("source_strategy") or "") == "冷启动":
            cold_rows += 1
        audited.append(
            {
                "id": row.get("id"),
                "date": str(row.get("date") or ""),
                "account": row.get("account"),
                "type": row.get("type"),
                "code": row.get("code"),
                "name": row.get("name"),
                "source_strategy": row.get("source_strategy"),
                "cold_tags": tags,
                "reason": row.get("reason"),
            }
        )
    return {
        "total": len(rows),
        "cold_rows": cold_rows,
        "tag_counts": dict(tag_counter),
        "rows": audited,
    }


def _print_section(title: str, audit: dict[str, Any], row_limit: int) -> None:
    print(f"\n=== {title} ===")
    print(f"total={audit['total']} cold_rows={audit['cold_rows']}")
    if "with_base_tags_json" in audit:
        print(f"with_base_tags_json={audit['with_base_tags_json']}")
    if audit.get("tag_counts"):
        print("tag_counts:")
        for tag, count in sorted(audit["tag_counts"].items(), key=lambda item: (-item[1], item[0])):
            print(f"  {tag}: {count}")
    for row in audit.get("rows", [])[:row_limit]:
        tags = ",".join(row.get("cold_tags") or [])
        print(
            f"- {row.get('code')} {row.get('name')} "
            f"strategy={row.get('strategy') or row.get('entry_strategy') or row.get('source_strategy')} "
            f"status={row.get('status') or row.get('type') or ''} tags=[{tags}]"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit cold-start tag persistence")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="Trade date YYYY-MM-DD")
    parser.add_argument("--json", action="store_true", help="Print JSON payload")
    parser.add_argument("--row-limit", type=int, default=20)
    args = parser.parse_args()

    trade_date = str(args.date)
    if len(trade_date) == 8 and trade_date.isdigit():
        trade_date = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"

    pm = PortfolioManager()
    result = {
        "trade_date": trade_date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "pending": _audit_pending(pm, trade_date),
        "positions": _audit_positions(pm),
        "transactions": _audit_transactions(pm, trade_date),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    print(f"Cold-start tag audit for {trade_date}")
    _print_section("pending_entry_signals", result["pending"], args.row_limit)
    _print_section("positions", result["positions"], args.row_limit)
    _print_section("transactions", result["transactions"], args.row_limit)


if __name__ == "__main__":
    main()

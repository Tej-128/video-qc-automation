from __future__ import annotations

import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

MONDAY_API_URL = "https://api.monday.com/v2"
BOARD_ID = "1662638864"
PAGE_LIMIT = 500

COLUMN_IDS = {
    "release_date": "date_145",
    "scriptwriter": "people",
    "draft_script_sent": "date3",
    "final_script_to_author": "date_2",
    "frameio_review_link": "link_1",
    "science_video_editor": "people_11",
    "finishing_editor": "people5",
    "rough_editor": "people_1",
}

FIRST_PAGE_QUERY = """
query MonthlyQcFirstPage($boardId: ID!, $limit: Int!) {
  boards(ids: [$boardId]) {
    id
    name
    items_page(limit: $limit) {
      cursor
      items {
        id
        name
        group { id title }
        column_values(ids: [
          "date_145",
          "people",
          "date3",
          "date_2",
          "link_1",
          "people_11",
          "people5",
          "people_1"
        ]) {
          id
          text
          value
        }
      }
    }
  }
}
"""

NEXT_PAGE_QUERY = """
query MonthlyQcNextPage($cursor: String!, $limit: Int!) {
  next_items_page(cursor: $cursor, limit: $limit) {
    cursor
    items {
      id
      name
      group { id title }
      column_values(ids: [
        "date_145",
        "people",
        "date3",
        "date_2",
        "link_1",
        "people_11",
        "people5",
        "people_1"
      ]) {
        id
        text
        value
      }
    }
  }
}
"""


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def post_graphql(token: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    response = requests.post(
        MONDAY_API_URL,
        json={"query": query, "variables": variables},
        headers={
            "Authorization": token,
            "Content-Type": "application/json",
            "API-Version": "2026-07",
        },
        timeout=60,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"], indent=2, ensure_ascii=False))
    return payload


def parse_month(raw: str) -> tuple[int, int]:
    try:
        dt = datetime.strptime(raw, "%Y-%m")
    except ValueError as exc:
        raise ValueError("REPORT_MONTH must be YYYY-MM, for example 2026-08.") from exc
    return dt.year, dt.month


def parse_date(text: str) -> datetime | None:
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%b %d", "%m/%d/%Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == "%b %d":
                parsed = parsed.replace(year=datetime.now().year)
            return parsed
        except ValueError:
            continue
    return None


def values_by_id(item: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {cv["id"]: cv for cv in item.get("column_values") or []}


def link_url(cv: dict[str, Any] | None) -> str:
    if not cv:
        return ""
    raw_value = cv.get("value")
    if raw_value:
        try:
            parsed = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
            if isinstance(parsed, dict) and parsed.get("url"):
                return str(parsed["url"]).strip()
        except (json.JSONDecodeError, TypeError):
            pass
    text = (cv.get("text") or "").strip()
    return text if text.startswith(("http://", "https://")) else ""


def article_id_from_name(name: str) -> str:
    match = re.match(r"^\s*(\d+)(?:\D|$)", name or "")
    return match.group(1) if match else ""


def fetch_all_items(token: str) -> tuple[str, list[dict[str, Any]]]:
    first = post_graphql(
        token,
        FIRST_PAGE_QUERY,
        {"boardId": BOARD_ID, "limit": PAGE_LIMIT},
    )
    boards = (first.get("data") or {}).get("boards") or []
    if not boards:
        raise RuntimeError(f"Board {BOARD_ID} was not returned.")

    board = boards[0]
    page = board.get("items_page") or {}
    items = list(page.get("items") or [])
    cursor = page.get("cursor")

    while cursor:
        nxt = post_graphql(
            token,
            NEXT_PAGE_QUERY,
            {"cursor": cursor, "limit": PAGE_LIMIT},
        )
        next_page = (nxt.get("data") or {}).get("next_items_page") or {}
        items.extend(next_page.get("items") or [])
        cursor = next_page.get("cursor")

    return board.get("name") or "", items


def normalize(item: dict[str, Any]) -> dict[str, str]:
    cvs = values_by_id(item)

    def text(column_key: str) -> str:
        cv = cvs.get(COLUMN_IDS[column_key]) or {}
        return (cv.get("text") or "").strip()

    return {
        "monday_item_id": str(item.get("id") or ""),
        "article_id": article_id_from_name(item.get("name") or ""),
        "item_name": item.get("name") or "",
        "group": ((item.get("group") or {}).get("title") or ""),
        "date_video_released": text("release_date"),
        "scriptwriter": text("scriptwriter"),
        "draft_script_sent": text("draft_script_sent"),
        "final_script_to_author": text("final_script_to_author"),
        "frameio_review_link": link_url(cvs.get(COLUMN_IDS["frameio_review_link"])),
        "science_video_editor": text("science_video_editor"),
        "finishing_editor": text("finishing_editor"),
        "rough_editor": text("rough_editor"),
    }


def main() -> int:
    token = require_env("MONDAY_API_TOKEN")
    report_month = require_env("REPORT_MONTH")
    target_year, target_month = parse_month(report_month)

    board_name, items = fetch_all_items(token)
    normalized = [normalize(item) for item in items]

    selected: list[dict[str, str]] = []
    unparseable_release_dates: list[dict[str, str]] = []

    for row in normalized:
        raw_date = row["date_video_released"]
        if not raw_date:
            continue

        parsed = parse_date(raw_date)
        if parsed is None:
            unparseable_release_dates.append(row)
            continue

        # Monday date text can omit year in some UI representations; when it does,
        # use the selected report year for the month comparison.
        if parsed.year == datetime.now().year and len(raw_date.split()) == 2 and "," not in raw_date:
            parsed = parsed.replace(year=target_year)

        if parsed.year == target_year and parsed.month == target_month:
            selected.append(row)

    selected.sort(key=lambda r: (r["date_video_released"], r["article_id"], r["item_name"]))

    missing_article_id = [r for r in selected if not r["article_id"]]
    missing_frameio = [r for r in selected if not r["frameio_review_link"]]
    missing_scriptwriter = [r for r in selected if not r["scriptwriter"]]

    out_dir = Path("artifacts")
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / f"monday_monthly_qc_{report_month}.json"
    csv_path = out_dir / f"monday_monthly_qc_{report_month}.csv"

    payload = {
        "mode": "READ_ONLY",
        "board_id": BOARD_ID,
        "board_name": board_name,
        "report_month": report_month,
        "total_board_items_scanned": len(items),
        "selected_project_count": len(selected),
        "missing_article_id_count": len(missing_article_id),
        "missing_frameio_link_count": len(missing_frameio),
        "missing_scriptwriter_count": len(missing_scriptwriter),
        "unparseable_release_date_count": len(unparseable_release_dates),
        "projects": selected,
    }
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    fieldnames = [
        "monday_item_id",
        "article_id",
        "item_name",
        "group",
        "date_video_released",
        "scriptwriter",
        "draft_script_sent",
        "final_script_to_author",
        "frameio_review_link",
        "science_video_editor",
        "finishing_editor",
        "rough_editor",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(selected)

    print("=" * 76)
    print("VIDEO QC AUTOMATION - MONTHLY MONDAY EXTRACTION")
    print("=" * 76)
    print(f"Mode                     : READ ONLY")
    print(f"Board                    : {BOARD_ID} | {board_name}")
    print(f"Report month             : {report_month}")
    print(f"Board items scanned      : {len(items)}")
    print(f"Projects selected        : {len(selected)}")
    print(f"Missing Article ID       : {len(missing_article_id)}")
    print(f"Missing Frame.io link    : {len(missing_frameio)}")
    print(f"Missing Scriptwriter     : {len(missing_scriptwriter)}")
    print(f"Unparseable release date : {len(unparseable_release_dates)}")
    print(f"JSON                     : {json_path}")
    print(f"CSV                      : {csv_path}")
    print("Writes to Monday         : NONE")
    print("=" * 76)

    print("\nSELECTED PROJECTS")
    for row in selected:
        print(
            f"- {row['article_id'] or '[NO-ID]'} | {row['item_name']} | "
            f"released={row['date_video_released']} | "
            f"writer={row['scriptwriter'] or '[blank]'} | "
            f"frameio={'YES' if row['frameio_review_link'] else 'NO'}"
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise

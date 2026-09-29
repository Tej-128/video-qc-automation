from __future__ import annotations

import calendar
import os
import sys
from datetime import date
from typing import Any

import requests

MONDAY_API_URL = "https://api.monday.com/v2"
BOARD_ID = "1662638864"

COLUMN_IDS = {
    "release_date": "date_145",
    "scriptwriter": "people",
    "draft_script_sent": "date3",
    "frameio_link": "link_1",
    "science_video_editor": "people_11",
    "finishing_editor": "people5",
    "rough_editor": "people_1",
}

PAGE_QUERY = """
query MonthlyItems($boardId: ID!, $limit: Int!) {
  boards(ids: [$boardId]) {
    id
    name
    items_page(limit: $limit) {
      cursor
      items {
        id
        name
        column_values(ids: [
          "date_145",
          "people",
          "date3",
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

NEXT_QUERY = """
query MonthlyItemsNext($cursor: String!, $limit: Int!) {
  next_items_page(cursor: $cursor, limit: $limit) {
    cursor
    items {
      id
      name
      column_values(ids: [
        "date_145",
        "people",
        "date3",
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
        raise RuntimeError(str(payload["errors"]))
    return payload


def column_map(item: dict[str, Any]) -> dict[str, str]:
    return {
        c.get("id", ""): (c.get("text") or "").strip()
        for c in item.get("column_values", [])
    }


def parse_date(text: str) -> date | None:
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def fetch_all_items(token: str) -> tuple[str, list[dict[str, Any]]]:
    first = post_graphql(token, PAGE_QUERY, {"boardId": BOARD_ID, "limit": 500})
    boards = (first.get("data") or {}).get("boards") or []
    if not boards:
        raise RuntimeError(f"Board {BOARD_ID} not returned.")

    board = boards[0]
    page = board.get("items_page") or {}
    items = list(page.get("items") or [])
    cursor = page.get("cursor")

    while cursor:
        nxt = post_graphql(token, NEXT_QUERY, {"cursor": cursor, "limit": 500})
        next_page = (nxt.get("data") or {}).get("next_items_page") or {}
        items.extend(next_page.get("items") or [])
        cursor = next_page.get("cursor")

    return board.get("name") or "", items


def main() -> int:
    token = require_env("MONDAY_API_TOKEN")
    year = int(require_env("REPORT_YEAR"))
    month = int(require_env("REPORT_MONTH"))

    if month < 1 or month > 12:
        raise ValueError("REPORT_MONTH must be between 1 and 12.")

    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])

    board_name, items = fetch_all_items(token)

    matched = []
    for item in items:
        cols = column_map(item)
        released = parse_date(cols.get(COLUMN_IDS["release_date"], ""))
        if released and start <= released <= end:
            matched.append((item, cols))

    missing_frameio = 0
    missing_writer = 0
    missing_any_editor = 0
    missing_draft_date = 0

    for _, cols in matched:
        if not cols.get(COLUMN_IDS["frameio_link"]):
            missing_frameio += 1
        if not cols.get(COLUMN_IDS["scriptwriter"]):
            missing_writer += 1
        if not any(
            cols.get(COLUMN_IDS[k])
            for k in ("science_video_editor", "finishing_editor", "rough_editor")
        ):
            missing_any_editor += 1
        if not cols.get(COLUMN_IDS["draft_script_sent"]):
            missing_draft_date += 1

    print("=" * 72)
    print("VIDEO QC AUTOMATION - MONTHLY MONDAY VALIDATION")
    print("=" * 72)
    print(f"Mode                     : READ ONLY")
    print(f"Board                    : {BOARD_ID} | {board_name}")
    print(f"Selected month           : {year:04d}-{month:02d}")
    print(f"Total board items scanned: {len(items)}")
    print(f"Rows in selected month   : {len(matched)}")
    print(f"Missing Frame.io link    : {missing_frameio}")
    print(f"Missing Scriptwriter     : {missing_writer}")
    print(f"Missing any editor       : {missing_any_editor}")
    print(f"Missing Draft Script Sent: {missing_draft_date}")
    print("Writes made              : NONE")
    print("=" * 72)

    if not matched:
        raise RuntimeError("No rows found for the selected release month.")

    if missing_frameio == len(matched):
        raise RuntimeError("Every selected row is missing a Frame.io link.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise

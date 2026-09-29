from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

MONDAY_API_URL = "https://api.monday.com/v2"
DEFAULT_BOARD_ID = "1662638864"
DEFAULT_SAMPLE_LIMIT = 10

QUERY = """
query BoardAudit($boardId: ID!, $limit: Int!) {
  boards(ids: [$boardId]) {
    id
    name
    columns {
      id
      title
      type
      settings_str
    }
    groups {
      id
      title
    }
    items_page(limit: $limit) {
      cursor
      items {
        id
        name
        group {
          id
          title
        }
        column_values {
          id
          text
          value
        }
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
        timeout=45,
    )
    response.raise_for_status()
    payload = response.json()

    if payload.get("errors"):
        raise RuntimeError(
            "Monday GraphQL returned errors:\n"
            + json.dumps(payload["errors"], indent=2, ensure_ascii=False)
        )

    return payload


def main() -> int:
    token = require_env("MONDAY_API_TOKEN")
    board_id = os.getenv("MONDAY_BOARD_ID", DEFAULT_BOARD_ID).strip()
    sample_limit = int(os.getenv("MONDAY_SAMPLE_LIMIT", str(DEFAULT_SAMPLE_LIMIT)))

    if sample_limit < 1 or sample_limit > 100:
        raise ValueError("MONDAY_SAMPLE_LIMIT must be between 1 and 100.")

    payload = post_graphql(
        token=token,
        query=QUERY,
        variables={"boardId": board_id, "limit": sample_limit},
    )

    boards = (payload.get("data") or {}).get("boards") or []
    if not boards:
        raise RuntimeError(
            f"Board {board_id} was not returned. Check the token's access to the board."
        )

    board = boards[0]
    result = {
        "mode": "READ_ONLY",
        "board_id": board.get("id"),
        "board_name": board.get("name"),
        "columns": board.get("columns") or [],
        "groups": board.get("groups") or [],
        "sample_items": ((board.get("items_page") or {}).get("items") or []),
        "next_cursor": (board.get("items_page") or {}).get("cursor"),
    }

    out_dir = Path("artifacts")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "monday_board_audit.json"
    out_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("=" * 72)
    print("VIDEO QC AUTOMATION - MONDAY READ-ONLY AUDIT")
    print("=" * 72)
    print(f"Board ID      : {result['board_id']}")
    print(f"Board name    : {result['board_name']}")
    print(f"Columns found : {len(result['columns'])}")
    print(f"Groups found  : {len(result['groups'])}")
    print(f"Sample items  : {len(result['sample_items'])}")
    print(f"Artifact      : {out_path}")
    print("Writes made   : NONE")
    print("=" * 72)

    print("\nCOLUMN MAP")
    for column in result["columns"]:
        print(
            f"- {column.get('title')} | id={column.get('id')} | type={column.get('type')}"
        )

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise

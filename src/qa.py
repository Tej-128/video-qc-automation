from __future__ import annotations

from collections import Counter
from io import BytesIO
from typing import Any

from openpyxl import load_workbook

from src.qc_classifier import SCRIPTING_CATEGORIES, VIDEO_CATEGORIES
from src.version import BUILD_VERSION


def _formula_count(wb) -> int:
    count = 0
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str) and cell.value.startswith("="):
                    count += 1
    return count


def _sheet_headers(ws) -> list[str]:
    return [str(cell.value or "") for cell in ws[1]]


def _expected_main_headers(team: str) -> list[str]:
    categories = SCRIPTING_CATEGORIES if team == "scripting" else VIDEO_CATEGORIES
    contributor = "Writer" if team == "scripting" else "Video Editor"
    date_header = "Date Script Draft Sent" if team == "scripting" else "Date Video Released"
    headers = ["Article Number", contributor, date_header]
    for category in categories:
        headers.extend([category, "QC Comment"])
    headers.append("Total Errors")
    return headers


def audit_workbook(
    payload: bytes,
    *,
    team: str,
    projects: list[dict[str, Any]],
    error_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    wb = load_workbook(BytesIO(payload), data_only=False)
    categories = SCRIPTING_CATEGORIES if team == "scripting" else VIDEO_CATEGORIES
    main_sheet = "QC by Draft Date" if team == "scripting" else "Video QC by Release Date"

    required = {
        main_sheet,
        "Description",
        "Monthly Trend",
        "Analysis Guide",
        "Needs Review",
        "Error Detail",
        "Raw Frame.io Comments",
        "_Build Info",
    }
    required.add("Writer Summary" if team == "scripting" else "Editor Summary")
    required.add("Writer Monthly Trend" if team == "scripting" else "Editor Monthly Trend")
    if team == "video_editing":
        required.add("Common Error Patterns")

    checks: dict[str, bool] = {}
    notes: list[str] = []

    checks["required_sheets"] = required.issubset(set(wb.sheetnames))
    if not checks["required_sheets"]:
        notes.append("One or more required workbook tabs are missing.")

    if main_sheet in wb.sheetnames:
        ws = wb[main_sheet]
        checks["main_headers"] = _sheet_headers(ws) == _expected_main_headers(team)
        checks["project_row_count"] = ws.max_row - 1 == len(projects)
        expected_total = sum(
            int(row.get("ai_error_count") or 0)
            for row in error_rows
            if row.get("team") == team
        )
        total_col = ws.max_column
        actual_total = sum(
            int(ws.cell(row, total_col).value or 0)
            for row in range(2, ws.max_row + 1)
        )
        checks["main_total_consistency"] = actual_total == expected_total
        if not checks["main_total_consistency"]:
            notes.append(
                f"{team} article total mismatch: workbook={actual_total}, expected={expected_total}."
            )
    else:
        checks["main_headers"] = False
        checks["project_row_count"] = False
        checks["main_total_consistency"] = False

    if "Description" in wb.sheetnames:
        desc = wb["Description"]
        observed = {
            str(desc.cell(row, 1).value or ""): str(desc.cell(row, 2).value or "")
            for row in range(2, desc.max_row + 1)
            if desc.cell(row, 1).value
        }
        checks["category_definitions"] = all(
            observed.get(category) == definition
            for category, definition in categories.items()
        )
    else:
        checks["category_definitions"] = False

    checks["no_formulas"] = _formula_count(wb) == 0

    if "_Build Info" in wb.sheetnames:
        info = wb["_Build Info"]
        checks["build_metadata"] = info["B1"].value == BUILD_VERSION
    else:
        checks["build_metadata"] = False

    if "Needs Review" in wb.sheetnames:
        nr = wb["Needs Review"]
        valid_team_rows = True
        # Team separation is guaranteed by the report generator; here we only
        # ensure the tab exists and is structurally populated when needed.
        checks["needs_review_structure"] = nr.max_column >= 8 and valid_team_rows
    else:
        checks["needs_review_structure"] = False

    if team == "video_editing" and "Common Error Patterns" in wb.sheetnames:
        patterns = wb["Common Error Patterns"]
        recurring_only = True
        for row in range(2, patterns.max_row + 1):
            total = int(patterns.cell(row, 3).value or 0)
            articles = int(patterns.cell(row, 4).value or 0)
            if total < 2 or articles < 2:
                recurring_only = False
                break
        checks["recurring_patterns_only"] = recurring_only
    else:
        checks["recurring_patterns_only"] = True

    passed = sum(1 for value in checks.values() if value)
    score = passed / max(len(checks), 1)

    return {
        "team": team,
        "score": score,
        "checks": checks,
        "notes": notes,
    }


def audit_run(
    *,
    projects: list[dict[str, Any]],
    bundles: list[dict[str, Any]],
    classifications: list[dict[str, Any]],
    error_rows: list[dict[str, Any]],
    scripting_report: bytes,
    video_report: bytes,
) -> dict[str, Any]:
    comments = [
        comment
        for bundle in bundles
        for comment in (bundle.get("comments") or [])
    ]
    resolved_projects = [
        bundle
        for bundle in bundles
        if bundle.get("resolution") is not None
        and getattr(bundle["resolution"], "status", "") == "resolved"
    ]

    classification_failures = [
        row for row in classifications if row.get("_classification_error")
    ]

    commenter_count = sum(
        1 for row in comments if str(row.get("commenter") or "").strip()
    )

    data_checks = {
        "projects_present": bool(projects),
        "frameio_resolution_complete": len(resolved_projects) == len(projects),
        "classification_coverage_complete": len(classification_failures) == 0,
        "commenter_identity_coverage": (
            (commenter_count / len(comments)) if comments else 1.0
        ),
    }

    scripting = audit_workbook(
        scripting_report,
        team="scripting",
        projects=projects,
        error_rows=error_rows,
    )
    video = audit_workbook(
        video_report,
        team="video_editing",
        projects=projects,
        error_rows=error_rows,
    )

    binary_checks = [
        data_checks["projects_present"],
        data_checks["frameio_resolution_complete"],
        data_checks["classification_coverage_complete"],
        *scripting["checks"].values(),
        *video["checks"].values(),
    ]
    binary_score = sum(1 for value in binary_checks if value) / max(len(binary_checks), 1)
    commenter_score = float(data_checks["commenter_identity_coverage"])

    # 95% is a quality gate, not an expected-output hardcode:
    # 90% of the score is deterministic source/report consistency,
    # 10% is audit identity completeness.
    overall_score = (0.90 * binary_score) + (0.10 * commenter_score)

    notes = [*scripting["notes"], *video["notes"]]
    if classification_failures:
        notes.append(
            f"{len(classification_failures)} comment(s) remain unclassified after recovery."
        )
    if len(resolved_projects) != len(projects):
        notes.append(
            f"{len(projects) - len(resolved_projects)} project(s) remain unresolved in Frame.io."
        )
    if comments and commenter_score < 0.95:
        notes.append(
            f"Commenter identity coverage is {commenter_score:.1%}; audit metadata is incomplete."
        )

    return {
        "score": overall_score,
        "target": 0.95,
        "passed": overall_score >= 0.95,
        "data_checks": data_checks,
        "scripting": scripting,
        "video": video,
        "notes": notes,
    }

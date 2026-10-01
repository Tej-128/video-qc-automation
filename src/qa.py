from __future__ import annotations

from io import BytesIO
from typing import Any

from openpyxl import load_workbook

from src.qc_classifier import SCRIPTING_CATEGORIES, VIDEO_CATEGORIES
from src.report_generator import _performance_eligible_article_ids
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


def _main_is_sorted(ws) -> bool:
    values = []
    for row in range(2, ws.max_row + 1):
        value = ws.cell(row, 3).value
        values.append((value, str(ws.cell(row, 1).value or "")))
    return values == sorted(values, key=lambda item: (item[0], item[1]))


def _review_headers_ok(ws) -> bool:
    required = {
        "Error Key",
        "Article Number",
        "Version",
        "Timecode",
        "Frame.io Review Link",
        "Comment",
        "AI Team",
        "AI Category",
        "AI Assignee",
        "AI Count",
        "AI Needs Review",
        "AI Performance Eligible",
        "Review Action",
        "Manual Team",
        "Manual Category",
        "Manual Assignee",
        "Manual Count",
        "Reviewer Notes",
    }
    return required.issubset(set(_sheet_headers(ws)))


def audit_workbook(
    payload: bytes,
    *,
    team: str,
    projects: list[dict[str, Any]],
    bundles: list[dict[str, Any]],
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
        "Extraction Audit",
        "Needs Review",
        "Review Overrides",
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
        checks["main_date_sort"] = _main_is_sorted(ws)

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
        checks["main_date_sort"] = False
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

    if "Extraction Audit" in wb.sheetnames:
        audit_ws = wb["Extraction Audit"]
        checks["extraction_audit_row_count"] = audit_ws.max_row - 1 == len(projects)
        link_ok = True
        for row in range(2, audit_ws.max_row + 1):
            value = str(audit_ws.cell(row, 2).value or "")
            if value and not audit_ws.cell(row, 2).hyperlink:
                link_ok = False
                break
        checks["extraction_links"] = link_ok
    else:
        checks["extraction_audit_row_count"] = False
        checks["extraction_links"] = False

    if "Needs Review" in wb.sheetnames:
        nr = wb["Needs Review"]
        checks["needs_review_structure"] = nr.max_column >= 11
    else:
        checks["needs_review_structure"] = False

    if "Review Overrides" in wb.sheetnames:
        checks["review_workflow_structure"] = _review_headers_ok(wb["Review Overrides"])
    else:
        checks["review_workflow_structure"] = False

    if "Monthly Trend" in wb.sheetnames:
        trend_header = str(wb["Monthly Trend"]["A1"].value or "")
        checks["trend_scope_labelled"] = "selected" in trend_header.casefold()
    else:
        checks["trend_scope_labelled"] = False

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

    eligible_ids = _performance_eligible_article_ids(
        projects, bundles, error_rows, team
    )
    summary_name = "Writer Summary" if team == "scripting" else "Editor Summary"
    if summary_name in wb.sheetnames:
        summary = wb[summary_name]
        workbook_articles = sum(
            int(summary.cell(row, 2).value or 0)
            for row in range(2, summary.max_row + 1)
        )
        checks["performance_denominator_consistency"] = workbook_articles == len(eligible_ids)
    else:
        checks["performance_denominator_consistency"] = False

    passed = sum(1 for value in checks.values() if value)
    score = passed / max(len(checks), 1)

    return {
        "team": team,
        "score": score,
        "checks": checks,
        "notes": notes,
        "performance_eligible_articles": len(eligible_ids),
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
    extraction_verified = [
        bundle
        for bundle in bundles
        if (bundle.get("extraction_audit") or {}).get("status") == "verified"
    ]

    classification_failures = [
        row for row in classifications if row.get("_classification_error")
    ]

    commenter_count = sum(
        1 for row in comments if str(row.get("commenter") or "").strip()
    )

    comment_ids = [
        str(row.get("comment_id") or "")
        for row in comments
        if row.get("comment_id")
    ]

    zero_error_policy_ok = True
    version_exclusion_ok = True
    for bundle in bundles:
        audit = bundle.get("extraction_audit") or {}
        bundle_comments = bundle.get("comments") or []
        if not bundle_comments and audit.get("zero_error_verified"):
            if audit.get("status") != "verified" or int(audit.get("eligible_versions") or 0) <= 0:
                zero_error_policy_ok = False

        versions = bundle.get("versions") or []
        if versions:
            for index, version in enumerate(versions):
                expected = 0 < index < len(versions) - 1
                if bool(version.get("included_for_qc")) != expected:
                    version_exclusion_ok = False
                    break

    data_checks = {
        "projects_present": bool(projects),
        "frameio_resolution_complete": len(resolved_projects) == len(projects),
        "extraction_verification_complete": len(extraction_verified) == len(projects),
        "classification_coverage_complete": len(classification_failures) == 0,
        "comment_ids_unique": len(comment_ids) == len(set(comment_ids)),
        "version_exclusion_policy": version_exclusion_ok,
        "zero_error_policy": zero_error_policy_ok,
        "commenter_identity_coverage": (
            (commenter_count / len(comments)) if comments else 1.0
        ),
    }

    scripting = audit_workbook(
        scripting_report,
        team="scripting",
        projects=projects,
        bundles=bundles,
        error_rows=error_rows,
    )
    video = audit_workbook(
        video_report,
        team="video_editing",
        projects=projects,
        bundles=bundles,
        error_rows=error_rows,
    )

    binary_checks = [
        data_checks["projects_present"],
        data_checks["frameio_resolution_complete"],
        data_checks["extraction_verification_complete"],
        data_checks["classification_coverage_complete"],
        data_checks["comment_ids_unique"],
        data_checks["version_exclusion_policy"],
        data_checks["zero_error_policy"],
        *scripting["checks"].values(),
        *video["checks"].values(),
    ]
    structural_score = sum(1 for value in binary_checks if value) / max(len(binary_checks), 1)

    critical_pass = (
        data_checks["frameio_resolution_complete"]
        and data_checks["extraction_verification_complete"]
        and data_checks["classification_coverage_complete"]
        and data_checks["version_exclusion_policy"]
        and data_checks["zero_error_policy"]
    )

    notes = [*scripting["notes"], *video["notes"]]
    if classification_failures:
        notes.append(
            f"{len(classification_failures)} comment(s) remain unclassified after recovery."
        )
    if len(resolved_projects) != len(projects):
        notes.append(
            f"{len(projects) - len(resolved_projects)} project(s) remain unresolved in Frame.io."
        )
    if len(extraction_verified) != len(projects):
        notes.append(
            f"{len(projects) - len(extraction_verified)} project(s) do not have fully verified intermediate-version extraction and are excluded from performance denominators."
        )

    commenter_score = float(data_checks["commenter_identity_coverage"])
    if comments and commenter_score < 0.95:
        notes.append(
            f"Commenter identity coverage is {commenter_score:.1%}; this is audit metadata only and does not count as semantic classification accuracy."
        )

    notes.append(
        "Structural QA does not measure semantic classification accuracy. Semantic accuracy is established through the Review Overrides workflow and reviewer agreement."
    )

    return {
        "score": structural_score,
        "score_type": "structural_and_coverage",
        "target": 0.95,
        "passed": structural_score >= 0.95 and critical_pass,
        "critical_pass": critical_pass,
        "semantic_accuracy": None,
        "data_checks": data_checks,
        "scripting": scripting,
        "video": video,
        "notes": notes,
    }

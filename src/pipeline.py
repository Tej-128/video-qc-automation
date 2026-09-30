from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any, Callable

from src.frameio_client import FrameIOClient, Resolution
from src.monday_monthly_extract import fetch_all_items, normalize, parse_date
from src.qc_classifier import attach_classifications, classify_comments
from src.report_generator import build_report

ProgressCallback = Callable[[str, float], None]


def _notify(callback: ProgressCallback | None, message: str, progress: float) -> None:
    if callback:
        callback(message, max(0.0, min(1.0, progress)))


def _month_projects(monday_token: str, year: int, month: int) -> tuple[str, list[dict[str, str]]]:
    board_name, items = fetch_all_items(monday_token)
    selected: list[dict[str, str]] = []

    for item in items:
        row = normalize(item)
        raw = row.get("date_video_released", "")
        parsed = parse_date(raw)
        if parsed is None:
            continue

        # Existing Monday data may occasionally render a date without a year.
        # When that happens, interpret it in the selected report year.
        if raw and len(raw.split()) == 2 and "," not in raw:
            parsed = parsed.replace(year=year)

        if parsed.year == year and parsed.month == month:
            selected.append(row)

    selected.sort(
        key=lambda r: (
            r.get("date_video_released", ""),
            r.get("article_id", ""),
            r.get("item_name", ""),
        )
    )
    return board_name, selected


def run_pipeline(
    *,
    monday_token: str,
    frameio_access_token: str,
    openai_api_key: str,
    year: int,
    month: int,
    openai_model: str = "gpt-5.6-terra",
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    started_at = datetime.utcnow().isoformat() + "Z"

    _notify(progress_callback, "Reading monthly projects from Monday.com...", 0.05)
    board_name, projects = _month_projects(monday_token, year, month)
    if not projects:
        raise RuntimeError(f"No Monday projects were found for {year:04d}-{month:02d}.")

    _notify(progress_callback, f"Found {len(projects)} monthly projects. Connecting to Frame.io...", 0.14)
    frameio = FrameIOClient(frameio_access_token)
    review_projects = frameio.find_review_projects()
    if not review_projects:
        raise RuntimeError(
            'Could not find a Frame.io project named "Projects for Review" '
            "under any account accessible to the authenticated user."
        )

    _notify(progress_callback, "Indexing Frame.io review links...", 0.20)
    share_maps = frameio.build_share_maps(review_projects)

    bundles: list[dict[str, Any]] = []
    total = len(projects)

    for index, project in enumerate(projects, start=1):
        base = 0.20 + (0.42 * ((index - 1) / max(total, 1)))
        _notify(
            progress_callback,
            f"Frame.io {index}/{total}: {project.get('article_id') or project.get('item_name')}",
            base,
        )

        if not project.get("frameio_review_link"):
            resolution = Resolution(
                status="needs_review",
                method="missing_monday_link",
                note="Monday row is missing the Frame.io Review Link.",
            )
            bundles.append(
                {"project": project, "resolution": resolution, "versions": [], "comments": []}
            )
            continue

        try:
            resolution = frameio.resolve_project(project, review_projects, share_maps)
            bundle = frameio.extract_intermediate_comments(project, resolution)
        except Exception as exc:
            resolution = Resolution(
                status="needs_review",
                method="frameio_error",
                note=f"Frame.io extraction failed: {type(exc).__name__}: {exc}",
            )
            bundle = {"project": project, "resolution": resolution, "versions": [], "comments": []}

        bundles.append(bundle)

    all_comments: list[dict[str, Any]] = []
    for bundle in bundles:
        all_comments.extend(bundle.get("comments") or [])

    _notify(
        progress_callback,
        f"Extracted {len(all_comments)} comments/replies. Classifying QC errors with OpenAI...",
        0.65,
    )

    def classification_progress(done: int, total_batches: int) -> None:
        fraction = done / max(total_batches, 1)
        _notify(
            progress_callback,
            f"OpenAI classification batch {done}/{total_batches} complete...",
            0.65 + (0.18 * fraction),
        )

    classifications = classify_comments(
        openai_api_key,
        all_comments,
        model=openai_model,
        progress_callback=classification_progress,
    )
    error_rows = attach_classifications(bundles, classifications)

    _notify(progress_callback, "Generating Scripting QC workbook...", 0.84)
    scripting_report = build_report(
        team="scripting",
        year=year,
        month=month,
        projects=projects,
        bundles=bundles,
        error_rows=error_rows,
    )

    _notify(progress_callback, "Generating Video Editing QC workbook...", 0.91)
    video_report = build_report(
        team="video_editing",
        year=year,
        month=month,
        projects=projects,
        bundles=bundles,
        error_rows=error_rows,
    )

    resolved = [
        bundle
        for bundle in bundles
        if bundle.get("resolution") and bundle["resolution"].status == "resolved"
    ]
    unresolved = [
        bundle
        for bundle in bundles
        if not bundle.get("resolution") or bundle["resolution"].status != "resolved"
    ]
    needs_review_errors = [row for row in error_rows if row.get("needs_review")]
    classification_failures = [
        row for row in error_rows
        if row.get("team") == "review"
        and row.get("error_summary") == "OpenAI classification failed for this comment"
    ]

    _notify(progress_callback, "Reports ready.", 1.0)

    return {
        "started_at": started_at,
        "board_name": board_name,
        "year": year,
        "month": month,
        "projects": projects,
        "bundles": bundles,
        "error_rows": error_rows,
        "metrics": {
            "monthly_projects": len(projects),
            "frameio_resolved": len(resolved),
            "frameio_unresolved": len(unresolved),
            "comments_analyzed": len(all_comments),
            "classified_error_rows": len(error_rows),
            "total_error_count": sum(int(row.get("ai_error_count") or 0) for row in error_rows),
            "needs_review": len(needs_review_errors) + len(unresolved),
            "classification_failures": len(classification_failures),
        },
        "unresolved": [
            {
                "article_id": (bundle.get("project") or {}).get("article_id", ""),
                "item_name": (bundle.get("project") or {}).get("item_name", ""),
                "method": getattr(bundle.get("resolution"), "method", ""),
                "reason": getattr(bundle.get("resolution"), "note", ""),
            }
            for bundle in unresolved
        ],
        "scripting_report": scripting_report,
        "video_report": video_report,
        "debug_resolutions": [
            {
                "article_id": (bundle.get("project") or {}).get("article_id", ""),
                **asdict(bundle["resolution"]),
            }
            for bundle in bundles
            if bundle.get("resolution")
        ],
    }

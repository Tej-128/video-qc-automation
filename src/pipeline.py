from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from difflib import SequenceMatcher
import re
from typing import Any, Callable

from src.frameio_client import FrameIOClient, Resolution
from src.monday_monthly_extract import fetch_all_items, normalize, parse_date
from src.qc_classifier import attach_classifications, classify_comments
from src.report_generator import build_report
from src.qa import audit_run

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


def _normalize_comment_text(text: str) -> str:
    text = re.sub(r"#(?:scripting|video_editing|video|audio)\b", " ", text or "", flags=re.I)
    text = re.sub(r"[^a-z0-9]+", " ", text.casefold())
    return " ".join(text.split())


def _token_set(text: str) -> set[str]:
    return {
        token
        for token in _normalize_comment_text(text).split()
        if len(token) >= 4
    }


def _repeat_similarity(current: str, prior: str) -> float:
    current_norm = _normalize_comment_text(current)
    prior_norm = _normalize_comment_text(prior)
    if not current_norm or not prior_norm:
        return 0.0
    if current_norm == prior_norm:
        return 1.0

    seq = SequenceMatcher(None, current_norm, prior_norm).ratio()
    current_tokens = _token_set(current)
    prior_tokens = _token_set(prior)
    if not current_tokens or not prior_tokens:
        overlap = 0.0
    else:
        overlap = len(current_tokens & prior_tokens) / max(
            min(len(current_tokens), len(prior_tokens)), 1
        )
    return max(seq, overlap)


def _annotate_repeat_context(bundles: list[dict[str, Any]]) -> None:
    explicit_repeat = re.compile(
        r"\b(previous comment|previous comments|as mentioned before|mentioned earlier|"
        r"still not|still needs|still need|not addressed|unaddressed|same issue|"
        r"requested earlier|commented earlier)\b",
        re.I,
    )

    for bundle in bundles:
        comments = sorted(
            bundle.get("comments") or [],
            key=lambda row: (
                int(row.get("version_number") or 0),
                str(row.get("created_at") or ""),
            ),
        )
        prior_by_version: dict[int, list[dict[str, Any]]] = {}

        for comment in comments:
            version = int(comment.get("version_number") or 0)
            text = str(comment.get("text") or "")
            best_score = 0.0
            best_text = ""
            best_version = 0

            for prior_version, prior_rows in prior_by_version.items():
                if prior_version >= version:
                    continue
                for prior in prior_rows:
                    prior_text = str(prior.get("text") or "")
                    score = _repeat_similarity(text, prior_text)
                    if score > best_score:
                        best_score = score
                        best_text = prior_text
                        best_version = prior_version

            has_repeat_language = bool(explicit_repeat.search(text)) and version > 2

            # Previous Comments Unaddressed must be supported by both a later
            # version and evidence that the correction matches earlier feedback.
            # Explicit "previous" wording alone is not enough.
            repeated = (
                version > 2
                and best_version > 0
                and (
                    best_score >= 0.88
                    or (has_repeat_language and best_score >= 0.55)
                )
            )

            comment["repeated_from_prior_version"] = repeated
            comment["prior_match_text"] = best_text if repeated else ""
            comment["prior_match_score"] = round(best_score, 4)
            comment["prior_match_version"] = best_version if repeated else 0
            comment["repeat_language_without_match"] = bool(
                has_repeat_language and not repeated
            )
            prior_by_version.setdefault(version, []).append(comment)


def _unverified_extraction_audit(note: str) -> dict[str, Any]:
    return {
        "status": "needs_review",
        "total_versions": 0,
        "eligible_versions": 0,
        "checked_versions": 0,
        "failed_versions": 0,
        "first_version": "",
        "final_version": "",
        "checked_version_names": [],
        "failed_version_names": [],
        "comment_count": 0,
        "zero_error_verified": False,
        "note": note,
    }


def _run_pipeline_once(
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
                {
                    "project": project,
                    "resolution": resolution,
                    "versions": [],
                    "comments": [],
                    "extraction_audit": _unverified_extraction_audit(resolution.note),
                }
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
            bundle = {
                "project": project,
                "resolution": resolution,
                "versions": [],
                "comments": [],
                "extraction_audit": _unverified_extraction_audit(resolution.note),
            }

        bundles.append(bundle)

    _annotate_repeat_context(bundles)

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

    _notify(progress_callback, "Auditing generated reports against live run data...", 0.96)
    qa = audit_run(
        projects=projects,
        bundles=bundles,
        classifications=classifications,
        error_rows=error_rows,
        scripting_report=scripting_report,
        video_report=video_report,
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
    extraction_needs_review = [
        bundle
        for bundle in bundles
        if (bundle.get("extraction_audit") or {}).get("status") != "verified"
    ]
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
            "needs_review": len(needs_review_errors) + len(extraction_needs_review),
            "extraction_needs_review": len(extraction_needs_review),
            "verified_zero_error_projects": sum(
                1
                for bundle in bundles
                if (bundle.get("extraction_audit") or {}).get("zero_error_verified")
            ),
            "classification_failures": len(classification_failures),
        },
        "qa": qa,
        "unresolved": [
            {
                "article_id": (bundle.get("project") or {}).get("article_id", ""),
                "item_name": (bundle.get("project") or {}).get("item_name", ""),
                "method": getattr(bundle.get("resolution"), "method", ""),
                "reason": (
                    (bundle.get("extraction_audit") or {}).get("note")
                    or getattr(bundle.get("resolution"), "note", "")
                ),
                "type": (
                    "Frame.io Resolution"
                    if not bundle.get("resolution")
                    or bundle["resolution"].status != "resolved"
                    else "Extraction Verification"
                ),
            }
            for bundle in extraction_needs_review
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


def run_pipeline(
    *,
    monday_token: str,
    frameio_access_token: str,
    openai_api_key: str,
    year: int,
    month: int,
    openai_model: str = "gpt-5.6-terra",
    progress_callback: ProgressCallback | None = None,
    max_quality_attempts: int = 3,
) -> dict[str, Any]:
    """
    Run the live pipeline and automatically retry the complete evidence chain when
    objective QA is below target. The retry loop is source-driven: it never
    hardcodes expected article counts, names, error totals, or contributor values.
    """
    latest: dict[str, Any] | None = None

    for attempt in range(1, max_quality_attempts + 1):
        def wrapped_progress(message: str, fraction: float) -> None:
            if progress_callback:
                progress_callback(
                    f"Quality attempt {attempt}/{max_quality_attempts}: {message}",
                    fraction,
                )

        latest = _run_pipeline_once(
            monday_token=monday_token,
            frameio_access_token=frameio_access_token,
            openai_api_key=openai_api_key,
            year=year,
            month=month,
            openai_model=openai_model,
            progress_callback=wrapped_progress,
        )
        latest["quality_attempt"] = attempt

        qa = latest.get("qa") or {}
        if qa.get("passed"):
            return latest

        # Only retry when another live read/model attempt can plausibly improve
        # the evidence. Structural workbook failures are deterministic code bugs
        # and should surface immediately rather than waste API calls.
        data_checks = qa.get("data_checks") or {}
        recoverable_extraction = any(
            (
                not bundle.get("resolution")
                or bundle["resolution"].status != "resolved"
                or int((bundle.get("extraction_audit") or {}).get("failed_versions") or 0) > 0
            )
            for bundle in latest.get("bundles") or []
            if (bundle.get("extraction_audit") or {}).get("status") != "verified"
        )
        recoverable = (
            not data_checks.get("frameio_resolution_complete", True)
            or not data_checks.get("classification_coverage_complete", True)
            or recoverable_extraction
        )
        if not recoverable:
            return latest

    assert latest is not None
    return latest

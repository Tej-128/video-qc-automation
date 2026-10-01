from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import requests

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.6-terra"

SCRIPTING_CATEGORIES = {
    "Language & Narration": "Grammar, wording, sentence structure, awkward phrasing, redundant narration, and unnecessary words",
    "Titles & On-screen Text": "Section titles, author names, affiliations, missing on-screen references, and title consistency",
    "Scientific Accuracy & Completeness": "Incorrect scientific statements, missing procedural details, terminology, missing measurements, unclear explanations",
    "Scientific Formatting": "Italics, superscripts, equations, scientific notation",
}

VIDEO_CATEGORIES = {
    "Audio/Visual": "Sound glitch, video glitch, blurry footage, footage pacing",
    "On-Screen Text": "Spelling errors, interpretation errors, format errors",
    "Result Section": "Incorrect highlights, incorrect labelling, not using contrast colours",
    "Previous Comments Unaddressed": "Comments from previous versions left unaddressed in new versions (editing and non-editing related)",
}

ALL_CATEGORIES = list(SCRIPTING_CATEGORIES) + list(VIDEO_CATEGORIES) + ["None"]

SCHEMA = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "record_key": {"type": "string"},
                    "issues": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "team": {"type": "string", "enum": ["scripting", "video_editing", "none"]},
                                "category": {"type": "string", "enum": ALL_CATEGORIES},
                                "error_summary": {"type": "string"},
                                "error_count": {"type": "integer", "minimum": 0, "maximum": 20},
                                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                                "needs_review": {"type": "boolean"},
                                "reason": {"type": "string"},
                                "pattern_label": {"type": "string"},
                            },
                            "required": ["team", "category", "error_summary", "error_count", "confidence", "needs_review", "reason", "pattern_label"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["record_key", "issues"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["classifications"],
    "additionalProperties": False,
}

ProgressCallback = Callable[[int, int], None]


def _system_prompt() -> str:
    scripting = "\n".join(f"- {k}: {v}" for k, v in SCRIPTING_CATEGORIES.items())
    video = "\n".join(f"- {k}: {v}" for k, v in VIDEO_CATEGORIES.items())
    return f"""You classify JoVE production QC comments into atomic errors for a monthly performance report.

Scripting categories:
{scripting}

Video-editing categories:
{video}

Rules:
1. Analyze every supplied comment, including replies, but do not count acknowledgements, questions, confirmations, or conversational replies as errors unless they clearly contain a new QC correction.
2. One comment may contain multiple independent corrections. Split them into separate issue objects when categories differ. If a comment clearly identifies multiple occurrences of the same error type, keep one issue object and set error_count to the explicit or clearly implied number; otherwise use 1.
3. Use ONLY the category definitions above. Do not broaden or invent category definitions.
4. Scripting means the underlying script/content/instruction is wrong. Video editing means the underlying instruction is acceptable but its audiovisual execution is wrong. A requested change to narration wording, scientific/procedural wording, terminology, measurements, or the content that should have been scripted is scripting unless the comment clearly says the script/instruction was already correct and the editor/VO execution failed to follow it.
5. Hashtags are supporting evidence, not a substitute for analyzing the actual error. If #scripting or #video_editing is explicitly present, treat it as a strong attribution hint because those labels were proposed for team attribution. Other operational tags such as #video or #audio must not by themselves decide who introduced the error.
6. Use Previous Comments Unaddressed ONLY when repeated_from_prior_version=true. That flag is created by deterministic cross-version comparison. If repeat_language_without_match=true but repeated_from_prior_version=false, classify the actual underlying issue instead and set needs_review=true because the wording references an earlier version without enough matching evidence.
7. If there is no genuine QC error, return an empty issues array.
8. If the team or category is ambiguous, choose the most plausible one but set needs_review=true and lower confidence.
9. Never invent facts outside the comment text and supplied metadata.
10. Keep error_summary short and concrete.
11. pattern_label must be a short normalized recurring-pattern phrase, reusing the same wording for similar errors across different comments (for example: "Footage pacing", "Audio-video mismatch", "Incorrect highlighting", "On-screen text formatting").
12. Return exactly one classification object for every supplied record_key, even when its issues array is empty.
"""


def _extract_output_text(payload: dict[str, Any]) -> str:
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                return str(content.get("text") or "")
    return ""


def _classify_batch(
    api_key: str,
    batch: list[dict[str, Any]],
    model: str,
    batch_number: int,
) -> tuple[int, list[dict[str, Any]]]:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    key_to_comment_id: dict[str, str] = {}
    user_payload = []
    for index, row in enumerate(batch, start=1):
        record_key = f"B{batch_number:03d}C{index:03d}"
        key_to_comment_id[record_key] = str(row.get("comment_id") or "")
        user_payload.append(
            {
                "record_key": record_key,
                "version_number": row.get("version_number"),
                "is_reply": bool(row.get("is_reply")),
                "parent_comment_id": row.get("parent_comment_id", ""),
                "commenter": row.get("commenter", ""),
                "text": row.get("text", ""),
                "repeated_from_prior_version": bool(row.get("repeated_from_prior_version")),
                "prior_match_text": row.get("prior_match_text", ""),
                "prior_match_score": row.get("prior_match_score", 0),
                "prior_match_version": row.get("prior_match_version", 0),
                "repeat_language_without_match": bool(row.get("repeat_language_without_match")),
            }
        )
    expected_keys = set(key_to_comment_id)

    body = {
        "model": model,
        "instructions": _system_prompt(),
        "input": [{
            "role": "user",
            "content": [{
                "type": "input_text",
                "text": "Classify these Frame.io QC comments:\n" + json.dumps(user_payload, ensure_ascii=False),
            }],
        }],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "video_qc_comment_classification",
                "description": "Atomic QC error classifications for supplied Frame.io comments.",
                "strict": True,
                "schema": SCHEMA,
            }
        },
        "reasoning": {"effort": "none"},
        "store": False,
    }

    last_error: Exception | None = None
    for attempt in range(1, 3):
        try:
            response = requests.post(
                OPENAI_RESPONSES_URL,
                headers=headers,
                json=body,
                timeout=(10, 60),
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise RuntimeError(f"OpenAI temporary HTTP {response.status_code}")
            response.raise_for_status()

            text = _extract_output_text(response.json())
            if not text:
                raise RuntimeError("OpenAI returned no structured output text.")

            parsed = json.loads(text)
            rows = parsed.get("classifications") or []
            returned_keys = {str(row.get("record_key") or "") for row in rows}

            if returned_keys != expected_keys:
                missing = sorted(expected_keys - returned_keys)
                extra = sorted(returned_keys - expected_keys)
                raise RuntimeError(
                    f"OpenAI batch coverage mismatch: missing={len(missing)}, extra={len(extra)}"
                )

            normalized_rows = []
            for row in rows:
                record_key = str(row.get("record_key") or "")
                normalized_rows.append(
                    {
                        "comment_id": key_to_comment_id[record_key],
                        "issues": row.get("issues") or [],
                    }
                )
            return batch_number, normalized_rows
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 * attempt)

    raise RuntimeError(
        f"OpenAI classification batch {batch_number} failed after 2 attempts: {last_error}"
    )


def classify_comments(
    api_key: str,
    comments: list[dict[str, Any]],
    *,
    model: str = DEFAULT_MODEL,
    batch_size: int = 50,
    max_workers: int = 4,
    progress_callback: ProgressCallback | None = None,
) -> list[dict[str, Any]]:
    """
    Classify every comment with progressive recovery:
    pass 1 = normal large parallel batches,
    pass 2 = only failed comments in batches of 10,
    pass 3 = only still-failed comments one at a time.

    No expected output values are hardcoded; recovery is driven entirely by
    API/coverage failures observed during the current run.
    """
    if not comments:
        return []

    resolved: dict[str, dict[str, Any]] = {}
    remaining = list(comments)

    def source_key(row: dict[str, Any]) -> str:
        return str(row.get("comment_id") or "")

    passes = [
        (batch_size, max_workers),
        (10, min(max_workers, 4)),
        (1, min(max_workers, 4)),
    ]

    for pass_index, (current_batch_size, current_workers) in enumerate(passes, start=1):
        if not remaining:
            break

        batches = [
            remaining[start : start + current_batch_size]
            for start in range(0, len(remaining), current_batch_size)
        ]
        total_batches = len(batches)
        failures: list[dict[str, Any]] = []
        completed = 0

        with ThreadPoolExecutor(max_workers=min(current_workers, total_batches)) as executor:
            futures = {
                executor.submit(
                    _classify_batch,
                    api_key,
                    batch,
                    model,
                    (pass_index * 10000) + index,
                ): batch
                for index, batch in enumerate(batches, start=1)
            }

            for future in as_completed(futures):
                source_batch = futures[future]
                try:
                    _, rows = future.result()
                    for row in rows:
                        resolved[str(row.get("comment_id") or "")] = row
                except Exception:
                    failures.extend(source_batch)

                completed += 1
                if progress_callback:
                    progress_callback(completed, total_batches)

        remaining = [
            row
            for row in failures
            if source_key(row) not in resolved
        ]

    for row in remaining:
        comment_id = source_key(row)
        resolved[comment_id] = {
            "comment_id": comment_id,
            "issues": [],
            "_classification_error": (
                "OpenAI classification could not be recovered after large-batch, "
                "small-batch, and single-comment attempts."
            ),
        }

    # Preserve original Frame.io comment order for deterministic downstream reports.
    return [
        resolved[source_key(row)]
        for row in comments
        if source_key(row) in resolved
    ]


def attach_classifications(projects: list[dict[str, Any]], classifications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_comment = {str(row.get("comment_id") or ""): row.get("issues") or [] for row in classifications}
    classification_errors = {
        str(row.get("comment_id") or ""): str(row.get("_classification_error") or "")
        for row in classifications
        if row.get("_classification_error")
    }
    error_rows: list[dict[str, Any]] = []

    for project_bundle in projects:
        project = project_bundle["project"]
        resolution = project_bundle["resolution"]
        editors = [project.get("science_video_editor", ""), project.get("finishing_editor", ""), project.get("rough_editor", "")]
        distinct_editors = [x for i, x in enumerate(editors) if x and x not in editors[:i]]
        primary_editor = project.get("science_video_editor") or project.get("finishing_editor") or project.get("rough_editor") or ""
        handover = len(distinct_editors) > 1
        extraction_audit = project_bundle.get("extraction_audit") or {}
        extraction_verified = extraction_audit.get("status") == "verified"
        extraction_note = str(extraction_audit.get("note") or "")

        for comment in project_bundle.get("comments") or []:
            comment_id = str(comment.get("comment_id") or "")
            if comment_id in classification_errors:
                error_rows.append({
                    "article_id": project.get("article_id", ""),
                    "item_name": project.get("item_name", ""),
                    "date_video_released": project.get("date_video_released", ""),
                    "draft_script_sent": project.get("draft_script_sent", ""),
                    "frameio_review_link": project.get("frameio_review_link", ""),
                    "resolution_method": resolution.method,
                    "asset_name": resolution.asset_name,
                    "version_number": comment.get("version_number"),
                    "version_name": comment.get("version_name", ""),
                    "comment_id": comment_id,
                    "parent_comment_id": comment.get("parent_comment_id", ""),
                    "is_reply": bool(comment.get("is_reply")),
                    "commenter": comment.get("commenter", ""),
                    "comment_created_at": comment.get("created_at", ""),
                    "timecode": comment.get("timestamp", ""),
                    "comment_text": comment.get("text", ""),
                    "issue_index": 0,
                    "team": "review",
                    "ai_category": "None",
                    "error_summary": "OpenAI classification failed for this comment",
                    "ai_error_count": 0,
                    "confidence": 0.0,
                    "needs_review": True,
                    "performance_eligible": False,
                    "classification_reason": (
                        classification_errors[comment_id]
                        + (f" | Extraction audit: {extraction_note}" if not extraction_verified and extraction_note else "")
                    ),
                    "pattern_label": "",
                    "ai_assignee": "",
                    "scriptwriter": project.get("scriptwriter", ""),
                    "science_video_editor": project.get("science_video_editor", ""),
                    "finishing_editor": project.get("finishing_editor", ""),
                    "rough_editor": project.get("rough_editor", ""),
                })

            for issue_index, issue in enumerate(by_comment.get(comment_id, []), start=1):
                team = issue.get("team", "none")
                category = issue.get("category", "None")
                if team == "none" or category == "None" or int(issue.get("error_count") or 0) <= 0:
                    continue

                if team == "scripting":
                    assignee = project.get("scriptwriter", "")
                    attribution_review = not bool(assignee)
                else:
                    # When rough/science/finishing editors differ, do not guess who
                    # owns the error. Keep it in Needs Review and exclude it from
                    # contributor performance until reviewed.
                    assignee = "" if handover else primary_editor
                    attribution_review = handover or not bool(assignee)

                confidence = float(issue.get("confidence") or 0)
                needs_review = (
                    bool(issue.get("needs_review"))
                    or confidence < 0.78
                    or attribution_review
                    or not extraction_verified
                )
                performance_eligible = bool(assignee) and not needs_review and extraction_verified

                classification_reason = str(issue.get("reason", "") or "")
                if not extraction_verified and extraction_note:
                    classification_reason = (
                        classification_reason + (" | " if classification_reason else "")
                        + f"Extraction audit: {extraction_note}"
                    )

                error_rows.append({
                    "article_id": project.get("article_id", ""),
                    "item_name": project.get("item_name", ""),
                    "date_video_released": project.get("date_video_released", ""),
                    "draft_script_sent": project.get("draft_script_sent", ""),
                    "frameio_review_link": project.get("frameio_review_link", ""),
                    "resolution_method": resolution.method,
                    "asset_name": resolution.asset_name,
                    "version_number": comment.get("version_number"),
                    "version_name": comment.get("version_name", ""),
                    "comment_id": comment.get("comment_id", ""),
                    "parent_comment_id": comment.get("parent_comment_id", ""),
                    "is_reply": bool(comment.get("is_reply")),
                    "commenter": comment.get("commenter", ""),
                    "comment_created_at": comment.get("created_at", ""),
                    "timecode": comment.get("timestamp", ""),
                    "comment_text": comment.get("text", ""),
                    "issue_index": issue_index,
                    "team": team,
                    "ai_category": category,
                    "error_summary": issue.get("error_summary", ""),
                    "ai_error_count": int(issue.get("error_count") or 1),
                    "confidence": confidence,
                    "needs_review": needs_review,
                    "performance_eligible": performance_eligible,
                    "classification_reason": classification_reason,
                    "pattern_label": issue.get("pattern_label", ""),
                    "ai_assignee": assignee,
                    "scriptwriter": project.get("scriptwriter", ""),
                    "science_video_editor": project.get("science_video_editor", ""),
                    "finishing_editor": project.get("finishing_editor", ""),
                    "rough_editor": project.get("rough_editor", ""),
                })
    return error_rows

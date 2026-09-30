from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import requests

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = "gpt-5.6-terra"

SCRIPTING_CATEGORIES = {
    "Language & Narration": "Grammar, wording, narration phrasing, language quality, pronunciation/narration-script issues.",
    "Titles & On-screen Text": "Scripting-side title, label, caption, or on-screen text content/instruction issues.",
    "Scientific Accuracy & Completeness": "Incorrect, incomplete, misleading, or missing scientific/procedural information.",
    "Scientific Formatting": "Scientific nomenclature and formatting such as italics, symbols, units, notation, capitalization, or conventions.",
}

VIDEO_CATEGORIES = {
    "Audio/Visual": "Editing issues involving audio, footage, visual quality, cuts, timing, synchronization, or presentation.",
    "On-Screen Text": "Execution/formatting errors in text that appears in the edited video, including labels, titles, spelling, placement, or styling.",
    "Result Section": "Editing problems specific to the result/results portion of the video, including result visuals, sequencing, highlighting, or presentation.",
    "Previous Comments Unaddressed": "A prior QC correction was not implemented and is explicitly raised again in a later version/comment.",
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
                    "comment_id": {"type": "string"},
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
                            },
                            "required": ["team", "category", "error_summary", "error_count", "confidence", "needs_review", "reason"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["comment_id", "issues"],
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
3. Scripting means the underlying script, content, or instruction is wrong. Video editing means the script or instruction may be correct but its audiovisual execution is wrong.
4. Use Previous Comments Unaddressed only when the comment explicitly says or clearly indicates that a previously requested correction remains unresolved. This is a new error in addition to the earlier original error.
5. If there is no genuine QC error, return an empty issues array.
6. If the team or category is ambiguous, choose the most plausible one but set needs_review=true and lower confidence.
7. Never invent facts outside the comment text and supplied metadata.
8. Keep error_summary short and concrete.
9. Return exactly one classification object for every supplied comment_id, even when its issues array is empty.
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
    user_payload = [
        {
            "comment_id": row.get("comment_id", ""),
            "version_number": row.get("version_number"),
            "is_reply": bool(row.get("is_reply")),
            "parent_comment_id": row.get("parent_comment_id", ""),
            "commenter": row.get("commenter", ""),
            "text": row.get("text", ""),
        }
        for row in batch
    ]
    expected_ids = {str(row.get("comment_id") or "") for row in user_payload}

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
        "reasoning": {"effort": "low"},
        "store": False,
    }

    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = requests.post(
                OPENAI_RESPONSES_URL,
                headers=headers,
                json=body,
                timeout=(15, 90),
            )
            if response.status_code == 429 or response.status_code >= 500:
                raise RuntimeError(f"OpenAI temporary HTTP {response.status_code}")
            response.raise_for_status()

            text = _extract_output_text(response.json())
            if not text:
                raise RuntimeError("OpenAI returned no structured output text.")

            parsed = json.loads(text)
            rows = parsed.get("classifications") or []
            returned_ids = {str(row.get("comment_id") or "") for row in rows}

            if returned_ids != expected_ids:
                missing = sorted(expected_ids - returned_ids)
                extra = sorted(returned_ids - expected_ids)
                raise RuntimeError(
                    f"OpenAI batch coverage mismatch: missing={len(missing)}, extra={len(extra)}"
                )
            return batch_number, rows
        except Exception as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(2 * attempt)

    raise RuntimeError(
        f"OpenAI classification batch {batch_number} failed after 3 attempts: {last_error}"
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
    if not comments:
        return []

    batches = [
        comments[start : start + batch_size]
        for start in range(0, len(comments), batch_size)
    ]
    total_batches = len(batches)
    completed = 0
    ordered: dict[int, list[dict[str, Any]]] = {}

    with ThreadPoolExecutor(max_workers=min(max_workers, total_batches)) as executor:
        futures = {
            executor.submit(_classify_batch, api_key, batch, model, index): (index, batch)
            for index, batch in enumerate(batches, start=1)
        }

        for future in as_completed(futures):
            batch_number, source_batch = futures[future]
            try:
                returned_batch_number, rows = future.result()
                ordered[returned_batch_number] = rows
            except Exception as exc:
                # Never lose the monthly report because one OpenAI batch failed.
                # Every comment in the failed batch is preserved and surfaced in
                # Needs Review, while the remaining batches continue normally.
                ordered[batch_number] = [
                    {
                        "comment_id": str(row.get("comment_id") or ""),
                        "issues": [],
                        "_classification_error": str(exc),
                    }
                    for row in source_batch
                ]
            completed += 1
            if progress_callback:
                progress_callback(completed, total_batches)

    results: list[dict[str, Any]] = []
    for batch_number in range(1, total_batches + 1):
        results.extend(ordered[batch_number])

    return results


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
                    "classification_reason": classification_errors[comment_id],
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
                    assignee = primary_editor
                    attribution_review = handover or not bool(assignee)

                confidence = float(issue.get("confidence") or 0)
                needs_review = bool(issue.get("needs_review")) or confidence < 0.78 or attribution_review

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
                    "classification_reason": issue.get("reason", ""),
                    "ai_assignee": assignee,
                    "scriptwriter": project.get("scriptwriter", ""),
                    "science_video_editor": project.get("science_video_editor", ""),
                    "finishing_editor": project.get("finishing_editor", ""),
                    "rough_editor": project.get("rough_editor", ""),
                })
    return error_rows

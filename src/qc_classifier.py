from __future__ import annotations

import json
import re
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

OPERATIONAL_CATEGORIES = {
    "VO Redos": "Voice-over redo/replacement requests that are operational changes and are not attributable to the video editor unless a separate editing execution error is explicitly stated.",
    "Editorial / Scope Adjustment": "Late editorial, scope, title-card, or reference changes where the comment requests a new/changed deliverable without evidence that the scripting or video-editing contributor made the original error.",
}

ALL_CATEGORIES = list(SCRIPTING_CATEGORIES) + list(VIDEO_CATEGORIES) + list(OPERATIONAL_CATEGORIES) + ["None"]

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
                                "team": {"type": "string", "enum": ["scripting", "video_editing", "operational", "none"]},
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
    operational = "\n".join(f"- {k}: {v}" for k, v in OPERATIONAL_CATEGORIES.items())
    return f"""You classify JoVE production QC comments into atomic errors for a monthly performance report.

Scripting categories:
{scripting}

Video-editing categories:
{video}

Operational/non-performance categories:
{operational}

Rules:
1. Analyze every supplied comment, including replies. Do not count acknowledgements, questions, confirmations, or conversational replies as errors unless they clearly contain a new QC correction. A reply that only clarifies or implements the same correction as its parent is the same error thread and must not create a second count.
2. One comment may contain multiple independent corrections. Split them into separate issue objects when categories differ. If a comment clearly identifies multiple occurrences of the same error type, keep one issue object and set error_count to the explicit or clearly implied number; otherwise use 1.
3. Use ONLY the category definitions above. Do not broaden or invent category definitions.
4. Scripting means the underlying script/content/instruction is wrong. Video editing means the approved instruction/content is acceptable but its audiovisual execution is wrong. Scientific/procedural meaning, equations, measurements, terminology, missing procedural detail, or what must be stated belongs in Scientific Accuracy & Completeness. Language & Narration is only grammar, wording, sentence structure, awkward phrasing, redundant narration, unnecessary words, or pronunciation where the underlying meaning is unchanged. A pure request to add/change a pause or narration timing without changing words/content is Audio/Visual, not Scripting.
5. If a highlight must appear only when the VO names a particular scientific/anatomical concept, treat that scientific correspondence as Scientific Accuracy & Completeness. General graph/region/curve/color highlighting execution remains Video Editing / Result Section.
6. A comment that mainly says "Use VO redo", "use the VO redo", or "Insert new VO" is normally operational category VO Redos, not a scripting or video-editor error. However, if the same comment also contains independent video instructions (for example specific highlighting, footage, table, crop, delete, blur, or on-screen execution directions), preserve those as separate video-editing issues instead of collapsing the entire comment into VO Redos.
7. Use Editorial / Scope Adjustment only when the text itself gives strong evidence of a new/late editorial request, author-proof change, source-material update, new section/title-card deliverable, or a title/scope change without an error rationale. Do not infer non-performance merely because a comment says add/edit/remove/replace. If scope-vs-error is ambiguous, set needs_review=true rather than silently finalizing it as non-performance.
8. Standalone replacement title/text comments can be genuine corrections even when they contain no verb such as "edit" or "change". Use previous_same_version_text, next_same_version_text, and standalone_title_context to avoid silently dropping them.
9. Hashtags are supporting evidence, not a substitute for analyzing the actual error. If #scripting or #video_editing is explicitly present, treat it as a strong attribution hint because those labels were proposed for team attribution. Other operational tags such as #video or #audio must not by themselves decide who introduced the error.
10. Use Previous Comments Unaddressed ONLY when repeated_from_prior_version=true. That flag is created by deterministic cross-version comparison. If repeat_language_without_match=true but repeated_from_prior_version=false, classify the actual underlying issue instead and set needs_review=true because the wording references an earlier version without enough matching evidence.
11. If there is no genuine QC error, return an empty issues array.
12. If the team or category is ambiguous, choose the most plausible one but set needs_review=true and lower confidence.
13. Never invent facts outside the comment text and supplied metadata.
14. Never paraphrase, summarize, rewrite, clean up, shorten, expand, correct, or otherwise alter the reviewer comment. Set error_summary to the exact supplied comment text, character-for-character. The application also enforces this deterministically downstream.
15. pattern_label must be a short normalized recurring-pattern phrase, reusing the same wording for similar errors across different comments (for example: "Footage pacing", "Audio-video mismatch", "Incorrect highlighting", "On-screen text formatting").
16. Return exactly one classification object for every supplied record_key, even when its issues array is empty.
"""


def _extract_output_text(payload: dict[str, Any]) -> str:
    for item in payload.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                return str(content.get("text") or "")
    return ""


def _operational_vo_redo_hint(text: str) -> bool:
    low = str(text or "").strip().casefold()
    if "#scripting" in low or "vo should" in low or "narration should" in low or "script should" in low:
        return False
    return low.startswith("use vo redo") or low.startswith("use the vo redo") or low.startswith("insert new vo")


def _explicit_video_defect_signal(text: str) -> bool:
    low = str(text or "").casefold()
    return bool(re.search(
        r"\b(blurry|glitch|out of sync|incorrect (?:footage|highlight|label)|wrong (?:footage|highlight|label)|"
        r"missing (?:footage|highlight|label)|footage (?:is|looks) (?:wrong|incorrect|blurry)|"
        r"not addressed|still not|previous comment)\b",
        low,
    ))


def _explicit_video_instruction_signal(text: str) -> bool:
    low = str(text or "").casefold()
    return bool(re.search(
        r"\b(highlight|highlighting|show|display|crop|blur|remove|delete|replace|"
        r"footage|table|figure|on[- ]?screen|title card|freeze frame)\b",
        low,
    ))


def _pure_narration_pause_hint(text: str) -> bool:
    low = str(text or "").casefold()
    if not re.search(r"\bpause\b|\bpauses\b", low):
        return False
    content_change = re.search(
        r"\b(replace|revise|reword|rewrite|change (?:the )?(?:wording|vo|narration|script)|"
        r"should say|say instead|incorrect wording|wrong wording|full form|specify|missing (?:word|phrase|content))\b",
        low,
    )
    return not bool(content_change)


def _scientific_highlight_timing_hint(text: str) -> bool:
    low = str(text or "").casefold()
    return bool(
        "highlight" in low
        and re.search(
            r"(?:highlight[^.\n]{0,120}\b(?:only )?(?:when|as)\b[^.\n]{0,80}\bvo\b|"
            r"\bvo\b[^.\n]{0,80}\b(?:says|mentions)\b[^.\n]{0,120}\bhighlight)",
            low,
        )
    )


def _mixed_vo_highlight_count(text: str) -> int:
    if not _operational_vo_redo_hint(text):
        return 0
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    numbered = [
        line for line in lines
        if re.search(r"\bhighlight(?:ing)?\b", line, re.I)
        and re.search(r"\b(?:for\s+)?\d+(?:\.\d+)+\b", line)
    ]
    if numbered:
        return len(numbered)
    return 1 if re.search(r"\bhighlight(?:ing)?\b", str(text or ""), re.I) else 0


def _strong_editorial_scope_signal(text: str) -> bool:
    low = str(text or "").casefold()
    error_rationale = re.search(
        r"\b(incorrect|wrong|misspelled|spelling|typo|abbreviation|first use|inconsistent|"
        r"consistency|capitalize|capitalise|lowercase|uppercase|missing|required|should have)\b",
        low,
    )
    if "author proof comment" in low:
        return True
    if "not shown" in low and "title" in low:
        return True
    if "see text protocol" in low and ("add on-screen text" in low or "add onscreen text" in low):
        return True
    if re.search(r"\b(insert|add)\b[^.\n]{0,40}\bsection title(?: card)?\b", low):
        return True
    if re.search(r"\b(edit|change|replace)\s+(?:the\s+)?title\s+to\b", low) and not error_rationale:
        return True
    if re.search(r"\b(use|replace with)\b[^.\n]{0,40}\b(updated|new|attached)\b[^.\n]{0,40}\b(figure|file|footage|image)\b", low):
        return True
    if re.search(r"\bif (?:yes|possible|available)\b", low):
        return True
    return False


_THREAD_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "with", "as",
    "is", "are", "be", "been", "it", "this", "that", "we", "could", "should",
    "would", "when", "using", "use", "from", "by", "please", "make", "following",
}


def _thread_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(text or "").casefold())
        if len(token) > 2 and token not in _THREAD_STOPWORDS
    }


def _same_thread_issue(parent_text: str, reply_text: str) -> bool:
    parent_tokens = _thread_tokens(parent_text)
    reply_tokens = _thread_tokens(reply_text)
    if not parent_tokens or not reply_tokens:
        return False
    overlap = len(parent_tokens & reply_tokens) / min(len(parent_tokens), len(reply_tokens))
    return overlap >= 0.45


def _deduplicate_reply_issues(
    comments: list[dict[str, Any]],
    classifications: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    source_by_id = {
        str(comment.get("comment_id") or ""): comment
        for comment in comments
        if comment.get("comment_id")
    }
    result_by_id = {
        str(row.get("comment_id") or ""): row
        for row in classifications
        if row.get("comment_id")
    }

    for comment_id, source in source_by_id.items():
        parent_id = str(source.get("parent_comment_id") or "")
        if not source.get("is_reply") or not parent_id:
            continue

        reply_result = result_by_id.get(comment_id)
        parent_result = result_by_id.get(parent_id)
        parent_source = source_by_id.get(parent_id)
        if not reply_result or not parent_result or not parent_source:
            continue
        if reply_result.get("_classification_error") or parent_result.get("_classification_error"):
            continue
        if not _same_thread_issue(parent_source.get("text", ""), source.get("text", "")):
            continue

        parent_pairs = {
            (str(issue.get("team") or ""), str(issue.get("category") or ""))
            for issue in (parent_result.get("issues") or [])
            if int(issue.get("error_count") or 0) > 0
        }
        kept = []
        removed = 0
        for issue in reply_result.get("issues") or []:
            pair = (str(issue.get("team") or ""), str(issue.get("category") or ""))
            if pair in parent_pairs and int(issue.get("error_count") or 0) > 0:
                removed += 1
                continue
            kept.append(issue)

        if removed:
            reply_result["issues"] = kept
            reply_result["_deduplicated_to_parent"] = parent_id
            reply_result["_deduplicated_issue_count"] = removed

    return classifications


def _scientific_content_correction_hint(source: dict[str, Any]) -> bool:
    low = str(source.get("text") or "").casefold()
    if "vo should specify" in low or "vo does not specify" in low or "specify what is being" in low:
        return True
    if "final concentration" in low and ("on-screen text" in low or "onscreen text" in low):
        return True
    if (
        ("modify the vo" in low or "vo to say" in low or "vo says" in low)
        and ("equation" in low or "calculate" in low)
    ):
        return True
    return False


def _editorial_scope_adjustment_hint(source: dict[str, Any]) -> bool:
    text = str(source.get("text") or "")
    if "#scripting" in text.casefold():
        return False
    return _strong_editorial_scope_signal(text)


def _normalize_model_issues(source: dict[str, Any], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    text = str(source.get("text") or "")
    normalized: list[dict[str, Any]] = []

    pure_vo_redo = _operational_vo_redo_hint(text)
    explicit_video_defect = _explicit_video_defect_signal(text)
    explicit_video_instruction = _explicit_video_instruction_signal(text)
    mixed_highlight_count = _mixed_vo_highlight_count(text)

    for raw in issues or []:
        issue = dict(raw)
        issue["error_summary"] = text
        if (
            pure_vo_redo
            and issue.get("team") in {"scripting", "video_editing"}
            and not explicit_video_defect
            and not explicit_video_instruction
        ):
            issue["team"] = "operational"
            issue["category"] = "VO Redos"
            issue["needs_review"] = False
            issue["reason"] = "VO redo request is treated as an operational change unless a separate contributor defect or video execution instruction is explicitly stated."
            issue["pattern_label"] = "VO Redo"
        normalized.append(issue)

    # Pure narration pause/timing requests do not belong to writer performance.
    if _pure_narration_pause_hint(text):
        normalized = [{
            "team": "video_editing",
            "category": "Audio/Visual",
            "error_summary": text,
            "error_count": 1,
            "confidence": 0.97,
            "needs_review": False,
            "reason": "Pure narration pause/timing correction with no wording or content change.",
            "pattern_label": "Narration timing",
        }]

    # When the timing of a highlight is explicitly tied to the scientific phrase
    # being spoken, the correction is about scientific correspondence.
    if _scientific_highlight_timing_hint(text):
        normalized = [{
            "team": "scripting",
            "category": "Scientific Accuracy & Completeness",
            "error_summary": text,
            "error_count": 1,
            "confidence": 0.97,
            "needs_review": False,
            "reason": "Highlight timing is explicitly tied to the scientific/anatomical concept named in the VO.",
            "pattern_label": "Scientific visual correspondence",
        }]

    if pure_vo_redo:
        retained = [
            row
            for row in normalized
            if (
                row.get("team") == "operational"
                and row.get("category") != "VO Redos"
            )
            or (
                (explicit_video_defect or explicit_video_instruction)
                and row.get("team") == "video_editing"
            )
            or (
                row.get("team") == "scripting"
                and not _operational_vo_redo_hint(text)
            )
        ]
        retained.append({
            "team": "operational",
            "category": "VO Redos",
            "error_summary": text,
            "error_count": 1,
            "confidence": 0.99,
            "needs_review": False,
            "reason": "Deterministic VO redo operational rule.",
            "pattern_label": "VO Redo",
        })

        if mixed_highlight_count:
            retained = [
                row for row in retained
                if not (
                    row.get("team") == "video_editing"
                    and row.get("category") == "Result Section"
                )
            ]
            retained.append({
                "team": "video_editing",
                "category": "Result Section",
                "error_summary": text,
                "error_count": mixed_highlight_count,
                "confidence": 0.98,
                "needs_review": False,
                "reason": "VO redo comment also contains independent highlight/no-highlight execution instructions.",
                "pattern_label": "Highlighting instructions",
            })
        normalized = retained

    if _editorial_scope_adjustment_hint(source):
        retained = [
            row for row in normalized
            if row.get("team") == "video_editing"
        ]
        retained.append({
            "team": "operational",
            "category": "Editorial / Scope Adjustment",
            "error_summary": text,
            "error_count": 1,
            "confidence": 0.95,
            "needs_review": False,
            "reason": "Strong textual evidence of a late/new editorial or scope adjustment; not charged to contributor performance.",
            "pattern_label": "Editorial scope adjustment",
        })
        normalized = retained

    # Model-proposed scope exclusions without strong evidence are not silently
    # finalized as non-performance. They remain visible for manual review.
    for row in normalized:
        if (
            row.get("team") == "operational"
            and row.get("category") == "Editorial / Scope Adjustment"
            and not _strong_editorial_scope_signal(text)
        ):
            row["needs_review"] = True
            row["confidence"] = min(float(row.get("confidence") or 0.75), 0.75)
            row["reason"] = (
                "Ambiguous editorial/scope-versus-contributor-error distinction; manual review required before final non-performance treatment."
            )

    if _scientific_content_correction_hint(source):
        retained_video = [
            row for row in normalized
            if row.get("team") == "video_editing" and explicit_video_defect
        ]
        best_confidence = max(
            [float(row.get("confidence") or 0) for row in normalized] or [0.95]
        )
        retained_video.append({
            "team": "scripting",
            "category": "Scientific Accuracy & Completeness",
            "error_summary": text,
            "error_count": 1,
            "confidence": max(best_confidence, 0.95),
            "needs_review": False,
            "reason": "Deterministic scientific-content rule for missing/specification/equation/concentration content.",
            "pattern_label": "Scientific content completeness",
        })
        normalized = retained_video

    if bool(source.get("standalone_title_context")) and not normalized:
        normalized.append({
            "team": "scripting",
            "category": "Titles & On-screen Text",
            "error_summary": text,
            "error_count": 1,
            "confidence": 0.80,
            "needs_review": True,
            "reason": "Standalone replacement text appears between title-edit comments; retained for review instead of being silently dropped.",
            "pattern_label": "Title wording",
        })

    return normalized

def _classify_batch(
    api_key: str,
    batch: list[dict[str, Any]],
    model: str,
    batch_number: int,
) -> tuple[int, list[dict[str, Any]]]:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    key_to_comment_id: dict[str, str] = {}
    key_to_source: dict[str, dict[str, Any]] = {}
    user_payload = []
    for index, row in enumerate(batch, start=1):
        record_key = f"B{batch_number:03d}C{index:03d}"
        key_to_comment_id[record_key] = str(row.get("comment_id") or "")
        key_to_source[record_key] = row
        user_payload.append(
            {
                "record_key": record_key,
                "version_number": row.get("version_number"),
                "is_reply": bool(row.get("is_reply")),
                "parent_comment_id": row.get("parent_comment_id", ""),
                "commenter": row.get("commenter", ""),
                "text": row.get("text", ""),
                "frameio_version": row.get("version_label") or (
                    f"v{row.get('version_number')}" if row.get("version_number") else ""
                ),
                "previous_same_version_text": row.get("previous_same_version_text", ""),
                "next_same_version_text": row.get("next_same_version_text", ""),
                "standalone_title_context": bool(row.get("standalone_title_context")),
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
                        "issues": _normalize_model_issues(
                            key_to_source[record_key],
                            row.get("issues") or [],
                        ),
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

    # Preserve original Frame.io comment order for deterministic downstream reports,
    # then consolidate reply-only continuations of the same parent issue.
    ordered = [
        resolved[source_key(row)]
        for row in comments
        if source_key(row) in resolved
    ]
    return _deduplicate_reply_issues(comments, ordered)


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
                    "version_label": comment.get("version_label") or (
                        f"v{comment.get('version_number')}" if comment.get("version_number") else ""
                    ),
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
                    "error_summary": comment.get("text", ""),
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
                error_count = int(issue.get("error_count") or 0)
                if team == "none" or category == "None" or error_count <= 0:
                    continue

                valid_pair = (
                    (team == "scripting" and category in SCRIPTING_CATEGORIES)
                    or (team == "video_editing" and category in VIDEO_CATEGORIES)
                    or (team == "operational" and category in OPERATIONAL_CATEGORIES)
                )
                if not valid_pair:
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
                        "version_label": comment.get("version_label") or (
                            f"v{comment.get('version_number')}" if comment.get("version_number") else ""
                        ),
                        "comment_id": comment.get("comment_id", ""),
                        "parent_comment_id": comment.get("parent_comment_id", ""),
                        "is_reply": bool(comment.get("is_reply")),
                        "commenter": comment.get("commenter", ""),
                        "comment_created_at": comment.get("created_at", ""),
                        "timecode": comment.get("timestamp", ""),
                        "comment_text": comment.get("text", ""),
                        "issue_index": issue_index,
                        "team": "review",
                        "ai_category": category,
                        "error_summary": comment.get("text", ""),
                        "ai_error_count": error_count,
                        "confidence": float(issue.get("confidence") or 0),
                        "needs_review": True,
                        "performance_eligible": False,
                        "classification_reason": (
                            f"AI returned an invalid team/category pair: {team} / {category}. "
                            "Manual team/category review is required."
                        ),
                        "pattern_label": issue.get("pattern_label", ""),
                        "ai_assignee": "",
                        "model_team": team,
                        "model_category": category,
                        "scriptwriter": project.get("scriptwriter", ""),
                        "science_video_editor": project.get("science_video_editor", ""),
                        "finishing_editor": project.get("finishing_editor", ""),
                        "rough_editor": project.get("rough_editor", ""),
                    })
                    continue

                if team == "operational":
                    assignee = ""
                    attribution_review = False
                elif team == "scripting":
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
                    or (team != "operational" and confidence < 0.78)
                    or attribution_review
                    or (team != "operational" and not extraction_verified)
                )
                # Category/wording uncertainty remains visible in Needs Review, but
                # a verified project with a clear contributor is counted provisionally.
                # Attribution blockers (handover/missing contributor) and extraction
                # uncertainty remain excluded until resolved.
                performance_eligible = (
                    team in {"scripting", "video_editing"}
                    and bool(assignee)
                    and extraction_verified
                    and not attribution_review
                )

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
                    "version_label": comment.get("version_label") or (
                        f"v{comment.get('version_number')}" if comment.get("version_number") else ""
                    ),
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
                    "error_summary": comment.get("text", ""),
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

from __future__ import annotations

from collections import Counter, defaultdict
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from src.qc_classifier import SCRIPTING_CATEGORIES, VIDEO_CATEGORIES

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(color="FFFFFF", bold=True)
STATIC_FONT = Font(color="666666")
IMPORTED_FONT = Font(color="008000")
INPUT_FONT = Font(color="0000FF")
FORMULA_FONT = Font(color="000000")
CAUTION_FILL = PatternFill("solid", fgColor="FCE4D6")


def _style_header(ws, row: int = 1) -> None:
    for cell in ws[row]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = f"A{row + 1}"


def _autofit(ws, min_width: int = 10, max_width: int = 48) -> None:
    for column_cells in ws.columns:
        letter = get_column_letter(column_cells[0].column)
        length = 0
        for cell in column_cells:
            value = cell.value
            if value is None:
                continue
            length = max(length, max(len(line) for line in str(value).split("\n")))
        ws.column_dimensions[letter].width = min(max(length + 2, min_width), max_width)


def _table(ws, name: str) -> None:
    if ws.max_row < 2 or ws.max_column < 1:
        return
    ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"
    table = Table(displayName=name, ref=ref)
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    ws.add_table(table)


def _write_description(ws, categories: dict[str, str]) -> None:
    ws.append(["Category", "Description"])
    for category, description in categories.items():
        ws.append([category, description])
    _style_header(ws)
    for row in ws.iter_rows(min_row=2):
        row[0].font = STATIC_FONT
        row[1].font = STATIC_FONT
        row[1].alignment = Alignment(wrap_text=True, vertical="top")
    _autofit(ws)


def _primary_editor(project: dict[str, str]) -> str:
    return project.get("science_video_editor") or project.get("finishing_editor") or project.get("rough_editor") or ""


def _project_counts(projects: list[dict[str, str]], team: str) -> Counter:
    counts = Counter()
    for project in projects:
        person = project.get("scriptwriter", "") if team == "scripting" else _primary_editor(project)
        if person:
            counts[person] += 1
    return counts


def _detail_headers(team: str) -> list[str]:
    base = [
        "Article ID", "Item Name", "Draft Script Sent", "Date Video Released",
        "Frame.io Review Link", "Version", "Version File", "Commenter", "Comment Date",
        "Timecode", "Reply?", "Frame.io Comment", "AI Error Summary", "AI Category",
        "AI Assignee",
    ]
    if team == "video_editing":
        base.extend(["Science Video Editor", "Finishing Editor", "Rough Editor"])
    base.extend([
        "AI Error Count", "Confidence", "Needs Review", "Manual Category Override",
        "Manual Assignee Override", "Manual Count Override", "Reviewer Notes",
        "Final Category", "Final Assignee", "Final Error Count", "Comment ID",
    ])
    return base


def _populate_detail(ws, error_rows: list[dict[str, Any]], team: str) -> dict[str, int]:
    headers = _detail_headers(team)
    ws.append(headers)
    _style_header(ws)
    index = {name: headers.index(name) + 1 for name in headers}

    for error in error_rows:
        if error.get("team") != team:
            continue
        row = [
            error.get("article_id", ""), error.get("item_name", ""),
            error.get("draft_script_sent", ""), error.get("date_video_released", ""),
            error.get("frameio_review_link", ""), error.get("version_number", ""),
            error.get("version_name", ""), error.get("commenter", ""),
            error.get("comment_created_at", ""), error.get("timecode", ""),
            "Yes" if error.get("is_reply") else "No", error.get("comment_text", ""),
            error.get("error_summary", ""), error.get("ai_category", ""), error.get("ai_assignee", ""),
        ]
        if team == "video_editing":
            row.extend([
                error.get("science_video_editor", ""),
                error.get("finishing_editor", ""),
                error.get("rough_editor", ""),
            ])
        row.extend([
            error.get("ai_error_count", 1), error.get("confidence", 0),
            "Yes" if error.get("needs_review") else "No", "", "", "", "", None, None, None,
            error.get("comment_id", ""),
        ])
        ws.append(row)
        r = ws.max_row
        ws.cell(r, index["Final Category"], f'=IF({get_column_letter(index["Manual Category Override"])}{r}<>"",{get_column_letter(index["Manual Category Override"])}{r},{get_column_letter(index["AI Category"])}{r})')
        ws.cell(r, index["Final Assignee"], f'=IF({get_column_letter(index["Manual Assignee Override"])}{r}<>"",{get_column_letter(index["Manual Assignee Override"])}{r},{get_column_letter(index["AI Assignee"])}{r})')
        ws.cell(r, index["Final Error Count"], f'=IF({get_column_letter(index["Manual Count Override"])}{r}<>"",{get_column_letter(index["Manual Count Override"])}{r},{get_column_letter(index["AI Error Count"])}{r})')

        for name in ("Manual Category Override", "Manual Assignee Override", "Manual Count Override", "Reviewer Notes"):
            ws.cell(r, index[name]).font = INPUT_FONT
        for name in ("Final Category", "Final Assignee", "Final Error Count"):
            ws.cell(r, index[name]).font = FORMULA_FONT
        for name in ("Article ID", "Item Name", "Draft Script Sent", "Date Video Released", "Frame.io Review Link", "Version", "Version File", "Commenter", "Comment Date", "Timecode", "Reply?", "Frame.io Comment", "AI Error Summary", "AI Category", "AI Assignee", "AI Error Count", "Confidence", "Comment ID"):
            ws.cell(r, index[name]).font = IMPORTED_FONT
        if error.get("needs_review"):
            ws.cell(r, index["Needs Review"]).fill = CAUTION_FILL

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    _autofit(ws)
    _table(ws, "ScriptingErrorDetail" if team == "scripting" else "VideoErrorDetail")
    return index


def _article_summary(ws, projects: list[dict[str, str]], error_rows: list[dict[str, Any]], team: str, categories: dict[str, str], detail_sheet_name: str, detail_index: dict[str, int], detail_max_row: int) -> None:
    person_label = "Scriptwriter" if team == "scripting" else "Video Editor"
    headers = ["Article ID", person_label, "Draft Script Sent", "Date Video Released"] + list(categories) + ["Total Errors", "Error Notes", "Needs Review"]
    ws.append(headers)
    _style_header(ws)

    detail_article = get_column_letter(detail_index["Article ID"])
    detail_category = get_column_letter(detail_index["Final Category"])
    detail_count = get_column_letter(detail_index["Final Error Count"])
    range_end = max(detail_max_row, 2)

    by_article: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in error_rows:
        if row.get("team") == team:
            by_article[str(row.get("article_id") or "")].append(row)

    for project in projects:
        article_id = project.get("article_id", "")
        person = project.get("scriptwriter", "") if team == "scripting" else _primary_editor(project)
        notes = "; ".join(dict.fromkeys(row.get("error_summary", "") for row in by_article.get(article_id, []) if row.get("error_summary")))
        needs_review = any(row.get("needs_review") for row in by_article.get(article_id, []))
        ws.append([article_id, person, project.get("draft_script_sent", ""), project.get("date_video_released", "")] + [None] * len(categories) + [None, notes, "Yes" if needs_review else "No"])
        r = ws.max_row
        article_cell = f"$A{r}"
        first_category_col = 5
        for offset, category in enumerate(categories):
            col = first_category_col + offset
            formula = (
                f'=SUMIFS(\'{detail_sheet_name}\'!${detail_count}$2:${detail_count}${range_end},'
                f'\'{detail_sheet_name}\'!${detail_article}$2:${detail_article}${range_end},{article_cell},'
                f'\'{detail_sheet_name}\'!${detail_category}$2:${detail_category}${range_end},"{category}")'
            )
            ws.cell(r, col, formula)
            ws.cell(r, col).font = FORMULA_FONT
        total_col = first_category_col + len(categories)
        ws.cell(r, total_col, f"=SUM({get_column_letter(first_category_col)}{r}:{get_column_letter(total_col-1)}{r})")
        ws.cell(r, total_col).font = FORMULA_FONT

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    _autofit(ws)
    _table(ws, "ScriptingArticleQC" if team == "scripting" else "VideoArticleQC")


def _summary_sheet(ws, projects: list[dict[str, str]], error_rows: list[dict[str, Any]], team: str, categories: dict[str, str]) -> None:
    project_counts = _project_counts(projects, team)
    people = sorted(project_counts)
    headers = ["Contributor", "Projects", "AI Errors", "Errors / Article", "Projects With AI Errors"] + list(categories)
    ws.append(headers)
    _style_header(ws)

    errors_by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in error_rows:
        if row.get("team") == team and row.get("ai_assignee"):
            errors_by_person[str(row["ai_assignee"])].append(row)

    for person in people:
        rows = errors_by_person.get(person, [])
        total = sum(int(row.get("ai_error_count") or 0) for row in rows)
        article_ids = {row.get("article_id") for row in rows if row.get("article_id") and int(row.get("ai_error_count") or 0) > 0}
        values: list[Any] = [person, project_counts[person], total, total / project_counts[person] if project_counts[person] else 0, len(article_ids)]
        for category in categories:
            values.append(sum(int(row.get("ai_error_count") or 0) for row in rows if row.get("ai_category") == category))
        ws.append(values)

    for row in ws.iter_rows(min_row=2):
        row[0].font = STATIC_FONT
        for cell in row[1:]:
            cell.font = FORMULA_FONT
    if ws.max_row >= 2:
        for cell in ws["D"][1:]:
            cell.number_format = "0.00"
    _autofit(ws)
    _table(ws, "WriterSummary" if team == "scripting" else "EditorSummary")


def _monthly_trend(ws, year: int, month: int, projects: list[dict[str, str]], error_rows: list[dict[str, Any]], team: str) -> None:
    ws.append(["Month", "Projects", "Total AI Errors", "Errors / Article", "Projects With AI Errors"])
    project_counts = _project_counts(projects, team)
    relevant = [row for row in error_rows if row.get("team") == team]
    total_errors = sum(int(row.get("ai_error_count") or 0) for row in relevant)
    article_ids = {row.get("article_id") for row in relevant if row.get("article_id") and int(row.get("ai_error_count") or 0) > 0}
    project_total = sum(project_counts.values())
    ws.append([f"{year:04d}-{month:02d}", project_total, total_errors, total_errors / project_total if project_total else 0, len(article_ids)])
    _style_header(ws)
    ws["D2"].number_format = "0.00"
    _autofit(ws)


def _contributor_trend(ws, year: int, month: int, projects: list[dict[str, str]], error_rows: list[dict[str, Any]], team: str) -> None:
    ws.append(["Month", "Contributor", "Projects", "AI Errors", "Errors / Article", "Projects With AI Errors"])
    project_counts = _project_counts(projects, team)
    errors_by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in error_rows:
        if row.get("team") == team and row.get("ai_assignee"):
            errors_by_person[str(row["ai_assignee"])].append(row)
    for person in sorted(project_counts):
        rows = errors_by_person.get(person, [])
        total = sum(int(row.get("ai_error_count") or 0) for row in rows)
        article_ids = {row.get("article_id") for row in rows if row.get("article_id") and int(row.get("ai_error_count") or 0) > 0}
        projects_count = project_counts[person]
        ws.append([f"{year:04d}-{month:02d}", person, projects_count, total, total / projects_count if projects_count else 0, len(article_ids)])
    _style_header(ws)
    for cell in ws["E"][1:]:
        cell.number_format = "0.00"
    _autofit(ws)
    _table(ws, "WriterMonthlyTrend" if team == "scripting" else "EditorMonthlyTrend")


def _needs_review(ws, error_rows: list[dict[str, Any]], team: str, unresolved: list[dict[str, Any]]) -> None:
    ws.append(["Article ID", "Type", "Reason", "Comment", "AI Category", "AI Assignee", "Confidence"])
    for bundle in unresolved:
        project = bundle.get("project") or {}
        resolution = bundle.get("resolution")
        if resolution and resolution.status != "resolved":
            ws.append([project.get("article_id", ""), "Frame.io Resolution", resolution.note, "", "", "", ""])
    for row in error_rows:
        if row.get("needs_review") and row.get("team") in {team, "review"}:
            issue_type = (
                "OpenAI Classification Failure"
                if row.get("team") == "review"
                else "Classification / Attribution"
            )
            ws.append([
                row.get("article_id", ""),
                issue_type,
                row.get("classification_reason", ""),
                row.get("comment_text", ""),
                row.get("ai_category", ""),
                row.get("ai_assignee", ""),
                row.get("confidence", 0),
            ])
    _style_header(ws)
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        row[1].fill = CAUTION_FILL
    _autofit(ws)


def _raw_comments(ws, bundles: list[dict[str, Any]]) -> None:
    ws.append(["Article ID", "Version", "Version File", "Comment ID", "Parent Comment ID", "Reply?", "Commenter", "Created At", "Timecode", "Comment"])
    for bundle in bundles:
        for comment in bundle.get("comments") or []:
            ws.append([comment.get("article_id", ""), comment.get("version_number", ""), comment.get("version_name", ""), comment.get("comment_id", ""), comment.get("parent_comment_id", ""), "Yes" if comment.get("is_reply") else "No", comment.get("commenter", ""), comment.get("created_at", ""), comment.get("timestamp", ""), comment.get("text", "")])
    _style_header(ws)
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.font = IMPORTED_FONT
            cell.alignment = Alignment(vertical="top", wrap_text=True)
    _autofit(ws)
    ws.sheet_state = "hidden"


def _analysis_guide(ws) -> None:
    rows = [
        ("Purpose", "Monthly performance overview generated from Frame.io QC comments linked through Monday.com."),
        ("Version rule", "Version 1 and the final/latest version are excluded. Every intermediate version is checked."),
        ("Comment rule", "Top-level comments and replies are analyzed; non-error acknowledgements/replies are not counted."),
        ("Atomic counting", "A comment can contain multiple corrections. Separate categories are split; clearly multiple same-category occurrences can have a count above 1."),
        ("Attribution", "Scripting errors are assigned to the Monday Scriptwriter. Video-editing errors use the Science Video Editor first, then Finishing Editor, then Rough Editor; multi-editor handovers are flagged for review."),
        ("Manual correction", "Use the blue Manual Override columns in Error Detail to correct category, assignee, count, or add reviewer notes. Article-category totals recalculate from the Final columns."),
        ("Needs Review", "Low-confidence, ambiguous, unresolved Frame.io links, and multi-editor attribution cases are surfaced for manual review rather than guessed."),
        ("Previous Comments Unaddressed", "Counted as a new video-editing error only when a later comment explicitly indicates an earlier requested correction remains unresolved."),
    ]
    ws.append(["Rule", "Definition"])
    for row in rows:
        ws.append(row)
    _style_header(ws)
    for row in ws.iter_rows(min_row=2):
        row[0].font = STATIC_FONT
        row[1].font = STATIC_FONT
        row[1].alignment = Alignment(wrap_text=True, vertical="top")
    _autofit(ws)


def _common_patterns(ws, error_rows: list[dict[str, Any]]) -> None:
    ws.append(["Category", "Error Pattern", "Total Count", "Articles", "Editors"])
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for row in error_rows:
        if row.get("team") != "video_editing":
            continue
        key = (str(row.get("ai_category") or ""), str(row.get("error_summary") or ""))
        bucket = groups.setdefault(key, {"count": 0, "articles": set(), "editors": set()})
        bucket["count"] += int(row.get("ai_error_count") or 0)
        if row.get("article_id"):
            bucket["articles"].add(row["article_id"])
        if row.get("ai_assignee"):
            bucket["editors"].add(row["ai_assignee"])
    for (category, pattern), bucket in sorted(groups.items(), key=lambda item: (-item[1]["count"], item[0])):
        ws.append([category, pattern, bucket["count"], len(bucket["articles"]), len(bucket["editors"])])
    _style_header(ws)
    _autofit(ws)


def build_report(*, team: str, year: int, month: int, projects: list[dict[str, str]], bundles: list[dict[str, Any]], error_rows: list[dict[str, Any]]) -> bytes:
    if team not in {"scripting", "video_editing"}:
        raise ValueError("team must be scripting or video_editing")

    categories = SCRIPTING_CATEGORIES if team == "scripting" else VIDEO_CATEGORIES
    wb = Workbook()
    wb.remove(wb.active)

    detail = wb.create_sheet("Error Detail", 0)
    detail_index = _populate_detail(detail, error_rows, team)

    summary_sheet_name = "QC by Draft Date" if team == "scripting" else "Video QC by Release Date"
    article = wb.create_sheet(summary_sheet_name, 0)
    _article_summary(article, projects, error_rows, team, categories, "Error Detail", detail_index, detail.max_row)

    description = wb.create_sheet("Description")
    _write_description(description, categories)

    trend = wb.create_sheet("Monthly Trend")
    _monthly_trend(trend, year, month, projects, error_rows, team)

    summary = wb.create_sheet("Writer Summary" if team == "scripting" else "Editor Summary")
    _summary_sheet(summary, projects, error_rows, team, categories)

    contributor_trend = wb.create_sheet("Writer Monthly Trend" if team == "scripting" else "Editor Monthly Trend")
    _contributor_trend(contributor_trend, year, month, projects, error_rows, team)

    if team == "video_editing":
        patterns = wb.create_sheet("Common Error Patterns")
        _common_patterns(patterns, error_rows)

    review = wb.create_sheet("Needs Review")
    unresolved = [bundle for bundle in bundles if bundle.get("resolution") and bundle["resolution"].status != "resolved"]
    _needs_review(review, error_rows, team, unresolved)

    guide = wb.create_sheet("Analysis Guide")
    _analysis_guide(guide)

    raw = wb.create_sheet("Raw Frame.io Comments")
    _raw_comments(raw, bundles)

    output = BytesIO()
    wb.save(output)
    return output.getvalue()

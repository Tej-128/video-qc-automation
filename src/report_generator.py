from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from io import BytesIO
from typing import Any

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from src.qc_classifier import SCRIPTING_CATEGORIES, VIDEO_CATEGORIES
from src.version import BUILD_LABEL, BUILD_VERSION

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
HEADER_FONT = Font(color="FFFFFF", bold=True)
TOTAL_FILL = PatternFill("solid", fgColor="D9EAF7")
TOTAL_FONT = Font(color="17365D", bold=True)
CAUTION_FILL = PatternFill("solid", fgColor="FCE4D6")
THIN = Side(style="thin", color="D9E2F3")
BOTTOM_BORDER = Border(bottom=THIN)


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%b %d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _month_key(value: Any) -> str:
    parsed = _as_date(value)
    return parsed.strftime("%Y-%m") if parsed else "Unknown"


def _style_header(ws, headers: list[str]) -> None:
    for idx, header in enumerate(headers, 1):
        cell = ws.cell(1, idx, header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.border = BOTTOM_BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 42
    ws.sheet_view.showGridLines = False


def _unique_texts(rows: list[dict[str, Any]]) -> list[str]:
    result: list[str] = []
    for row in rows:
        text = str(row.get("error_summary") or row.get("comment_text") or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _distinct_editors(project: dict[str, Any]) -> list[str]:
    values = [
        str(project.get("science_video_editor") or "").strip(),
        str(project.get("finishing_editor") or "").strip(),
        str(project.get("rough_editor") or "").strip(),
    ]
    return [value for index, value in enumerate(values) if value and value not in values[:index]]


def _article_contributor(project: dict[str, Any], team: str) -> str:
    if team == "scripting":
        return str(project.get("scriptwriter") or "").strip()
    editors = _distinct_editors(project)
    if len(editors) == 1:
        return editors[0]
    if not editors:
        return ""
    return "Needs Review"


def _category_config(team: str) -> dict[str, str]:
    return SCRIPTING_CATEGORIES if team == "scripting" else VIDEO_CATEGORIES


def _team_config(team: str) -> dict[str, str]:
    if team == "scripting":
        return {
            "main_sheet": "QC by Draft Date",
            "contributor": "Writer",
            "date_header": "Date Script Draft Sent",
            "date_key": "draft_script_sent",
            "summary": "Writer Summary",
            "contributor_trend": "Writer Monthly Trend",
        }
    return {
        "main_sheet": "Video QC by Release Date",
        "contributor": "Video Editor",
        "date_header": "Date Video Released",
        "date_key": "date_video_released",
        "summary": "Editor Summary",
        "contributor_trend": "Editor Monthly Trend",
    }


def _main_sheet(wb, projects, error_rows, team):
    cfg = _team_config(team)
    categories = _category_config(team)
    ws = wb.create_sheet(cfg["main_sheet"])
    headers = ["Article Number", cfg["contributor"], cfg["date_header"]]
    for category in categories:
        headers.extend([category, "QC Comment"])
    headers.append("Total Errors")
    _style_header(ws, headers)

    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 23
    ws.column_dimensions["C"].width = 20
    comment_widths = [53, 48, 48, 48]
    col = 4
    for index, _ in enumerate(categories):
        ws.column_dimensions[ws.cell(1, col).column_letter].width = 30 if col not in (4, 10) else 23
        ws.column_dimensions[ws.cell(1, col + 1).column_letter].width = comment_widths[min(index, len(comment_widths) - 1)]
        col += 2
    ws.column_dimensions[ws.cell(1, col).column_letter].width = 14

    by_article: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in error_rows:
        if row.get("team") == team:
            by_article[str(row.get("article_id") or "")].append(row)

    for project in projects:
        article_id = str(project.get("article_id") or "")
        rows = by_article.get(article_id, [])
        values: list[Any] = [
            project.get("item_name") or article_id,
            _article_contributor(project, team),
            _as_date(project.get(cfg["date_key"])),
        ]
        total = 0
        for category in categories:
            category_rows = [row for row in rows if row.get("ai_category") == category]
            count = sum(int(row.get("ai_error_count") or 0) for row in category_rows)
            total += count
            comments = _unique_texts(category_rows)
            values.extend([count, "; ".join(comments) if comments else None])
        values.append(total)
        ws.append(values)

        row_number = ws.max_row
        ws.row_dimensions[row_number].height = 72
        ws.cell(row_number, 3).number_format = "yyyy-mm-dd"
        for column in range(4, len(headers), 2):
            ws.cell(row_number, column).alignment = Alignment(horizontal="center", vertical="center")
            if column + 1 < len(headers):
                ws.cell(row_number, column + 1).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(row_number, len(headers)).alignment = Alignment(horizontal="center", vertical="center")
        ws.cell(row_number, len(headers)).fill = TOTAL_FILL
        ws.cell(row_number, len(headers)).font = TOTAL_FONT
        if team == "video_editing" and _article_contributor(project, team) == "Needs Review":
            ws.cell(row_number, 2).fill = CAUTION_FILL

    return ws


def _description_sheet(wb, team):
    categories = _category_config(team)
    ws = wb.create_sheet("Description")
    ws.append(["Error Category", "Description"])
    if team == "scripting":
        order = [
            "Scientific Accuracy & Completeness",
            "Language & Narration",
            "Titles & On-screen Text",
            "Scientific Formatting",
        ]
    else:
        order = list(categories)
    for category in order:
        ws.append([category, categories[category]])
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 95
    return ws


def _article_error_map(error_rows, team):
    by_article = defaultdict(list)
    for row in error_rows:
        if row.get("team") == team:
            by_article[str(row.get("article_id") or "")].append(row)
    return by_article


def _monthly_trend_sheet(wb, projects, error_rows, team):
    cfg = _team_config(team)
    categories = _category_config(team)
    ws = wb.create_sheet("Monthly Trend")
    headers = ["Month", "Articles", *categories.keys(), "Total Errors", "Errors per Article"]
    _style_header(ws, headers)
    ws.column_dimensions["A"].width = 13
    ws.column_dimensions["B"].width = 18

    by_article = _article_error_map(error_rows, team)
    grouped: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"articles": set(), **{category: 0 for category in categories}}
    )

    for project in projects:
        month = _month_key(project.get(cfg["date_key"]))
        article_id = str(project.get("article_id") or "")
        grouped[month]["articles"].add(article_id)
        for row in by_article.get(article_id, []):
            category = row.get("ai_category")
            if category in categories:
                grouped[month][category] += int(row.get("ai_error_count") or 0)

    for month in sorted(grouped):
        data = grouped[month]
        counts = [data[category] for category in categories]
        total = sum(counts)
        articles = len(data["articles"])
        ws.append([month, articles, *counts, total, total / articles if articles else 0])

    if ws.max_row > 1:
        total_col = 3 + len(categories)
        ws.cell(
            ws.max_row + 1,
            total_col,
            sum(ws.cell(row, total_col).value or 0 for row in range(2, ws.max_row + 1)),
        )
        for row in ws.iter_rows(min_row=2, max_col=len(headers)):
            for cell in row[1:-1]:
                cell.alignment = Alignment(horizontal="center")
            row[-1].number_format = "0.00"

        chart = BarChart()
        chart.type = "col"
        chart.style = 10
        chart.height = 8
        chart.width = 10
        data = Reference(ws, min_col=total_col, min_row=1, max_row=ws.max_row - 1)
        categories_ref = Reference(ws, min_col=1, min_row=2, max_row=ws.max_row - 1)
        chart.add_data(data, titles_from_data=True)
        chart.set_categories(categories_ref)
        chart.legend = None
        chart.y_axis.title = "Total Errors"
        chart.x_axis.title = "Month"
        ws.add_chart(chart, "I1")

    return ws


def _performance_project_counts(projects, team):
    counts = Counter()
    for project in projects:
        person = _article_contributor(project, team)
        if person and person != "Needs Review":
            counts[person] += 1
    return counts


def _performance_errors(error_rows, team):
    return [
        row
        for row in error_rows
        if row.get("team") == team
        and row.get("performance_eligible", not row.get("needs_review", False))
        and row.get("ai_assignee")
    ]


def _summary_sheet(wb, projects, error_rows, team):
    cfg = _team_config(team)
    categories = _category_config(team)
    ws = wb.create_sheet(cfg["summary"])
    headers = [cfg["contributor"], "Articles", *categories.keys(), "Total Errors", "Errors per Article"]
    _style_header(ws, headers)

    project_counts = _performance_project_counts(projects, team)
    category_counts = defaultdict(Counter)

    for row in _performance_errors(error_rows, team):
        category_counts[str(row.get("ai_assignee"))][str(row.get("ai_category"))] += int(
            row.get("ai_error_count") or 0
        )

    rows = []
    for person, articles in project_counts.items():
        counts = [category_counts[person][category] for category in categories]
        total = sum(counts)
        rows.append([person, articles, *counts, total, total / articles if articles else 0])

    rows.sort(key=lambda row: (-row[-2], row[0]))
    for row in rows:
        ws.append(row)

    for column in ws.iter_cols(
        min_col=len(headers), max_col=len(headers), min_row=2, max_row=ws.max_row
    ):
        for cell in column:
            cell.number_format = "0.00"

    return ws


def _contributor_monthly_sheet(wb, projects, error_rows, team):
    cfg = _team_config(team)
    categories = _category_config(team)
    ws = wb.create_sheet(cfg["contributor_trend"])
    headers = [cfg["contributor"], "Month", "Articles", *categories.keys(), "Total Errors", "Errors per Article"]
    _style_header(ws, headers)

    project_map = {str(project.get("article_id") or ""): project for project in projects}
    project_counts = defaultdict(set)

    for project in projects:
        person = _article_contributor(project, team)
        if person and person != "Needs Review":
            key = (person, _month_key(project.get(cfg["date_key"])))
            project_counts[key].add(str(project.get("article_id") or ""))

    category_counts = defaultdict(Counter)
    for row in _performance_errors(error_rows, team):
        project = project_map.get(str(row.get("article_id") or ""), {})
        key = (
            str(row.get("ai_assignee") or ""),
            _month_key(project.get(cfg["date_key"])),
        )
        category_counts[key][str(row.get("ai_category"))] += int(
            row.get("ai_error_count") or 0
        )

    ranges = {}
    contributors = sorted({key[0] for key in project_counts})
    cursor = 2

    for index, contributor in enumerate(contributors):
        start = cursor
        keys = sorted(
            [key for key in project_counts if key[0] == contributor],
            key=lambda key: key[1],
        )
        for key in keys:
            counts = [category_counts[key][category] for category in categories]
            total = sum(counts)
            articles = len(project_counts[key])
            ws.append(
                [
                    contributor,
                    key[1],
                    articles,
                    *counts,
                    total,
                    total / articles if articles else 0,
                ]
            )
            cursor += 1
        ranges[contributor] = (start, cursor - 1)
        if index != len(contributors) - 1:
            ws.append([None] * len(headers))
            cursor += 1

    for column in ws.iter_cols(
        min_col=len(headers), max_col=len(headers), min_row=2, max_row=ws.max_row
    ):
        for cell in column:
            cell.number_format = "0.00"

    candidates = []
    total_col = 4 + len(categories)
    metric_col = total_col + 1

    for person, (start, end) in ranges.items():
        if end - start + 1 >= 2:
            latest = str(ws.cell(end, 2).value or "")
            total = sum(int(ws.cell(row, total_col).value or 0) for row in range(start, end + 1))
            candidates.append((latest, total, person, start, end))

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    positions = ["I6", "I21", "M6", "M21"]
    title_alias = {
        "Pallavi Sharma": "Pallavi",
        "Poornima G": "Poornima",
        "Kathyayani Sridharan": "KS",
        "Sri Lakshmi Priya": "SLP",
    }

    for position, (_, _, person, start, end) in zip(positions, candidates[:4]):
        chart = BarChart()
        chart.type = "col"
        chart.style = 10
        chart.height = 7.5
        chart.width = 8
        data = Reference(ws, min_col=metric_col, min_row=start, max_row=end)
        categories_ref = Reference(ws, min_col=2, min_row=start, max_row=end)
        chart.add_data(data, titles_from_data=False)
        chart.set_categories(categories_ref)
        chart.title = title_alias.get(person, person)
        chart.legend = None
        chart.y_axis.title = "Errors per Article"
        ws.add_chart(chart, position)

    return ws


def _analysis_sheet(wb, error_rows, team):
    categories = _category_config(team)
    ws = wb.create_sheet("Analysis Guide")
    _style_header(ws, ["Analysis Item", "Result / Interpretation"])
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 95

    totals = Counter()
    for row in error_rows:
        if row.get("team") == team:
            totals[str(row.get("ai_category"))] += int(row.get("ai_error_count") or 0)

    rows = [("Verified grand total", sum(totals.values()))]
    for category in categories:
        rows.append((category, totals[category]))

    rows.extend(
        [
            (
                "How to assess improvement",
                "Use Errors per Article and its month-over-month change. Raw counts should be read together with the number of articles completed.",
            ),
            (
                "Important limitation",
                "Rows flagged Needs Review are kept out of contributor performance totals until attribution/classification is confirmed.",
            ),
        ]
    )

    for row in rows:
        ws.append(row)

    for row_number in range(2, ws.max_row + 1):
        ws.row_dimensions[row_number].height = 38
        ws.cell(row_number, 1).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(row_number, 2).alignment = Alignment(wrap_text=True, vertical="top")

    return ws


def _needs_review_sheet(wb, bundles, error_rows):
    ws = wb.create_sheet("Needs Review")
    headers = [
        "Article Number",
        "Type",
        "Reason",
        "Comment",
        "AI Category",
        "AI Assignee",
        "Confidence",
        "Performance Counted?",
    ]
    _style_header(ws, headers)

    for bundle in bundles:
        resolution = bundle.get("resolution")
        project = bundle.get("project") or {}
        if resolution is not None and getattr(resolution, "status", "") != "resolved":
            ws.append(
                [
                    project.get("item_name") or project.get("article_id"),
                    "Frame.io Resolution",
                    getattr(resolution, "note", ""),
                    "",
                    "",
                    "",
                    "",
                    "No",
                ]
            )

    for row in error_rows:
        if row.get("needs_review") or not row.get("performance_eligible", True):
            ws.append(
                [
                    row.get("item_name") or row.get("article_id"),
                    "Classification / Attribution",
                    row.get("classification_reason") or "Needs manual verification",
                    row.get("comment_text") or "",
                    row.get("ai_category") or "",
                    row.get("ai_assignee") or "",
                    row.get("confidence", ""),
                    "Yes" if row.get("performance_eligible", False) else "No",
                ]
            )

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        row[1].fill = CAUTION_FILL

    return ws


def _error_detail_sheet(wb, error_rows):
    ws = wb.create_sheet("Error Detail")
    headers = [
        "Article Number",
        "Team",
        "Category",
        "Error Count",
        "Error Summary",
        "AI Assignee",
        "Performance Eligible",
        "Needs Review",
        "Confidence",
        "Version",
        "Comment",
        "Comment ID",
        "Pattern Label",
    ]
    _style_header(ws, headers)

    for row in error_rows:
        ws.append(
            [
                row.get("item_name") or row.get("article_id"),
                row.get("team"),
                row.get("ai_category"),
                row.get("ai_error_count"),
                row.get("error_summary"),
                row.get("ai_assignee"),
                "Yes" if row.get("performance_eligible", False) else "No",
                "Yes" if row.get("needs_review") else "No",
                row.get("confidence", ""),
                row.get("version_number", ""),
                row.get("comment_text", ""),
                row.get("comment_id", ""),
                row.get("pattern_label", ""),
            ]
        )

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    ws.sheet_state = "hidden"
    return ws


def _raw_comments_sheet(wb, bundles):
    ws = wb.create_sheet("Raw Frame.io Comments")
    headers = [
        "Article Number",
        "Version",
        "Version File",
        "Comment ID",
        "Parent Comment ID",
        "Reply?",
        "Commenter",
        "Created At",
        "Timecode",
        "Comment",
    ]
    _style_header(ws, headers)

    for bundle in bundles:
        project = bundle.get("project") or {}
        for row in bundle.get("comments") or []:
            ws.append(
                [
                    project.get("item_name") or row.get("article_id"),
                    row.get("version_number"),
                    row.get("version_name"),
                    row.get("comment_id"),
                    row.get("parent_comment_id"),
                    "Yes" if row.get("is_reply") else "No",
                    row.get("commenter"),
                    row.get("created_at"),
                    row.get("timestamp"),
                    row.get("text"),
                ]
            )

    ws.sheet_state = "hidden"
    return ws


def _pattern_sheet(wb, error_rows):
    ws = wb.create_sheet("Common Error Patterns")
    _style_header(ws, ["Category", "Recurring Pattern", "Total Count", "Articles", "Editors"])

    grouped = defaultdict(lambda: {"count": 0, "articles": set(), "editors": set()})
    for row in error_rows:
        if row.get("team") != "video_editing":
            continue
        pattern = str(row.get("pattern_label") or row.get("ai_category") or "").strip()
        key = (str(row.get("ai_category") or ""), pattern)
        grouped[key]["count"] += int(row.get("ai_error_count") or 0)
        if row.get("article_id"):
            grouped[key]["articles"].add(row.get("article_id"))
        if row.get("ai_assignee"):
            grouped[key]["editors"].add(row.get("ai_assignee"))

    for (category, pattern), info in sorted(
        grouped.items(), key=lambda item: (-item[1]["count"], item[0])
    ):
        ws.append(
            [
                category,
                pattern,
                info["count"],
                len(info["articles"]),
                len(info["editors"]),
            ]
        )

    return ws



def _build_info_sheet(wb, team: str, year: int, month: int) -> None:
    ws = wb.create_sheet("_Build Info")
    ws.append(["Build Version", BUILD_VERSION])
    ws.append(["Build Label", BUILD_LABEL])
    ws.append(["Report Team", team])
    ws.append(["Selected Release Month", f"{year:04d}-{month:02d}"])
    ws.append(["Reference", "Swati June QC format + approved Video QC error sheet"])
    ws.sheet_state = "hidden"


def build_report(
    *,
    team: str,
    year: int,
    month: int,
    projects: list[dict[str, Any]],
    bundles: list[dict[str, Any]],
    error_rows: list[dict[str, Any]],
) -> bytes:
    if team not in {"scripting", "video_editing"}:
        raise ValueError("team must be scripting or video_editing")

    wb = Workbook()
    wb.remove(wb.active)

    _main_sheet(wb, projects, error_rows, team)
    _description_sheet(wb, team)
    _monthly_trend_sheet(wb, projects, error_rows, team)
    _summary_sheet(wb, projects, error_rows, team)
    _contributor_monthly_sheet(wb, projects, error_rows, team)

    if team == "video_editing":
        _pattern_sheet(wb, error_rows)

    _analysis_sheet(wb, error_rows, team)
    _needs_review_sheet(wb, bundles, error_rows)
    _error_detail_sheet(wb, error_rows)
    _raw_comments_sheet(wb, bundles)
    _build_info_sheet(wb, team, year, month)

    output = BytesIO()
    wb.save(output)
    return output.getvalue()

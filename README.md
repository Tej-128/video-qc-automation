# Video QC Automation

Production-oriented monthly QC reporting for JoVE Video Production.

## Flow

**Streamlit → Monday.com → Frame.io → OpenAI → 2 Excel reports**

1. Select report month/year.
2. Read the monthly project population from Monday board `1662638864`.
3. Use each row's existing `frame.io Review Link` as the primary asset mapping.
4. Resolve the corresponding Frame.io version stack under **Projects for Review**.
5. Exclude Version 1 and the final/latest version; extract comments from every intermediate version.
6. Classify atomic QC errors with OpenAI into the approved scripting/video-editing categories.
7. Attribute scripting errors to the Monday Scriptwriter; flag ambiguous video-editor handovers for manual review.
8. Generate separate Scripting QC and Video Editing QC Excel workbooks.

## Output

- `YYYY_MM_Scripting_QC.xlsx`
- `YYYY_MM_Video_Editing_QC.xlsx`

Each workbook contains article-level QC, contributor summaries/trends, editable manual overrides, a Needs Review sheet, an analysis guide, and a hidden raw-comment audit sheet.

## Streamlit secrets

These values must be configured in Streamlit Community Cloud and must never be committed:

- `FRAMEIO_CLIENT_ID`
- `FRAMEIO_CLIENT_SECRET`
- `MONDAY_API_TOKEN`
- `OPENAI_API_KEY`
- Optional: `OPENAI_MODEL` (defaults to `gpt-5.6-terra`)

## Security

This repository is public, so it contains code only. Production credentials, Frame.io tokens, Monday data, QC comments, generated reports, and internal spreadsheets must not be committed or uploaded as public GitHub Actions artifacts.

The production workflow is read-only against Monday.com and Frame.io.

# Video QC Automation

Read-only-first automation for generating monthly Video QC reports from Monday.com, Frame.io, and OpenAI.

## Current milestone

**Phase 1: Monday.com read-only extraction**

The first workflow only reads board metadata and sample rows from Monday.com board `1662638864`. It does not create, update, or delete Monday.com data.

## Security

This repository is public. Never commit API keys, access tokens, Frame.io credentials, OpenAI keys, QC source data, or generated production reports.

Secrets belong in GitHub Actions / Streamlit secrets only.

## Planned flow

Monday.com -> Frame.io review link -> version/comment extraction -> QC classification -> contributor attribution -> two Excel reports.

## Required GitHub secret for Phase 1

- `MONDAY_API_TOKEN`

The remaining secrets will be added only when those integrations are built.

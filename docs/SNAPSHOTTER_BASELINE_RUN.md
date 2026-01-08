# Snapshotter — Baseline Run (Replit)

This document verifies the Snapshotter execution harness is “known-good” before deeper changes.

## Prereqs

Environment variables required:
- `SNAPSHOTTER_REPO_URL`
- `SNAPSHOTTER_S3_BUCKET`

Recommended:
- `SNAPSHOTTER_REF` (default: `main`)
- `SNAPSHOTTER_S3_PREFIX` (default: `repo-scans/snapshotter`)
- `SNAPSHOTTER_DRY_RUN` (recommended `true` for baseline)

AWS credentials (v0.1):
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION` (optional; boto3 can infer)

## Outputs (Local + S3)

Snapshotter generates these artifacts:
- `repo_index.json`
- `artifact_manifest.json`
- `ARCHITECTURE_SUMMARY_SNAPSHOT.json`
- `GAPS_AND_INCONSISTENCIES.json`
- `ONBOARDING.md`

Local output directory (workspace):
- `out/<repo_slug>/<timestamp_utc>/<job_id>/...`

Local clone directory (workspace):
- `.snapshotter_tmp/repo`

S3 upload prefix:
- `<SNAPSHOTTER_S3_PREFIX>/<repo_slug>/<timestamp_utc>/<job_id>/...`

## Baseline Run (Dry-run)

Set:
- `SNAPSHOTTER_DRY_RUN=true`

Run:
```bash
uv run python main.py
```

Expected:
- Process prints a single JSON object with "ok": true
- "stage" is "done_dry_run"
- "artifacts" includes local paths to the 5 artifacts above
- No S3 uploads occur in dry-run mode

## Baseline Run (Real upload)
Set:
- `SNAPSHOTTER_DRY_RUN=false`

Run:

```bash
uv run python main.py
```

Expected:
- Process prints a single JSON object with `"ok": true`
- `"stage"` is `"done"`
- `"artifacts"` contains `s3://...` URIs for the 5 artifacts above
- Objects exist in S3 under the computed job prefix

## Failure contract
On failure, Snapshotter prints:
- `ok=false`
- `stage`
- `error_code`
- `error_message`
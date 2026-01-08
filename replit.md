# Repo Snapshot Uploader (Snapshotter)

## Overview
Snapshotter clones a git repository, builds a deterministic repo index + stub Pass 2 artifacts, validates them, and uploads artifacts to AWS S3 using server-side encryption (SSE-S3 AES256).

## Project Structure
```
├── main.py                  # Entry point - orchestrates job parse, clone, pass1, validate, upload
├── pyproject.toml           # uv/Python project config
├── snapshotter/
│   ├── job.py               # Job configuration models (Pydantic)
│   ├── git_ops.py           # Git clone/checkout operations
│   ├── pass1.py             # Pass 1 repo index builder (bounded scan)
│   ├── read_plan.py         # Deterministic Pass 1 read-plan suggestions
│   ├── s3_uploader.py       # S3 upload utilities (SSE=AES256 enforced)
│   └── utils.py             # Helpers (timestamps, hashing, repo slug)
│   └── validate_basic.py    # Basic artifact sanity validation
```

## Canonical Job Input (Required)

Snapshotter accepts **one** canonical job payload. Provide it using **one** of:

- `SNAPSHOTTER_JOB_JSON` — JSON string payload (Replit Secrets friendly)
- stdin — JSON payload piped to stdin (recommended for LangGraph)
- `SNAPSHOTTER_JOB_FILE` — path to a JSON file in the workspace

The job payload is parsed into the `Job` model (`snapshotter/job.py`) and is the authoritative input for:
- `repo_url`, `ref`, `mode`
- `limits`, `filters`
- `output` (S3 bucket + prefix)
- `metadata`

## Environment Variables

### Required (AWS)
- `AWS_ACCESS_KEY_ID` - AWS access key
- `AWS_SECRET_ACCESS_KEY` - AWS secret key
- `AWS_REGION` - AWS region (optional; boto3 can infer)

### Optional (dev convenience only)
- `SNAPSHOTTER_DRY_RUN` - Set to "true" to skip S3 upload

> Note: Repo + output config should come from the job payload (not separate env vars).

## Running

### Replit (payload in env)
```bash
uv run python main.py
```

### Stdin (LangGraph-friendly)
```bash
echo '{"repo_url":"https://github.com/stepan-o/fruitful-lab.git","ref":"main","mode":"full","limits":{"max_file_bytes":10485760,"max_total_bytes":262144000,"max_files":20000},"filters":{"deny_dirs":["node_modules",".git",".next","dist","build",".venv"],"deny_file_regex":["(?i).*\\.pem$","(?i).*\\.key$","(?i).*id_rsa$"],"allow_exts":["*"]},"output":{"s3_bucket":"YOUR_BUCKET","s3_prefix":"repo-scans/snapshotter"},"metadata":{"triggered_by":"langgraph","notes":"stdin run"}}' \
| SNAPSHOTTER_DRY_RUN=true uv run python main.py
```

## Outputs
### Local workspace structure
Artifacts are written to:

```
out/<repo_slug>/<timestamp_utc>/<job_id>/
├── repo_index.json
├── artifact_manifest.json
├── ARCHITECTURE_SUMMARY_SNAPSHOT.json
├── GAPS_AND_INCONSISTENCIES.json
└── ONBOARDING.md
```

Local clone directory:
- `.snapshotter_tmp/repo`

### S3 output structure
Artifacts are uploaded to:

```
s3://{bucket}/{s3_prefix}/{repo_slug}/{timestamp_utc}/{job_id}/
├── repo_index.json
├── artifact_manifest.json
├── ARCHITECTURE_SUMMARY_SNAPSHOT.json
├── GAPS_AND_INCONSISTENCIES.json
└── ONBOARDING.md
```

### Result Contract (stdout)
On success (dry-run):
- `ok=true`
- `stage="done_dry_run"`
- artifacts contains local paths

On success (real upload):
- `ok=true`
- `stage="done"`
- artifacts contains `s3://...` URIs

On failure:
- `ok=false`
- `stage`
- `error_code`
- `error_message`

## Recent Changes
- 2026-01-08: Switched to single job payload input (env/stdin/file); removed tarball output; workspace-local outputs under `out/...` and `.snapshotter_tmp/...`; enforced AES256 SSE for S3 uploads.
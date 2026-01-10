# Snapshotter — Baseline Run (Replit)

This document verifies the Snapshotter execution harness is “known-good” before deeper changes.

## Prereqs

### Canonical job input (required)

Provide the job payload using **one** of the following:

- **Env var**: `SNAPSHOTTER_JOB_JSON` (a JSON string)
- **Stdin**: JSON payload piped to stdin (recommended for LangGraph)
- **File**: `SNAPSHOTTER_JOB_FILE` (path to a JSON file in the workspace)

### Optional developer convenience (non-authoritative)

- `SNAPSHOTTER_DRY_RUN` (recommended `true` for baseline)

AWS credentials (v0.1):
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`
- `AWS_REGION` (optional; boto3 can infer)

## Job payload template (copy/paste)

```json
{
  "repo_url": "https://github.com/stepan-o/fruitful-lab.git",
  "ref": "main",
  "mode": "full",
  "limits": {
    "max_file_bytes": 10485760,
    "max_total_bytes": 262144000,
    "max_files": 20000
  },
  "filters": {
    "deny_dirs": ["node_modules", ".git", ".next", "dist", "build", ".venv"],
    "allow_exts": ["*"],
    "allow_binary": false
  },
  "output": {
    "s3_bucket": "YOUR_BUCKET_NAME",
    "s3_prefix": "repo-scans/snapshotter"
  },
  "metadata": {
    "triggered_by": "manual",
    "notes": "baseline run"
  }
}
```

## Outputs (Local + S3)
Snapshotter generates these artifacts:
- `repo_index.json`
- `artifact_manifest.json`
- `ARCHITECTURE_SUMMARY_SNAPSHOT.json`
- `GAPS_AND_INCONSISTENCIES.json`
- `ONBOARDING.md`

Notes:
- `artifact_manifest.json` includes per-artifact integrity hashes (`sha256`) and stable equivalence hashes (`stable_fingerprints`), plus a `run_fingerprint_sha256` you can compare across reruns.
- Raw `sha256` is byte integrity and will differ for JSON artifacts containing timestamps/job ids; use stable_fingerprints / run_fingerprint_sha256 to compare reruns.

Local output directory (workspace):
- `out/<repo_slug>/<timestamp_utc>/<job_id>/...`

Local clone directory (workspace):
- `.snapshotter_tmp/repo`

S3 upload prefix:
- `<s3_prefix>/<repo_slug>/<timestamp_utc>/<job_id>/...`

## Baseline Run (Dry-run)
Set:
- `SNAPSHOTTER_DRY_RUN=true`

Provide job payload (Replit Secrets):
- `SNAPSHOTTER_JOB_JSON` = (paste the JSON payload)

Run:
```bash
uv run python main.py
```

Expected:
- Process prints a single JSON object with `"ok": true`
- `"stage"` is `"done_dry_run"`
- `"artifacts"` includes local paths to the 5 artifacts above
- No S3 uploads occur in dry-run mode

## Baseline Run (Real upload)
Set:
- `SNAPSHOTTER_DRY_RUN=false`

Provide job payload (Replit Secrets):
- `SNAPSHOTTER_JOB_JSON` = (paste the JSON payload with real bucket/prefix)

Run:
```bash
uv run python main.py
```

Expected:
- Process prints a single JSON object with "ok": true
- "stage" is "done"
- "artifacts" contains s3://... URIs for the 5 artifacts above
- Objects exist in S3 under the computed job prefix

## LangGraph / stdin one-liner (recommended)
Dry-run:
```bash
echo '{"repo_url":"https://github.com/stepan-o/fruitful-lab.git","ref":"main","mode":"full","limits":{"max_file_bytes":10485760,"max_total_bytes":262144000,"max_files":20000},"filters":{"deny_dirs":["node_modules",".git",".next","dist","build",".venv"],"allow_exts":["*"],"allow_binary":false},"output":{"s3_bucket":"YOUR_BUCKET_NAME","s3_prefix":"repo-scans/snapshotter"},"metadata":{"triggered_by":"langgraph","notes":"stdin baseline"}}' \
| SNAPSHOTTER_DRY_RUN=true uv run python main.py
```

Real upload:
```bash
echo '{"repo_url":"https://github.com/stepan-o/fruitful-lab.git","ref":"main","mode":"full","limits":{"max_file_bytes":10485760,"max_total_bytes":262144000,"max_files":20000},"filters":{"deny_dirs":["node_modules",".git",".next","dist","build",".venv"],"allow_exts":["*"],"allow_binary":false},"output":{"s3_bucket":"YOUR_BUCKET_NAME","s3_prefix":"repo-scans/snapshotter"},"metadata":{"triggered_by":"langgraph","notes":"stdin baseline"}}' \
| SNAPSHOTTER_DRY_RUN=false uv run python main.py
```

## Failure contract
On failure, Snapshotter prints:
- `ok=false`
- `stage`
- `error_code`
- `error_message`
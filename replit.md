# Repo Snapshot Uploader

## Overview
Python script that clones a git repository, scans its files, and uploads snapshot data to AWS S3 using server-side encryption (SSE-S3 AES256).

## Project Structure
```
├── main.py                  # Entry point - orchestrates clone, scan, upload
├── pyproject.toml           # uv/Python project config
├── snapshotter/
│   ├── job.py               # Job configuration models (Pydantic)
│   ├── git_ops.py           # Git clone/checkout operations
│   ├── scanner.py           # Repository file scanner
│   ├── s3_uploader.py       # S3 upload utilities
│   └── utils.py             # Helpers (timestamps, hashing, etc.)
```

## Environment Variables (Required)
- `AWS_ACCESS_KEY_ID` - AWS access key
- `AWS_SECRET_ACCESS_KEY` - AWS secret key
- `AWS_REGION` - AWS region (default: us-east-1)
- `SNAPSHOTTER_REPO_URL` - Git repository URL to snapshot
- `SNAPSHOTTER_S3_BUCKET` - Target S3 bucket name

## Environment Variables (Optional)
- `SNAPSHOTTER_REF` - Git ref to checkout (default: main)
- `SNAPSHOTTER_S3_PREFIX` - S3 key prefix (default: repo-scans/snapshotter)
- `SNAPSHOTTER_JOB_ID` - Custom job ID (auto-generated if not set)
- `SNAPSHOTTER_MODE` - Scan mode: full or light (default: full)
- `SNAPSHOTTER_DRY_RUN` - Set to "true" to skip S3 upload

## Running
```bash
uv run python main.py
```

## S3 Output Structure
```
s3://{bucket}/{prefix}/{repo_slug}/{timestamp}/{job_id}/
├── repo_index.json        # File listing with SHA256 hashes
└── artifact_manifest.json # Upload metadata
```

## Recent Changes
- 2025-12-25: Initial setup with uv, S3 upload functionality

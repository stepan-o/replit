# snapshotter/pass2_stub.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from snapshotter.utils import utc_ts


def write_json(path: str | Path, obj: dict[str, Any]) -> None:
    Path(path).write_text(json.dumps(obj, indent=2), encoding="utf-8")


def build_architecture_snapshot_stub(
    *,
    repo_url: str,
    resolved_commit: str,
    job_id: str,
    files_scanned: int,
) -> dict[str, Any]:
    return {
        "generated_at": utc_ts(),
        "repo": {
            "repo_url": repo_url,
            "resolved_commit": resolved_commit or "unknown",
            "job_id": job_id,
        },
        "coverage": {
            "files_scanned": files_scanned,
            "files_read": 0,
            "files_not_read": files_scanned,
        },
        "modules": [],
        "uncertainties": [
            {
                "type": "incomplete_extraction",
                "description": "Pass 2 not implemented yet (stub output).",
                "files_involved": [],
                "suggested_questions": [
                    "Which files should the LLM read first for onboarding (README, entrypoints, core modules)?"
                ],
            }
        ],
        "files_read": [],
        "files_not_read": [],
    }


def build_gaps_stub(*, job_id: str) -> dict[str, Any]:
    return {
        "generated_at": utc_ts(),
        "job_id": job_id,
        "items": [],
    }


def build_onboarding_stub(*, repo_url: str, resolved_commit: str) -> str:
    return f"""# Onboarding (stub)

Repo: {repo_url}
Commit: {resolved_commit}

Pass 2 semantic onboarding has not been generated yet.
"""
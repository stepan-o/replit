# snapshotter/validate_basic.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional

JSON_FILENAMES = {
    "repo_index.json",
    "artifact_manifest.json",
    "ARCHITECTURE_SUMMARY_SNAPSHOT.json",
    "GAPS_AND_INCONSISTENCIES.json",
}


def _must_exist(path: str | Path, label: str) -> None:
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"Missing artifact: {label} at {p}")
    if not p.is_file():
        raise RuntimeError(f"Artifact path is not a file: {label} at {p}")


def _must_parse_json(path: str | Path, label: str) -> None:
    p = Path(path)
    try:
        json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"Invalid JSON for {label} at {p}: {e}") from e


def validate_basic_artifacts(local_paths: Dict[str, Optional[str]]) -> None:
    """
    Minimal sanity validation:
    - required artifacts exist
    - JSON artifacts parse as JSON
    """
    required_keys = [
        "repo_index",
        "artifact_manifest",
        "architecture_snapshot",
        "gaps",
        "onboarding",
    ]

    for key in required_keys:
        p = local_paths.get(key)
        if not p:
            raise RuntimeError(f"Missing local path for required artifact key: {key}")
        _must_exist(p, key)

        name = Path(p).name
        if name in JSON_FILENAMES:
            _must_parse_json(p, key)
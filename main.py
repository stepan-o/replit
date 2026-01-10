# main.py
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from snapshotter.git_ops import clone_and_checkout
from snapshotter.job import Job
from snapshotter.pass1 import build_repo_index, write_json
from snapshotter.s3_uploader import S3Uploader
from snapshotter.utils import sha256_bytes, stable_json_fingerprint_sha256
from snapshotter.validate_basic import validate_basic_artifacts

# -----------------------------
# Stages (canonical)
# -----------------------------
STAGE_INIT = "init"
STAGE_PARSE_JOB = "parse_job"
STAGE_CLONE = "clone"
STAGE_PASS1_REPO_INDEX = "pass1_repo_index"
STAGE_PASS2_STUB = "pass2_stub"
STAGE_PASS1_MANIFEST = "pass1_manifest"
STAGE_VALIDATE_BASIC = "validate_basic"
STAGE_UPLOAD = "upload"
STAGE_DONE = "done"
STAGE_DONE_DRY_RUN = "done_dry_run"


def utc_ts() -> str:
    from snapshotter.utils import utc_ts as _utc_ts

    return _utc_ts()


def parse_bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def file_sha256(path: str | Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def _stable_fingerprint_for_artifact(path: Path) -> str:
    """
    Stable fingerprint that ignores volatile keys for JSON artifacts.
    For non-JSON artifacts, returns the raw bytes sha256.
    """
    raw = path.read_bytes()
    if path.suffix.lower() == ".json":
        try:
            obj = json.loads(raw.decode("utf-8"))
            return stable_json_fingerprint_sha256(obj)
        except Exception:
            # fallback: if JSON parse fails, treat as raw bytes
            return sha256_bytes(raw)
    return sha256_bytes(raw)


def build_artifact_manifest(local_paths: dict[str, Optional[str]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    stable_fingerprints: dict[str, str] = {}

    for name, p in local_paths.items():
        if not p:
            continue
        path = Path(p)
        if not path.exists():
            continue

        raw = path.read_bytes()
        items.append(
            {
                "name": name,
                "filename": path.name,
                "bytes": len(raw),
                "sha256": sha256_bytes(raw),  # exact bytes integrity hash
            }
        )

        stable_fingerprints[name] = _stable_fingerprint_for_artifact(path)

    items.sort(key=lambda x: x["name"])  # determinism

    # Stable “equivalence” fingerprint for the whole run (all artifacts),
    # independent of timestamps/job_ids inside the JSON artifacts.
    canonical = json.dumps(
        stable_fingerprints,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    run_fingerprint_sha256 = sha256_bytes(canonical)

    return {
        "generated_at": utc_ts(),
        "items": items,
        "stable_fingerprints": stable_fingerprints,
        "run_fingerprint_sha256": run_fingerprint_sha256,
    }


def build_architecture_summary_snapshot_stub(
    *, repo_url: str, resolved_commit: str, job_id: str, repo_index: dict[str, Any]
) -> dict[str, Any]:
    files_scanned = int(repo_index.get("counts", {}).get("files_scanned", 0))
    files_included = int(repo_index.get("counts", {}).get("files_included", 0))
    included_paths = [f.get("path") for f in repo_index.get("files", []) if f.get("path")]
    included_paths.sort()

    return {
        "generated_at": utc_ts(),
        "repo": {"repo_url": repo_url, "resolved_commit": resolved_commit or "unknown", "job_id": job_id},
        "coverage": {"files_scanned": files_scanned, "files_read": 0, "files_not_read": files_included},
        "modules": [],
        "uncertainties": [
            {
                "type": "incomplete_extraction",
                "description": "Pass 2 semantic analysis not implemented yet (stub output).",
                "files_involved": [],
                "suggested_questions": [
                    "Which files should be prioritized for onboarding (README, entrypoints, core modules)?"
                ],
            }
        ],
        "files_read": [],
        "files_not_read": [{"path": p, "reason": "pass2_not_run"} for p in included_paths],
    }


def build_gaps_and_inconsistencies_stub(*, job_id: str) -> dict[str, Any]:
    return {"generated_at": utc_ts(), "job_id": job_id, "items": []}


def build_onboarding_stub(*, repo_url: str, resolved_commit: str) -> str:
    return (
        "# Onboarding (stub)\n\n"
        f"Repo: {repo_url}\n"
        f"Commit: {resolved_commit}\n\n"
        "Pass 2 semantic onboarding has not been generated yet.\n"
    )


def _print_success(out: dict[str, Any]) -> None:
    print(json.dumps(out, indent=2))


def _print_failure(stage: str, err: Exception) -> None:
    out = {
        "ok": False,
        "stage": stage,
        "error_code": f"SNAPSHOTTER_FAILED_{stage.upper()}",
        "error_message": str(err),
    }
    print(json.dumps(out, indent=2))


def _read_job_payload_json_required() -> tuple[dict[str, Any], str]:
    """
    Canonical job input (MANDATORY):
      1) SNAPSHOTTER_JOB_JSON env (JSON string)
      2) SNAPSHOTTER_JOB_FILE env (path to JSON file)
      3) stdin (if piped)
    """
    raw = os.environ.get("SNAPSHOTTER_JOB_JSON")
    if raw and raw.strip():
        return json.loads(raw), "env:SNAPSHOTTER_JOB_JSON"

    job_file = os.environ.get("SNAPSHOTTER_JOB_FILE")
    if job_file and job_file.strip():
        p = Path(job_file)
        return json.loads(p.read_text(encoding="utf-8")), f"file:{p}"

    if not sys.stdin.isatty():
        data = sys.stdin.read()
        if data and data.strip():
            return json.loads(data), "stdin"

    raise RuntimeError(
        "Missing required job payload. Provide one of: "
        "SNAPSHOTTER_JOB_JSON (env), SNAPSHOTTER_JOB_FILE (env path), or pipe JSON to stdin."
    )


def _job_from_payload(payload: dict[str, Any]) -> Job:
    # pydantic v2
    return Job.model_validate(payload).finalize()


def main() -> None:
    load_dotenv()

    stage = STAGE_INIT

    try:
        dry_run = parse_bool(os.environ.get("SNAPSHOTTER_DRY_RUN"), default=False)
        aws_region = os.environ.get("AWS_REGION")  # optional

        # --- parse job (payload authoritative + required) ---
        stage = STAGE_PARSE_JOB
        payload, payload_src = _read_job_payload_json_required()

        # --- LangGraph path (opt-in) ---
        use_langgraph = parse_bool(os.environ.get("SNAPSHOTTER_USE_LANGGRAPH"), default=False)
        if use_langgraph:
            # Import only when needed so linear users don't pay import cost / dependency issues.
            from snapshotter.graph import SnapshotterStageError, run_snapshotter_graph

            try:
                out = run_snapshotter_graph(
                    payload=payload,
                    payload_src=payload_src,
                    dry_run=dry_run,
                    aws_region=aws_region,
                )
                _print_success(out)
                return
            except SnapshotterStageError as se:
                _print_failure(se.stage, se)
                raise SystemExit(1) from se

        # --- Linear path (baseline) ---
        job = _job_from_payload(payload)

        # --- dirs ---
        workdir = ".snapshotter_tmp"
        repo_dir = os.path.join(workdir, "repo")
        out_dir = os.path.join("out", job.repo_slug or "repo", job.timestamp_utc or "ts", job.job_id or "job")
        Path(workdir).mkdir(parents=True, exist_ok=True)
        Path(out_dir).mkdir(parents=True, exist_ok=True)

        # local artifact paths
        local_repo_index = os.path.join(out_dir, "repo_index.json")
        local_manifest = os.path.join(out_dir, "artifact_manifest.json")
        local_arch = os.path.join(out_dir, "ARCHITECTURE_SUMMARY_SNAPSHOT.json")
        local_gaps = os.path.join(out_dir, "GAPS_AND_INCONSISTENCIES.json")
        local_onboarding = os.path.join(out_dir, "ONBOARDING.md")

        # --- clone ---
        stage = STAGE_CLONE
        resolved_commit = clone_and_checkout(job.repo_url, job.ref, workdir)

        # --- pass1: repo_index ---
        stage = STAGE_PASS1_REPO_INDEX
        repo_index = build_repo_index(repo_dir, job)
        repo_index["job"]["resolved_commit"] = resolved_commit
        write_json(local_repo_index, repo_index)

        # --- pass2: stubs ---
        stage = STAGE_PASS2_STUB
        arch_stub = build_architecture_summary_snapshot_stub(
            repo_url=job.repo_url,
            resolved_commit=resolved_commit,
            job_id=job.job_id or "unknown",
            repo_index=repo_index,
        )
        write_json(local_arch, arch_stub)

        gaps_stub = build_gaps_and_inconsistencies_stub(job_id=job.job_id or "unknown")
        write_json(local_gaps, gaps_stub)

        Path(local_onboarding).write_text(
            build_onboarding_stub(repo_url=job.repo_url, resolved_commit=resolved_commit), encoding="utf-8"
        )

        # --- manifest ---
        stage = STAGE_PASS1_MANIFEST
        manifest = build_artifact_manifest(
            {
                "repo_index": local_repo_index,
                "architecture_snapshot": local_arch,
                "gaps": local_gaps,
                "onboarding": local_onboarding,
            }
        )
        write_json(local_manifest, manifest)

        # --- validate ---
        stage = STAGE_VALIDATE_BASIC
        validate_basic_artifacts(
            {
                "repo_index": local_repo_index,
                "artifact_manifest": local_manifest,
                "architecture_snapshot": local_arch,
                "gaps": local_gaps,
                "onboarding": local_onboarding,
            }
        )

        repo_index_sha = file_sha256(local_repo_index)

        uploader = S3Uploader(bucket=job.output.s3_bucket, prefix=job.s3_job_prefix(), region=aws_region)

        if dry_run:
            stage = STAGE_DONE_DRY_RUN
            _print_success(
                {
                    "ok": True,
                    "stage": stage,
                    "job_id": job.job_id,
                    "repo_url": job.repo_url,
                    "requested_ref": job.ref,
                    "resolved_commit": resolved_commit,
                    "s3_bucket": job.output.s3_bucket,
                    "s3_prefix": job.s3_job_prefix(),
                    "job_payload_source": payload_src,
                    "artifacts": {
                        "repo_index_local": local_repo_index,
                        "artifact_manifest_local": local_manifest,
                        "architecture_snapshot_local": local_arch,
                        "gaps_local": local_gaps,
                        "onboarding_local": local_onboarding,
                    },
                    "hashes": {"repo_index_sha256": repo_index_sha},
                }
            )
            return

        stage = STAGE_UPLOAD
        s3_paths: dict[str, Optional[str]] = {
            "repo_index": uploader.upload_file("repo_index.json", local_repo_index, content_type="application/json"),
            "artifact_manifest": uploader.upload_file(
                "artifact_manifest.json", local_manifest, content_type="application/json"
            ),
            "architecture_snapshot": uploader.upload_file(
                "ARCHITECTURE_SUMMARY_SNAPSHOT.json", local_arch, content_type="application/json"
            ),
            "gaps": uploader.upload_file("GAPS_AND_INCONSISTENCIES.json", local_gaps, content_type="application/json"),
            "onboarding": uploader.upload_file("ONBOARDING.md", local_onboarding, content_type="text/markdown"),
        }

        stage = STAGE_DONE
        _print_success(
            {
                "ok": True,
                "stage": stage,
                "job_id": job.job_id,
                "repo_url": job.repo_url,
                "requested_ref": job.ref,
                "resolved_commit": resolved_commit,
                "s3_bucket": job.output.s3_bucket,
                "s3_prefix": job.s3_job_prefix(),
                "job_payload_source": payload_src,
                "artifacts": {
                    "repo_index": s3_paths["repo_index"],
                    "artifact_manifest": s3_paths["artifact_manifest"],
                    "architecture_snapshot": s3_paths["architecture_snapshot"],
                    "gaps": s3_paths["gaps"],
                    "onboarding": s3_paths["onboarding"],
                },
                "hashes": {"repo_index_sha256": repo_index_sha},
            }
        )

    except Exception as e:
        _print_failure(stage, e)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
# snapshotter/graph.py
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, TypedDict

from snapshotter.git_ops import clone_and_checkout
from snapshotter.job import Job
from snapshotter.pass1 import build_repo_index, write_json
from snapshotter.s3_uploader import S3Uploader
from snapshotter.utils import sha256_bytes, stable_json_fingerprint_sha256, utc_ts
from snapshotter.validate_basic import validate_basic_artifacts

try:
    from langgraph.graph import END, StateGraph
except Exception as e:  # pragma: no cover
    raise RuntimeError(
        "LangGraph is required for SNAPSHOTTER_USE_LANGGRAPH=true. Install 'langgraph'."
    ) from e


# -----------------------------
# Stages (canonical)
# -----------------------------
STAGE_INIT = "init"
STAGE_PARSE_JOB = "parse_job"
STAGE_CLONE = "clone"
STAGE_PASS1_REPO_INDEX = "pass1_repo_index"
STAGE_PASS2_MAKE_READ_PLAN = "pass2_make_read_plan"
STAGE_PASS2_FETCH_FILES = "pass2_fetch_files"
STAGE_PASS2_GENERATE_OUTPUTS = "pass2_generate_outputs"
STAGE_PASS1_MANIFEST = "pass1_manifest"
STAGE_VALIDATE_BASIC = "validate_basic"
STAGE_UPLOAD = "upload"
STAGE_EMIT_RESULT = "emit_result"
STAGE_DONE = "done"
STAGE_DONE_DRY_RUN = "done_dry_run"


class SnapshotterStageError(RuntimeError):
    def __init__(self, stage: str, inner: Exception):
        super().__init__(str(inner))
        self.stage = stage
        self.inner = inner


@dataclass(frozen=True)
class RuntimeConfig:
    dry_run: bool
    aws_region: str | None


class SnapshotterState(TypedDict, total=False):
    payload: dict[str, Any]
    payload_src: str
    config: RuntimeConfig
    stage: str

    job: Job
    resolved_commit: str

    workdir: str
    repo_dir: str
    out_dir: str

    local_paths: dict[str, str]

    repo_index: dict[str, Any]
    read_plan: list[str]
    file_contents_map: dict[str, str]

    s3_paths: dict[str, Optional[str]]
    result: dict[str, Any]


def _file_sha256(path: str | Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def _stable_fingerprint_for_artifact(path: Path) -> str:
    raw = path.read_bytes()
    if path.suffix.lower() == ".json":
        try:
            obj = json.loads(raw.decode("utf-8"))
            return stable_json_fingerprint_sha256(obj)
        except Exception:
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
                "sha256": sha256_bytes(raw),
            }
        )
        stable_fingerprints[name] = _stable_fingerprint_for_artifact(path)

    items.sort(key=lambda x: x["name"])

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


def _build_architecture_summary_snapshot_stub(
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


def _build_gaps_and_inconsistencies_stub(*, job_id: str) -> dict[str, Any]:
    return {"generated_at": utc_ts(), "job_id": job_id, "items": []}


def _build_onboarding_stub(*, repo_url: str, resolved_commit: str) -> str:
    return (
        "# Onboarding (stub)\n\n"
        f"Repo: {repo_url}\n"
        f"Commit: {resolved_commit}\n\n"
        "Pass 2 semantic onboarding has not been generated yet.\n"
    )


def node_load_job(state: SnapshotterState) -> SnapshotterState:
    stage = STAGE_PARSE_JOB
    try:
        job = Job.model_validate(state["payload"]).finalize()

        workdir = ".snapshotter_tmp"
        repo_dir = f"{workdir}/repo"
        out_dir = f"out/{job.repo_slug or 'repo'}/{job.timestamp_utc or 'ts'}/{job.job_id or 'job'}"
        Path(workdir).mkdir(parents=True, exist_ok=True)
        Path(out_dir).mkdir(parents=True, exist_ok=True)

        local_paths = {
            "repo_index": str(Path(out_dir) / "repo_index.json"),
            "artifact_manifest": str(Path(out_dir) / "artifact_manifest.json"),
            "architecture_snapshot": str(Path(out_dir) / "ARCHITECTURE_SUMMARY_SNAPSHOT.json"),
            "gaps": str(Path(out_dir) / "GAPS_AND_INCONSISTENCIES.json"),
            "onboarding": str(Path(out_dir) / "ONBOARDING.md"),
        }

        state["stage"] = stage
        state["job"] = job
        state["workdir"] = workdir
        state["repo_dir"] = repo_dir
        state["out_dir"] = out_dir
        state["local_paths"] = local_paths
        return state
    except Exception as e:
        raise SnapshotterStageError(stage, e) from e


def node_clone_repo(state: SnapshotterState) -> SnapshotterState:
    stage = STAGE_CLONE
    try:
        job = state["job"]
        resolved_commit = clone_and_checkout(job.repo_url, job.ref, state["workdir"])
        state["stage"] = stage
        state["resolved_commit"] = resolved_commit
        return state
    except Exception as e:
        raise SnapshotterStageError(stage, e) from e


def node_pass1_build_index(state: SnapshotterState) -> SnapshotterState:
    stage = STAGE_PASS1_REPO_INDEX
    try:
        job = state["job"]
        repo_index = build_repo_index(state["repo_dir"], job)
        repo_index["job"]["resolved_commit"] = state.get("resolved_commit", "unknown")
        write_json(state["local_paths"]["repo_index"], repo_index)

        state["stage"] = stage
        state["repo_index"] = repo_index
        return state
    except Exception as e:
        raise SnapshotterStageError(stage, e) from e


def node_pass2_make_read_plan(state: SnapshotterState) -> SnapshotterState:
    stage = STAGE_PASS2_MAKE_READ_PLAN
    try:
        candidates = state["repo_index"].get("read_plan_suggestions", {}).get("candidates", [])
        paths = [c.get("path") for c in candidates if c.get("path")]
        state["stage"] = stage
        state["read_plan"] = paths
        return state
    except Exception as e:
        raise SnapshotterStageError(stage, e) from e


def node_pass2_fetch_files(state: SnapshotterState) -> SnapshotterState:
    stage = STAGE_PASS2_FETCH_FILES
    try:
        state["stage"] = stage
        state["file_contents_map"] = {}
        return state
    except Exception as e:
        raise SnapshotterStageError(stage, e) from e


def node_pass2_generate_outputs(state: SnapshotterState) -> SnapshotterState:
    stage = STAGE_PASS2_GENERATE_OUTPUTS
    try:
        job = state["job"]
        lp = state["local_paths"]
        repo_index = state["repo_index"]
        resolved_commit = state.get("resolved_commit", "unknown")

        write_json(
            lp["architecture_snapshot"],
            _build_architecture_summary_snapshot_stub(
                repo_url=job.repo_url,
                resolved_commit=resolved_commit,
                job_id=job.job_id or "unknown",
                repo_index=repo_index,
            ),
        )
        write_json(lp["gaps"], _build_gaps_and_inconsistencies_stub(job_id=job.job_id or "unknown"))
        Path(lp["onboarding"]).write_text(
            _build_onboarding_stub(repo_url=job.repo_url, resolved_commit=resolved_commit),
            encoding="utf-8",
        )

        state["stage"] = stage
        return state
    except Exception as e:
        raise SnapshotterStageError(stage, e) from e


def node_pass1_manifest(state: SnapshotterState) -> SnapshotterState:
    stage = STAGE_PASS1_MANIFEST
    try:
        lp = state["local_paths"]
        manifest = build_artifact_manifest(
            {
                "repo_index": lp["repo_index"],
                "architecture_snapshot": lp["architecture_snapshot"],
                "gaps": lp["gaps"],
                "onboarding": lp["onboarding"],
            }
        )
        write_json(lp["artifact_manifest"], manifest)
        state["stage"] = stage
        return state
    except Exception as e:
        raise SnapshotterStageError(stage, e) from e


def node_validate_basic(state: SnapshotterState) -> SnapshotterState:
    stage = STAGE_VALIDATE_BASIC
    try:
        lp = state["local_paths"]
        validate_basic_artifacts(
            {
                "repo_index": lp["repo_index"],
                "artifact_manifest": lp["artifact_manifest"],
                "architecture_snapshot": lp["architecture_snapshot"],
                "gaps": lp["gaps"],
                "onboarding": lp["onboarding"],
            }
        )
        state["stage"] = stage
        return state
    except Exception as e:
        raise SnapshotterStageError(stage, e) from e


def node_upload_artifacts(state: SnapshotterState) -> SnapshotterState:
    stage = STAGE_UPLOAD
    try:
        job = state["job"]
        cfg = state["config"]
        lp = state["local_paths"]

        uploader = S3Uploader(bucket=job.output.s3_bucket, prefix=job.s3_job_prefix(), region=cfg.aws_region)

        if cfg.dry_run:
            state["stage"] = stage
            state["s3_paths"] = {}
            return state

        s3_paths: dict[str, Optional[str]] = {
            "repo_index": uploader.upload_file("repo_index.json", lp["repo_index"], content_type="application/json"),
            "artifact_manifest": uploader.upload_file(
                "artifact_manifest.json", lp["artifact_manifest"], content_type="application/json"
            ),
            "architecture_snapshot": uploader.upload_file(
                "ARCHITECTURE_SUMMARY_SNAPSHOT.json", lp["architecture_snapshot"], content_type="application/json"
            ),
            "gaps": uploader.upload_file("GAPS_AND_INCONSISTENCIES.json", lp["gaps"], content_type="application/json"),
            "onboarding": uploader.upload_file("ONBOARDING.md", lp["onboarding"], content_type="text/markdown"),
        }

        state["stage"] = stage
        state["s3_paths"] = s3_paths
        return state
    except Exception as e:
        raise SnapshotterStageError(stage, e) from e


def node_emit_result(state: SnapshotterState) -> SnapshotterState:
    stage = STAGE_EMIT_RESULT
    try:
        job = state["job"]
        cfg = state["config"]
        resolved_commit = state.get("resolved_commit", "unknown")
        lp = state["local_paths"]

        repo_index_sha = _file_sha256(lp["repo_index"])

        if cfg.dry_run:
            state["result"] = {
                "ok": True,
                "stage": STAGE_DONE_DRY_RUN,
                "job_id": job.job_id,
                "repo_url": job.repo_url,
                "requested_ref": job.ref,
                "resolved_commit": resolved_commit,
                "s3_bucket": job.output.s3_bucket,
                "s3_prefix": job.s3_job_prefix(),
                "job_payload_source": state.get("payload_src", "unknown"),
                "artifacts": {
                    "repo_index_local": lp["repo_index"],
                    "artifact_manifest_local": lp["artifact_manifest"],
                    "architecture_snapshot_local": lp["architecture_snapshot"],
                    "gaps_local": lp["gaps"],
                    "onboarding_local": lp["onboarding"],
                },
                "hashes": {"repo_index_sha256": repo_index_sha},
            }
            state["stage"] = stage
            return state

        s3p = state.get("s3_paths", {})
        state["result"] = {
            "ok": True,
            "stage": STAGE_DONE,
            "job_id": job.job_id,
            "repo_url": job.repo_url,
            "requested_ref": job.ref,
            "resolved_commit": resolved_commit,
            "s3_bucket": job.output.s3_bucket,
            "s3_prefix": job.s3_job_prefix(),
            "job_payload_source": state.get("payload_src", "unknown"),
            "artifacts": {
                "repo_index": s3p.get("repo_index"),
                "artifact_manifest": s3p.get("artifact_manifest"),
                "architecture_snapshot": s3p.get("architecture_snapshot"),
                "gaps": s3p.get("gaps"),
                "onboarding": s3p.get("onboarding"),
            },
            "hashes": {"repo_index_sha256": repo_index_sha},
        }
        state["stage"] = stage
        return state
    except Exception as e:
        raise SnapshotterStageError(stage, e) from e


def build_snapshotter_graph():
    g = StateGraph(SnapshotterState)

    g.add_node("load_job", node_load_job)
    g.add_node("clone_repo", node_clone_repo)
    g.add_node("pass1_build_index", node_pass1_build_index)
    g.add_node("pass2_make_read_plan", node_pass2_make_read_plan)
    g.add_node("pass2_fetch_files", node_pass2_fetch_files)
    g.add_node("pass2_generate_outputs", node_pass2_generate_outputs)
    g.add_node("pass1_manifest", node_pass1_manifest)
    g.add_node("validate_basic", node_validate_basic)
    g.add_node("upload_artifacts", node_upload_artifacts)
    g.add_node("emit_result", node_emit_result)

    g.set_entry_point("load_job")
    g.add_edge("load_job", "clone_repo")
    g.add_edge("clone_repo", "pass1_build_index")
    g.add_edge("pass1_build_index", "pass2_make_read_plan")
    g.add_edge("pass2_make_read_plan", "pass2_fetch_files")
    g.add_edge("pass2_fetch_files", "pass2_generate_outputs")
    g.add_edge("pass2_generate_outputs", "pass1_manifest")
    g.add_edge("pass1_manifest", "validate_basic")
    g.add_edge("validate_basic", "upload_artifacts")
    g.add_edge("upload_artifacts", "emit_result")
    g.add_edge("emit_result", END)

    return g.compile()


def run_snapshotter_graph(
    *,
    payload: dict[str, Any],
    payload_src: str,
    dry_run: bool,
    aws_region: str | None,
) -> dict[str, Any]:
    app = build_snapshotter_graph()
    state: SnapshotterState = {
        "payload": payload,
        "payload_src": payload_src,
        "config": RuntimeConfig(dry_run=dry_run, aws_region=aws_region),
        "stage": STAGE_INIT,
    }
    final_state = app.invoke(state)
    return final_state["result"]
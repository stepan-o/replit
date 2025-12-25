import json
import os
import tarfile
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from snapshotter.git_ops import clone_and_checkout
from snapshotter.job import Job, Output
from snapshotter.pass1 import build_repo_index, write_json
from snapshotter.s3_uploader import S3Uploader
from snapshotter.utils import sha256_bytes


def parse_bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "y", "on"}


def file_sha256(path: str | Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def build_tarball_from_index(repo_dir: str, repo_index: dict[str, Any], out_path: str | Path) -> None:
    """
    Build repo_snapshot.tar.gz from ONLY the included files in repo_index.
    This keeps tarball aligned with Pass 1 filters (safety + boundedness).
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    files = repo_index.get("files", [])
    with tarfile.open(out_path, "w:gz") as tf:
        for f in files:
            rel_path = f["path"]
            abs_path = Path(repo_dir) / rel_path
            if abs_path.exists() and abs_path.is_file():
                tf.add(str(abs_path), arcname=rel_path)


def build_artifact_manifest(local_paths: dict[str, str | None]) -> dict[str, Any]:
    items = []
    for name, p in local_paths.items():
        if not p:
            continue
        path = Path(p)
        if not path.exists():
            continue
        b = path.read_bytes()
        items.append(
            {
                "name": name,
                "filename": path.name,
                "bytes": len(b),
                "sha256": sha256_bytes(b),
            }
        )
    # determinism
    items.sort(key=lambda x: x["name"])
    return {
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime(
            "%Y-%m-%dT%H-%M-%SZ"
        ),
        "items": items,
    }


def main():
    load_dotenv()  # harmless in Replit; useful locally

    # --- env ---
    repo_url = os.environ["SNAPSHOTTER_REPO_URL"]
    ref = os.environ.get("SNAPSHOTTER_REF", "main")

    bucket = os.environ["SNAPSHOTTER_S3_BUCKET"]
    prefix = os.environ.get("SNAPSHOTTER_S3_PREFIX", "repo-scans/snapshotter")

    dry_run = parse_bool(os.environ.get("SNAPSHOTTER_DRY_RUN"), default=False)
    enable_tarball = parse_bool(os.environ.get("SNAPSHOTTER_TARBALL"), default=True)
    aws_region = os.environ.get("AWS_REGION")  # optional

    # --- job ---
    job = Job(
        job_id=os.environ.get("SNAPSHOTTER_JOB_ID"),
        repo_url=repo_url,
        ref=ref,
        mode=os.environ.get("SNAPSHOTTER_MODE", "full"),
        output=Output(s3_bucket=bucket, s3_prefix=prefix),
    ).finalize()

    # --- dirs ---
    workdir = "/tmp/snapshotter"
    repo_dir = os.path.join(workdir, "repo")
    out_dir = os.path.join(workdir, "out", job.repo_slug or "repo", job.timestamp_utc or "ts", job.job_id or "job")
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    stage = "init"

    # local artifact paths
    local_repo_index = os.path.join(out_dir, "repo_index.json")
    local_tarball = os.path.join(out_dir, "repo_snapshot.tar.gz")
    local_manifest = os.path.join(out_dir, "artifact_manifest.json")

    # result placeholders
    s3_paths: dict[str, str | None] = {
        "repo_index": None,
        "tarball": None,
        "artifact_manifest": None,
        "architecture_snapshot": None,
        "gaps": None,
        "onboarding": None,
    }

    try:
        # --- clone ---
        stage = "clone"
        resolved_commit = clone_and_checkout(job.repo_url, job.ref, workdir)

        # --- pass1: repo_index ---
        stage = "pass1_repo_index"
        repo_index = build_repo_index(repo_dir, job)
        repo_index["job"]["resolved_commit"] = resolved_commit
        write_json(local_repo_index, repo_index)

        # --- tarball (optional) ---
        stage = "pass1_tarball"
        if enable_tarball:
            build_tarball_from_index(repo_dir, repo_index, local_tarball)
        else:
            local_tarball = None

        # --- artifact_manifest ---
        stage = "pass1_manifest"
        manifest = build_artifact_manifest(
            {
                "repo_index": local_repo_index,
                "tarball": local_tarball,
            }
        )
        write_json(local_manifest, manifest)

        # --- hashes ---
        repo_index_sha = file_sha256(local_repo_index)
        tarball_sha = file_sha256(local_tarball) if local_tarball else None

        # --- upload ---
        stage = "upload"
        uploader = S3Uploader(bucket=bucket, prefix=job.s3_job_prefix(), region=aws_region)

        if dry_run:
            # Keep s3 paths empty; still return local paths so you can inspect outputs.
            out = {
                "ok": True,
                "stage": "pass1_complete_dry_run",
                "job_id": job.job_id,
                "repo_url": job.repo_url,
                "requested_ref": job.ref,
                "resolved_commit": resolved_commit,
                "s3_bucket": bucket,
                "s3_prefix": job.s3_job_prefix(),
                "artifacts": {
                    "repo_index_local": local_repo_index,
                    "artifact_manifest_local": local_manifest,
                    "tarball_local": local_tarball,
                },
                "hashes": {
                    "repo_index_sha256": repo_index_sha,
                    "tarball_sha256": tarball_sha,
                },
                "next": [
                    "Pass 2: LLM semantic outputs (ARCHITECTURE_SUMMARY_SNAPSHOT.json, GAPS_AND_INCONSISTENCIES.json, ONBOARDING.md)",
                    "Upload Pass 2 artifacts under same job prefix",
                ],
            }
            print(json.dumps(out, indent=2))
            return

        # real uploads (SSE=AES256 enforced in uploader)
        s3_paths["repo_index"] = uploader.upload_file("repo_index.json", local_repo_index, content_type="application/json")
        s3_paths["artifact_manifest"] = uploader.upload_file(
            "artifact_manifest.json", local_manifest, content_type="application/json"
        )
        if local_tarball:
            s3_paths["tarball"] = uploader.upload_file("repo_snapshot.tar.gz", local_tarball, content_type="application/gzip")

        # --- final contract output ---
        out = {
            "ok": True,
            "job_id": job.job_id,
            "repo_url": job.repo_url,
            "requested_ref": job.ref,
            "resolved_commit": resolved_commit,
            "s3_bucket": bucket,
            "s3_prefix": job.s3_job_prefix(),
            "artifacts": {
                "repo_index": s3_paths["repo_index"],
                "artifact_manifest": s3_paths["artifact_manifest"],
                "tarball": s3_paths["tarball"],
                "architecture_snapshot": s3_paths["architecture_snapshot"],
                "gaps": s3_paths["gaps"],
                "onboarding": s3_paths["onboarding"],
            },
            "hashes": {
                "repo_index_sha256": repo_index_sha,
                "tarball_sha256": tarball_sha,
            },
        }
        print(json.dumps(out, indent=2))

    except Exception as e:
        out = {
            "ok": False,
            "stage": stage,
            "error_code": f"SNAPSHOTTER_FAILED_{stage.upper()}",
            "error_message": str(e),
        }
        print(json.dumps(out, indent=2))
        raise


if __name__ == "__main__":
    main()
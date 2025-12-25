import json
import os
from dotenv import load_dotenv

from snapshotter.job import Job, Output
from snapshotter.git_ops import clone_and_checkout
from snapshotter.scanner import scan_repo
from snapshotter.s3_uploader import get_s3_client, upload_json


def main():
    load_dotenv()

    repo_url = os.environ["SNAPSHOTTER_REPO_URL"]
    ref = os.environ.get("SNAPSHOTTER_REF", "main")
    bucket = os.environ["SNAPSHOTTER_S3_BUCKET"]
    prefix = os.environ.get("SNAPSHOTTER_S3_PREFIX", "repo-scans/snapshotter")
    dry_run = os.environ.get("SNAPSHOTTER_DRY_RUN", "false").lower() == "true"

    job = Job(
        job_id=os.environ.get("SNAPSHOTTER_JOB_ID"),
        repo_url=repo_url,
        ref=ref,
        mode=os.environ.get("SNAPSHOTTER_MODE", "full"),
        output=Output(s3_bucket=bucket, s3_prefix=prefix),
    ).finalize()

    workdir = "/tmp/snapshotter"
    repo_dir = os.path.join(workdir, "repo")
    stage = "clone"

    try:
        print(f"Cloning {job.repo_url} @ {job.ref}...")
        resolved_commit = clone_and_checkout(job.repo_url, job.ref, workdir)
        print(f"Resolved commit: {resolved_commit}")

        stage = "scan"
        print("Scanning repository...")
        files = list(scan_repo(repo_dir, job))
        print(f"Found {len(files)} files")

        repo_index = {
            "job_id": job.job_id,
            "repo_url": job.repo_url,
            "requested_ref": job.ref,
            "resolved_commit": resolved_commit,
            "timestamp_utc": job.timestamp_utc,
            "mode": job.mode,
            "file_count": len(files),
            "files": files,
        }

        artifact_manifest = {
            "job_id": job.job_id,
            "repo_slug": job.repo_slug,
            "timestamp_utc": job.timestamp_utc,
            "s3_bucket": job.output.s3_bucket,
            "s3_job_prefix": job.s3_job_prefix(),
            "artifacts": [],
        }

        if dry_run:
            out = {
                "ok": True,
                "stage": "dry_run",
                "job_id": job.job_id,
                "repo_url": job.repo_url,
                "requested_ref": job.ref,
                "resolved_commit": resolved_commit,
                "s3_bucket": job.output.s3_bucket,
                "s3_job_prefix": job.s3_job_prefix(),
                "file_count": len(files),
                "message": "Dry run complete. Set SNAPSHOTTER_DRY_RUN=false to upload.",
            }
            print(json.dumps(out, indent=2))
            return

        stage = "upload"
        print("Uploading to S3...")
        s3 = get_s3_client()

        index_key = f"{job.s3_job_prefix()}/repo_index.json"
        index_result = upload_json(s3, repo_index, bucket, index_key)
        artifact_manifest["artifacts"].append({
            "name": "repo_index.json",
            **index_result,
        })
        print(f"Uploaded: {index_key}")

        manifest_key = f"{job.s3_job_prefix()}/artifact_manifest.json"
        manifest_result = upload_json(s3, artifact_manifest, bucket, manifest_key)
        print(f"Uploaded: {manifest_key}")

        out = {
            "ok": True,
            "stage": "complete",
            "job_id": job.job_id,
            "repo_url": job.repo_url,
            "requested_ref": job.ref,
            "resolved_commit": resolved_commit,
            "s3_bucket": bucket,
            "s3_job_prefix": job.s3_job_prefix(),
            "file_count": len(files),
            "artifacts_uploaded": len(artifact_manifest["artifacts"]) + 1,
        }
        print(json.dumps(out, indent=2))

    except Exception as e:
        out = {
            "ok": False,
            "stage": stage,
            "error_code": "SNAPSHOTTER_FAILED",
            "error_message": str(e),
        }
        print(json.dumps(out, indent=2))
        raise


if __name__ == "__main__":
    main()

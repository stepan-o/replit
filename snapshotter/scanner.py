import os
import re
from pathlib import Path
from typing import Iterator

from snapshotter.job import Filters, Job
from snapshotter.utils import sha256_bytes


def should_skip_dir(dirname: str, filters: Filters) -> bool:
    return dirname in filters.deny_dirs


def should_skip_file(filepath: str, filters: Filters) -> bool:
    fp = filepath.replace("\\", "/")
    for pattern in filters.deny_file_regex:
        if re.search(pattern, fp):
            return True
    return False


def extension_allowed(filepath: str, filters: Filters) -> bool:
    if "*" in filters.allow_exts:
        return True
    ext = Path(filepath).suffix.lstrip(".")
    return ext in filters.allow_exts


def scan_repo(repo_dir: str, job: Job) -> Iterator[dict]:
    filters = job.filters
    limits = job.limits
    total_bytes = 0
    file_count = 0

    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = sorted([d for d in dirs if not should_skip_dir(d, filters)])
        files = sorted(files)

        for filename in files:
            if file_count >= limits.max_files:
                return

            filepath = os.path.join(root, filename)
            rel_path = os.path.relpath(filepath, repo_dir).replace("\\", "/")

            if should_skip_file(rel_path, filters):
                continue
            if not extension_allowed(rel_path, filters):
                continue

            try:
                stat = os.stat(filepath)
                size = stat.st_size
            except OSError:
                continue

            if size > limits.max_file_bytes:
                continue
            if total_bytes + size > limits.max_total_bytes:
                continue

            try:
                with open(filepath, "rb") as f:
                    content = f.read()
                sha = sha256_bytes(content)
            except Exception:
                sha = None
                content = None

            total_bytes += size
            file_count += 1

            yield {
                "path": rel_path,
                "size_bytes": size,
                "sha256": sha,
            }
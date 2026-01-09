# snapshotter/pass1.py
from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from typing import Any

from snapshotter.job import Job

# deterministic read-plan suggestions
from snapshotter.read_plan import suggest_files_to_read
from snapshotter.utils import is_probably_binary, sha256_bytes, utc_ts

LANG_BY_EXT = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".json": "json",
    ".md": "markdown",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
}

IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([a-zA-Z0-9_\.]+)\s+import|import\s+([a-zA-Z0-9_\.]+))",
    re.M,
)


def infer_language(path: str) -> str:
    ext = Path(path).suffix.lower()
    return LANG_BY_EXT.get(ext, "unknown")


def parse_python_defs_and_imports(text: str) -> tuple[list[str], list[str]]:
    defs: list[str] = []
    imports: list[str] = []
    try:
        tree = ast.parse(text)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                defs.append(node.name)
            elif isinstance(node, ast.Import):
                for n in node.names:
                    imports.append(n.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
    except Exception:
        pass
    return defs, sorted(set(imports))


def parse_best_effort_imports(text: str) -> list[str]:
    found = set()
    for m in IMPORT_RE.finditer(text):
        mod = m.group(1) or m.group(2)
        if mod:
            found.add(mod)
    return sorted(found)


def build_repo_index(repo_dir: str, job: Job) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    total_bytes = 0
    files_scanned = 0
    files_included = 0

    deny_dirs = set(job.filters.deny_dirs)
    deny_file_regex = [re.compile(p) for p in job.filters.deny_file_regex]
    allow_all = "*" in job.filters.allow_exts
    allow_exts = set([e.lower().lstrip(".") for e in job.filters.allow_exts if e != "*"])

    # v0.1: binary is skipped unless explicitly allowed
    allow_binary = bool(getattr(job.filters, "allow_binary", False))

    max_files_reached = False

    def record_skip(rel_path: str, reason: str, size: int | None = None):
        skipped.append({"path": rel_path, "reason": reason, "bytes": size or 0})

    for root, dirs, filenames in os.walk(repo_dir):
        # Deterministic traversal: os.walk order is not guaranteed.
        dirs[:] = sorted([d for d in dirs if d not in deny_dirs])
        filenames = sorted(filenames)

        if max_files_reached:
            break

        for fn in filenames:
            if files_scanned >= job.limits.max_files:
                if not max_files_reached:
                    record_skip("*", "max_files_reached")
                    max_files_reached = True
                break

            abs_path = os.path.join(root, fn)
            rel_path = os.path.relpath(abs_path, repo_dir).replace("\\", "/")
            files_scanned += 1

            # deny regex should use search(), not match()
            if any(rx.search(rel_path) for rx in deny_file_regex):
                record_skip(rel_path, "deny_file_regex")
                continue

            ext = Path(rel_path).suffix.lower().lstrip(".")
            if not allow_all and ext not in allow_exts:
                record_skip(rel_path, "ext_not_allowed")
                continue

            try:
                size = os.stat(abs_path).st_size
            except OSError:
                record_skip(rel_path, "stat_failed")
                continue

            if size > job.limits.max_file_bytes:
                record_skip(rel_path, "max_file_bytes_exceeded", size)
                continue

            if total_bytes + size > job.limits.max_total_bytes:
                record_skip(rel_path, "max_total_bytes_exceeded", size)
                continue

            try:
                raw = Path(abs_path).read_bytes()
            except Exception:
                record_skip(rel_path, "read_failed", size)
                continue

            # Binary detection (skip unless explicitly allowed)
            if (not allow_binary) and is_probably_binary(raw):
                record_skip(rel_path, "binary_file", size)
                continue

            sha = sha256_bytes(raw)
            language = infer_language(rel_path)

            imports: list[str] = []
            top_defs: list[str] = []
            flags: list[str] = []

            if language == "python":
                try:
                    text = raw.decode("utf-8", errors="replace")
                    top_defs, imports = parse_python_defs_and_imports(text)
                except Exception:
                    flags.append("python_parse_failed")
            else:
                try:
                    text = raw.decode("utf-8", errors="replace")
                    imports = parse_best_effort_imports(text)
                    flags.append("top_level_defs_unknown")
                except Exception:
                    flags.append("text_decode_failed")

            total_bytes += size
            files_included += 1
            files.append(
                {
                    "path": rel_path,
                    "bytes": size,
                    "sha256": sha,
                    "language": language,
                    "imports": imports,
                    "top_level_defs": top_defs,
                    "flags": flags,
                }
            )

        if max_files_reached:
            break

    files.sort(key=lambda x: x["path"])
    skipped.sort(key=lambda x: x["path"])

    read_plan_suggestions = suggest_files_to_read(files, max_files=120)

    # Job contract: payload + derived fields live under repo_index.job
    job_block = job.model_dump()
    # main.py patches this after clone
    job_block["resolved_commit"] = "unknown"

    return {
        "generated_at": utc_ts(),
        "job": job_block,
        "counts": {
            "files_scanned": files_scanned,
            "files_included": files_included,
            "files_skipped": len(skipped),
            "bytes_included": total_bytes,
        },
        "files": files,
        "skipped_files": skipped,
        "read_plan_suggestions": read_plan_suggestions,
    }


def write_json(path: str | Path, obj: dict) -> None:
    Path(path).write_text(json.dumps(obj, indent=2), encoding="utf-8")
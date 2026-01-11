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

# Python-only fallback import regex (kept for non-python text formats)
PY_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+([a-zA-Z0-9_\.]+)\s+import|import\s+([a-zA-Z0-9_\.]+))",
    re.M,
)

# --------------------------------------------------------------------------------------
# JS/TS import extraction (regex + comment stripping; deterministic)
# --------------------------------------------------------------------------------------

JS_ANY_IMPORT_EXPORT_FROM_RE = re.compile(
    r"""(?msx)
    ^\s*
    (?:import|export)\s+
    (?:type\s+)?                 # "import type ..." / "export type ..."
    (?:[\s\S]*?)                 # bindings (may be multiline)
    \sfrom\s*
    ["']([^"']+)["']\s*;?
    """
)

JS_IMPORT_SIDE_EFFECT_RE = re.compile(
    r"""(?mx)
    ^\s*import\s*["']([^"']+)["']\s*;?
    """
)

JS_DYNAMIC_IMPORT_RE = re.compile(r"""\bimport\s*\(\s*["']([^"']+)["']\s*\)""")
JS_REQUIRE_RE = re.compile(r"""\brequire\s*\(\s*["']([^"']+)["']\s*\)""")

TS_IMPORT_ASSIGN_REQUIRE_RE = re.compile(
    r"""(?mx)
    ^\s*import\s+[A-Za-z_\$][\w\$]*\s*=\s*require\s*\(\s*["']([^"']+)["']\s*\)\s*;?
    """
)

JS_TOP_DEF_RE = re.compile(
    r"""(?mx)
    ^\s*
    (?:export\s+(?:default\s+)?)?
    (?:declare\s+)?                 # TS
    (?:async\s+)?                   # async function
    (?:
        function\s+([A-Za-z_\$][\w\$]*)
      | class\s+([A-Za-z_\$][\w\$]*)
      | (?:const|let|var)\s+([A-Za-z_\$][\w\$]*)\s*=
      | interface\s+([A-Za-z_\$][\w\$]*)      # TS
      | type\s+([A-Za-z_\$][\w\$]*)\s*=       # TS
      | enum\s+([A-Za-z_\$][\w\$]*)           # TS
    )
    """
)

# ------------------------------------------------------------
# TS/JS path alias + module resolution (Option B)
# ------------------------------------------------------------

# Typical resolver extensions for TS/JS imports (ordered)
JS_RESOLVE_EXTS = (
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".d.ts",
    ".json",
)

# Extremely common config locations (we scan these deterministically)
CONFIG_CANDIDATES = (
    "tsconfig.json",
    "jsconfig.json",
    "frontend/tsconfig.json",
    "frontend/jsconfig.json",
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
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
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


def parse_best_effort_pythonish_imports(text: str) -> list[str]:
    """Fallback for non-Python, non-JS files (e.g., shell-ish snippets in docs)."""
    found = set()
    for m in PY_IMPORT_RE.finditer(text):
        mod = m.group(1) or m.group(2)
        if mod:
            found.add(mod)
    return sorted(found)


def strip_js_ts_comments(text: str) -> str:
    """
    Remove // and /* */ comments while preserving newlines.
    See earlier design notes in your existing file.
    """
    out: list[str] = []
    i = 0
    n = len(text)

    in_line = False
    in_block = False
    in_s = False
    in_d = False
    in_t = False
    esc = False

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if in_line:
            if c == "\n":
                in_line = False
                out.append("\n")
            else:
                out.append(" ")
            i += 1
            continue

        if in_block:
            if c == "*" and nxt == "/":
                in_block = False
                out.append("  ")
                i += 2
            else:
                out.append("\n" if c == "\n" else " ")
                i += 1
            continue

        # strings
        if in_s:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == "'":
                in_s = False
            i += 1
            continue

        if in_d:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_d = False
            i += 1
            continue

        if in_t:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == "`":
                in_t = False
            i += 1
            continue

        # comment starts (only when not in a string)
        if c == "/" and nxt == "/":
            in_line = True
            out.append("  ")
            i += 2
            continue

        if c == "/" and nxt == "*":
            in_block = True
            out.append("  ")
            i += 2
            continue

        # string starts
        if c == "'":
            in_s = True
            out.append(c)
            i += 1
            continue

        if c == '"':
            in_d = True
            out.append(c)
            i += 1
            continue

        if c == "`":
            in_t = True
            out.append(c)
            i += 1
            continue

        out.append(c)
        i += 1

    return "".join(out)


def parse_js_ts_imports(text: str) -> list[str]:
    """
    Extract JS/TS module specifiers:
    - import ... from 'x'
    - export ... from 'x'
    - import 'x'
    - import('x')
    - require('x')
    - import Foo = require('x') (TS)
    Runs on comment-stripped text to avoid false positives.
    """
    cleaned = strip_js_ts_comments(text)
    found: set[str] = set()

    for m in JS_IMPORT_SIDE_EFFECT_RE.finditer(cleaned):
        found.add(m.group(1))

    for m in JS_ANY_IMPORT_EXPORT_FROM_RE.finditer(cleaned):
        found.add(m.group(1))

    for m in TS_IMPORT_ASSIGN_REQUIRE_RE.finditer(cleaned):
        found.add(m.group(1))

    for m in JS_DYNAMIC_IMPORT_RE.finditer(cleaned):
        found.add(m.group(1))

    for m in JS_REQUIRE_RE.finditer(cleaned):
        found.add(m.group(1))

    return sorted(found)


def parse_js_ts_top_level_defs(text: str) -> list[str]:
    cleaned = strip_js_ts_comments(text)
    defs: set[str] = set()
    for m in JS_TOP_DEF_RE.finditer(cleaned):
        for g in m.groups():
            if g and isinstance(g, str):
                defs.add(g)
    return sorted(defs)


# -----------------------------
# Path alias parsing (tsconfig/jsconfig)
# -----------------------------

def _strip_jsonc(text: str) -> str:
    """
    Strip // and /* */ comments for JSONC-like configs (tsconfig/jsconfig).
    We reuse the JS/TS comment stripper (safe enough for typical configs).
    """
    return strip_js_ts_comments(text)


def _load_jsonc_file(abs_path: Path) -> dict[str, Any] | None:
    try:
        raw = abs_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    cleaned = _strip_jsonc(raw).strip()
    if not cleaned:
        return None
    try:
        obj = json.loads(cleaned)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _normalize_rel_path(p: str) -> str:
    return p.replace("\\", "/").lstrip("./")


def _extract_tsconfig_aliases(repo_dir: str) -> dict[str, Any]:
    """
    Best-effort extraction of:
      - compilerOptions.baseUrl
      - compilerOptions.paths
    Output format (stable):
      {
        "configs_used": [...],
        "baseUrl": "... or None",
        "paths": { "@/*": ["frontend/*", ...], ... },
        "alias_prefixes": [ {"alias_prefix":"@/", "targets":["frontend/"]}, ... ],
      }
    """
    repo_root = Path(repo_dir)
    used: list[str] = []
    base_url: str | None = None
    paths: dict[str, list[str]] = {}

    for rel in CONFIG_CANDIDATES:
        p = repo_root / rel
        if not p.exists() or not p.is_file():
            continue
        obj = _load_jsonc_file(p)
        if not obj:
            continue

        used.append(_normalize_rel_path(rel))
        co = obj.get("compilerOptions")
        if isinstance(co, dict):
            bu = co.get("baseUrl")
            if isinstance(bu, str) and bu.strip() and base_url is None:
                # baseUrl is relative to the config file's directory
                cfg_dir = str(p.parent).replace("\\", "/")
                # compute repo-relative baseUrl
                # If baseUrl is ".", it means config dir
                bu_norm = bu.strip()
                if bu_norm == ".":
                    # config directory relative to repo
                    base_url = _normalize_rel_path(os.path.relpath(cfg_dir, repo_dir))
                else:
                    base_url = _normalize_rel_path(
                        os.path.relpath(str((p.parent / bu_norm).resolve()), repo_dir)
                    )

            ps = co.get("paths")
            if isinstance(ps, dict):
                for k, v in ps.items():
                    if not isinstance(k, str):
                        continue
                    if isinstance(v, list):
                        vv = [x for x in v if isinstance(x, str) and x.strip()]
                        if vv:
                            # normalize each target relative to repo
                            norm_targets: list[str] = []
                            for t in vv:
                                # target is relative to baseUrl (ts behavior), but many repos treat it like relative to config.
                                # We’ll normalize as repo-relative by anchoring to config dir first,
                                # then stripping leading ./ and normalizing slashes.
                                # If t is absolute-like, we still normalize.
                                t0 = t.strip()
                                # Drop trailing "/*" later when we compute prefixes; keep raw too.
                                abs_t = (p.parent / t0).resolve()
                                norm_targets.append(_normalize_rel_path(os.path.relpath(str(abs_t), repo_dir)))
                            paths.setdefault(k, [])
                            for nt in norm_targets:
                                if nt not in paths[k]:
                                    paths[k].append(nt)

    # Build prefix-style alias rules for fast deterministic mapping.
    # We only support the common star-suffix patterns: "@/*" -> ["frontend/*"]
    alias_prefixes: list[dict[str, Any]] = []
    for alias_pat, targets in paths.items():
        if not isinstance(alias_pat, str):
            continue
        if "*" not in alias_pat:
            continue

        # only handle trailing "/*" or "*" cases deterministically
        if alias_pat.endswith("/*"):
            alias_prefix = alias_pat[:-1]  # keep trailing "/"
        elif alias_pat.endswith("*"):
            alias_prefix = alias_pat[:-1]
        else:
            continue

        t_prefixes: list[str] = []
        for t in targets:
            # normalize target prefix similarly
            if t.endswith("/*"):
                tp = t[:-1]
            elif t.endswith("*"):
                tp = t[:-1]
            else:
                # if target does not have star, treat as directory prefix if it ends with '/'
                tp = t if t.endswith("/") else (t + "/")
            tp = _normalize_rel_path(tp)
            if tp and tp not in t_prefixes:
                t_prefixes.append(tp)

        if alias_prefix and t_prefixes:
            alias_prefixes.append({"alias_prefix": alias_prefix, "targets": t_prefixes})

    # deterministic order
    alias_prefixes.sort(key=lambda x: (x.get("alias_prefix", ""), json.dumps(x, sort_keys=True)))

    return {
        "configs_used": used,
        "baseUrl": base_url,
        "paths": paths,
        "alias_prefixes": alias_prefixes,
    }


def _is_probably_external_js_spec(spec: str) -> bool:
    s = (spec or "").strip()
    if not s:
        return True
    if s.startswith(("http://", "https://", "data:")):
        return True
    # Node/TS special protocols
    if s.startswith(("node:", "bun:", "deno:")):
        return True
    # relative and rooted are internal candidates
    if s.startswith(("./", "../", "/")):
        return False
    # path aliases like @/ are internal candidates
    if s.startswith("@/"):
        return False
    # scoped packages (@scope/pkg) are external unless they match a known alias pattern
    # (we treat them as external; alias prefixes will handle them if configured)
    return True


def _candidate_paths_for_module_noext(module_noext: str) -> list[str]:
    """
    Given a repo-relative module path with no extension, produce candidates.
    """
    out: list[str] = []
    p = module_noext.rstrip("/")
    if not p:
        return out

    # direct file with extension
    for ext in JS_RESOLVE_EXTS:
        out.append(p + ext)

    # index files
    for ext in JS_RESOLVE_EXTS:
        out.append(p + "/index" + ext)

    return out


def _resolve_js_ts_import_to_repo_path(
    *,
    spec: str,
    from_file_repo_path: str,
    repo_dir: str,
    alias_info: dict[str, Any],
) -> str | None:
    """
    Resolve a TS/JS module specifier to a repo-relative path (if it maps to an existing file).
    Uses:
      - relative resolution
      - leading "/" resolution from repo root
      - tsconfig/jsconfig alias prefixes
      - baseUrl fallback for bare-ish internal paths (best-effort)

    Returns normalized repo-relative path with forward slashes, or None.
    """
    s = (spec or "").strip()
    if not s:
        return None

    repo_root = Path(repo_dir)
    from_dir = Path(from_file_repo_path).parent.as_posix().rstrip("/")

    # 1) relative
    if s.startswith(("./", "../")):
        base = f"{from_dir}/{s}" if from_dir else s
        module_noext = Path(base).as_posix()
        module_noext = os.path.normpath(module_noext).replace("\\", "/")
        module_noext = module_noext.lstrip("./")
        # If spec already has an ext, just check that exact file exists.
        if Path(s).suffix:
            cand = module_noext
            if (repo_root / cand).exists():
                return cand
            return None

        for cand in _candidate_paths_for_module_noext(module_noext):
            if (repo_root / cand).exists():
                return cand
        return None

    # 2) rooted from repo root
    if s.startswith("/"):
        module_noext = s.lstrip("/")
        # if ext already present
        if Path(module_noext).suffix:
            if (repo_root / module_noext).exists():
                return _normalize_rel_path(module_noext)
            return None
        for cand in _candidate_paths_for_module_noext(module_noext):
            if (repo_root / cand).exists():
                return _normalize_rel_path(cand)
        return None

    # 3) alias prefixes (from tsconfig/jsconfig)
    alias_prefixes = alias_info.get("alias_prefixes", [])
    if isinstance(alias_prefixes, list):
        for rule in alias_prefixes:
            if not isinstance(rule, dict):
                continue
            ap = rule.get("alias_prefix")
            targets = rule.get("targets")
            if not isinstance(ap, str) or not ap:
                continue
            if not isinstance(targets, list) or not targets:
                continue
            if s.startswith(ap):
                tail = s[len(ap):]
                tail = tail.lstrip("/")
                for tp in targets:
                    if not isinstance(tp, str) or not tp:
                        continue
                    module_noext = (tp + tail).replace("\\", "/")
                    module_noext = os.path.normpath(module_noext).replace("\\", "/")
                    module_noext = _normalize_rel_path(module_noext)

                    # if spec includes ext
                    if Path(s).suffix:
                        if (repo_root / module_noext).exists():
                            return module_noext
                        continue

                    for cand in _candidate_paths_for_module_noext(module_noext):
                        if (repo_root / cand).exists():
                            return _normalize_rel_path(cand)

    # 4) common Next alias fallback: "@/..." -> "frontend/..." (only if it exists)
    if s.startswith("@/"):
        tail = s[2:].lstrip("/")
        module_noext = _normalize_rel_path(f"frontend/{tail}")
        if Path(module_noext).suffix:
            if (repo_root / module_noext).exists():
                return module_noext
        else:
            for cand in _candidate_paths_for_module_noext(module_noext):
                if (repo_root / cand).exists():
                    return _normalize_rel_path(cand)

    # 5) baseUrl fallback for bare-ish internal paths (best-effort)
    base_url = alias_info.get("baseUrl")
    if isinstance(base_url, str) and base_url.strip():
        # Try baseUrl/spec as a path.
        module_noext = _normalize_rel_path(f"{base_url.rstrip('/')}/{s.lstrip('/')}")
        if Path(module_noext).suffix:
            if (repo_root / module_noext).exists():
                return module_noext
        else:
            for cand in _candidate_paths_for_module_noext(module_noext):
                if (repo_root / cand).exists():
                    return _normalize_rel_path(cand)

    return None


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

    # Option B: collect alias info once (deterministic)
    path_aliases = _extract_tsconfig_aliases(repo_dir)

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

            imports_raw: list[str] = []
            imports_resolved_internal: list[str] = []
            imports_external: list[str] = []

            top_defs: list[str] = []
            flags: list[str] = []

            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                record_skip(rel_path, "text_decode_failed", size)
                continue

            if language == "python":
                try:
                    top_defs, imports_raw = parse_python_defs_and_imports(text)
                except Exception:
                    flags.append("python_parse_failed")

            elif language in ("typescript", "javascript"):
                imports_raw = parse_js_ts_imports(text)
                top_defs = parse_js_ts_top_level_defs(text)
                if not top_defs:
                    flags.append("top_level_defs_best_effort_empty")

                # Option B: resolve internal JS/TS imports to canonical repo paths
                internal_set: set[str] = set()
                external_set: set[str] = set()

                for spec in imports_raw:
                    if _is_probably_external_js_spec(spec):
                        # Could still be internal via baseUrl, but we treat bare deps as external by default.
                        # We'll attempt baseUrl mapping too, but only if it resolves to an existing file.
                        resolved = _resolve_js_ts_import_to_repo_path(
                            spec=spec,
                            from_file_repo_path=rel_path,
                            repo_dir=repo_dir,
                            alias_info=path_aliases,
                        )
                        if resolved:
                            internal_set.add(resolved)
                        else:
                            external_set.add(spec)
                    else:
                        resolved = _resolve_js_ts_import_to_repo_path(
                            spec=spec,
                            from_file_repo_path=rel_path,
                            repo_dir=repo_dir,
                            alias_info=path_aliases,
                        )
                        if resolved:
                            internal_set.add(resolved)
                        else:
                            # relative/alias/root that doesn't resolve to a file still matters; keep as external-like token
                            # so downstream can see it and decide.
                            flags.append("import_unresolved")
                            external_set.add(spec)

                imports_resolved_internal = sorted(internal_set)
                imports_external = sorted(external_set)

            else:
                # fallback for other text formats
                imports_raw = parse_best_effort_pythonish_imports(text)
                flags.append("top_level_defs_unknown")

            total_bytes += size
            files_included += 1

            # Backward compatibility: keep "imports" as the raw list
            files.append(
                {
                    "path": rel_path,
                    "bytes": size,
                    "sha256": sha,
                    "language": language,
                    "imports": imports_raw,
                    "imports_raw": imports_raw,
                    "imports_resolved_internal": imports_resolved_internal,
                    "imports_external": imports_external,
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
        "path_aliases": path_aliases,  # Option B: allows pass2 to normalize deps deterministically
        "files": files,
        "skipped_files": skipped,
        "read_plan_suggestions": read_plan_suggestions,
    }


def write_json(path: str | Path, obj: dict) -> None:
    Path(path).write_text(json.dumps(obj, indent=2), encoding="utf-8")
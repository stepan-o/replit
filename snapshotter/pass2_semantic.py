# snapshotter/pass2_semantic.py
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any


class Pass2SemanticError(RuntimeError):
    pass


class Pass2SemanticLLMOutputError(Pass2SemanticError):
    """
    Raised when the model returns text that cannot be parsed into the required JSON object.

    Carries the raw (and optionally repaired) model output so the caller can persist it for inspection.
    """

    def __init__(self, message: str, *, raw_text: str, repaired_text: str | None = None):
        super().__init__(message)
        self.raw_text = raw_text
        self.repaired_text = repaired_text


@dataclass(frozen=True)
class SemanticCaps:
    onboarding_enabled: bool
    model: str
    max_output_tokens: int
    # input caps (defensive; prevents accidental huge prompts)
    max_arch_input_chars: int
    max_arch_files: int
    max_arch_chars_per_file: int


def _bool_from_env(name: str, default: bool) -> bool:
    v = os.environ.get(name, "").strip()
    if not v:
        return default
    return v not in ("0", "false", "False", "no", "NO", "off", "OFF")


def _int_from_env(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    if not v:
        return default
    try:
        return int(v)
    except Exception:
        return default


def _semantic_caps_from_env() -> SemanticCaps:
    onboarding_enabled = _bool_from_env("SNAPSHOTTER_PASS2_ONBOARDING", True)
    model = os.environ.get("SNAPSHOTTER_LLM_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"
    max_output_tokens = _int_from_env("SNAPSHOTTER_LLM_MAX_OUTPUT_TOKENS", 4000)

    max_arch_input_chars = _int_from_env("SNAPSHOTTER_PASS2_MAX_ARCH_INPUT_CHARS", 240_000)
    max_arch_files = _int_from_env("SNAPSHOTTER_PASS2_MAX_ARCH_FILES", 120)
    max_arch_chars_per_file = _int_from_env("SNAPSHOTTER_PASS2_MAX_ARCH_CHARS_PER_FILE", 9000)

    return SemanticCaps(
        onboarding_enabled=onboarding_enabled,
        model=model,
        max_output_tokens=max_output_tokens,
        max_arch_input_chars=max_arch_input_chars,
        max_arch_files=max_arch_files,
        max_arch_chars_per_file=max_arch_chars_per_file,
    )


def _extract_text_from_responses_obj(resp: Any) -> str:
    t = getattr(resp, "output_text", None)
    if isinstance(t, str) and t.strip():
        return t

    out = getattr(resp, "output", None)
    if not isinstance(out, list):
        return ""

    chunks: list[str] = []
    for item in out:
        if isinstance(item, dict):
            item_content = item.get("content")
        else:
            item_content = getattr(item, "content", None)

        if item_content and isinstance(item_content, list):
            for c in item_content:
                if isinstance(c, dict):
                    c_text = c.get("text")
                else:
                    c_text = getattr(c, "text", None)

                if isinstance(c_text, str) and c_text:
                    chunks.append(c_text)
                elif isinstance(c_text, dict):
                    try:
                        chunks.append(json.dumps(c_text, ensure_ascii=False))
                    except Exception:
                        pass

    return "".join(chunks)


def _looks_truncated(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    if not s.endswith("}"):
        return True

    in_str = False
    esc = False
    bal = 0
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            bal += 1
        elif ch == "}":
            bal -= 1
    return bal != 0


def _extract_first_json_object_span(text: str) -> str | None:
    s = text or ""
    start = None

    in_str = False
    esc = False
    bal = 0

    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            continue

        if ch == "{":
            if start is None:
                start = i
            bal += 1
        elif ch == "}":
            if start is not None:
                bal -= 1
                if bal == 0:
                    return s[start : i + 1]
    return None


def _try_parse_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        raise Pass2SemanticError("OpenAI response was empty; expected a JSON object.")

    try:
        obj = json.loads(text)
        if not isinstance(obj, dict):
            raise Pass2SemanticError("OpenAI response parsed but is not a JSON object.")
        return obj
    except Exception:
        candidate = _extract_first_json_object_span(text)
        if not candidate:
            m = re.search(r"\{.*\}", text, flags=re.DOTALL)
            if not m:
                raise Pass2SemanticError(f"OpenAI response was not valid JSON. First 400 chars:\n{text[:400]}")
            candidate = m.group(0)

        obj = json.loads(candidate)
        if not isinstance(obj, dict):
            raise Pass2SemanticError("Salvaged JSON parsed but is not a JSON object.")
        return obj


def _build_json_repair_prompt(bad_text: str) -> str:
    return (
        "You are a JSON repair tool.\n"
        "You will be given text that is intended to be a single JSON object, but may contain minor JSON syntax errors.\n"
        "Your task: output ONLY a valid JSON object that preserves the SAME structure and content as closely as possible.\n"
        "Rules:\n"
        "- Output JSON only. No markdown, no commentary.\n"
        "- Do not change top-level keys or semantics.\n"
        "- Only fix syntax (missing commas, quotes, escaping, trailing commas, etc.).\n"
        "- If the input is clearly truncated/incomplete and cannot be repaired losslessly, still output the best-effort JSON object you can, "
        "but NEVER invent new fields beyond what is implied.\n\n"
        "INPUT (verbatim):\n"
        + (bad_text or "")
    )


def _openai_call_json(*, prompt: str, model: str, max_output_tokens: int, system: str) -> dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise Pass2SemanticError("OPENAI_API_KEY is not set; cannot run pass2 semantic generation.")

    try:
        from openai import OpenAI  # type: ignore
    except Exception as e:
        raise Pass2SemanticError(f"openai python SDK not available or too old for Responses API: {e}") from e

    client = OpenAI(api_key=api_key)

    input_payload = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]

    def _responses_create_text(inp: Any) -> str:
        last_err: Exception | None = None
        for attempt_kwargs in (
            {
                "model": model,
                "input": inp,
                "max_output_tokens": max_output_tokens,
                "text": {"format": {"type": "json_object"}},
                "temperature": 0,
            },
            {
                "model": model,
                "input": inp,
                "max_output_tokens": max_output_tokens,
                "text": {"format": {"type": "json_object"}},
            },
            {
                "model": model,
                "input": inp,
                "max_tokens": max_output_tokens,
                "text": {"format": {"type": "json_object"}},
            },
        ):
            try:
                resp = client.responses.create(**attempt_kwargs)
                return _extract_text_from_responses_obj(resp)
            except TypeError as e:
                last_err = e
                continue
        raise Pass2SemanticError(f"OpenAI Responses API call failed due to incompatible SDK args: {last_err}")

    try:
        text = _responses_create_text(input_payload)
    except Exception as e:
        raise Pass2SemanticError(f"OpenAI Responses API call failed: {e}") from e

    try:
        return _try_parse_json(text)
    except Exception as parse_err:
        if _looks_truncated(text):
            raise Pass2SemanticLLMOutputError(
                "OpenAI returned truncated/incomplete JSON (likely hit max output tokens). "
                "Increase SNAPSHOTTER_LLM_MAX_OUTPUT_TOKENS and retry.\n"
                f"Parse error: {parse_err}\n"
                f"First 400 chars:\n{text[:400]}",
                raw_text=text,
            ) from parse_err

        repair_prompt = _build_json_repair_prompt(text)
        repair_input = [
            {"role": "system", "content": "You are a JSON repair tool. Output JSON only."},
            {"role": "user", "content": repair_prompt},
        ]

        repaired_text: str | None = None
        try:
            repaired_text = _responses_create_text(repair_input)
        except Exception as e:
            raise Pass2SemanticLLMOutputError(
                "OpenAI JSON repair call failed.\n"
                f"First 400 chars of original:\n{text[:400]}",
                raw_text=text,
            ) from e

        try:
            return _try_parse_json(repaired_text)
        except Exception as e:
            raise Pass2SemanticLLMOutputError(
                "Failed to parse OpenAI JSON response (including repair attempt).\n"
                f"Original first 400 chars:\n{text[:400]}",
                raw_text=text,
                repaired_text=repaired_text,
            ) from e


# -------------------------------------------------------------------
# Option B: read Pass1 raw + resolved imports
# -------------------------------------------------------------------

def _extract_pass1_import_edges(repo_index: dict[str, Any]) -> dict[str, dict[str, set[str]]]:
    """
    Returns:
      { file_path: { "raw": set[str], "resolved_internal": set[str], "external": set[str] } }
    """
    edges: dict[str, dict[str, set[str]]] = {}
    files = repo_index.get("files", []) or []
    for f in files:
        if not isinstance(f, dict):
            continue
        path = f.get("path")
        if not isinstance(path, str) or not path:
            continue

        raw_set: set[str] = set()
        internal_set: set[str] = set()
        external_set: set[str] = set()

        # Compatibility: old "imports" list
        imp = f.get("imports")
        if isinstance(imp, list):
            for x in imp:
                if isinstance(x, str) and x.strip():
                    raw_set.add(x.strip())

        # New: imports_raw
        imp_raw = f.get("imports_raw")
        if isinstance(imp_raw, list):
            for x in imp_raw:
                if isinstance(x, str) and x.strip():
                    raw_set.add(x.strip())

        # New: resolved internal + external
        imp_int = f.get("imports_resolved_internal")
        if isinstance(imp_int, list):
            for x in imp_int:
                if isinstance(x, str) and x.strip():
                    internal_set.add(x.strip())

        imp_ext = f.get("imports_external")
        if isinstance(imp_ext, list):
            for x in imp_ext:
                if isinstance(x, str) and x.strip():
                    external_set.add(x.strip())

        if raw_set or internal_set or external_set:
            edges[path] = {"raw": raw_set, "resolved_internal": internal_set, "external": external_set}

    return edges


def _language_by_path_from_repo_index(repo_index: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in repo_index.get("files", []) or []:
        if not isinstance(f, dict):
            continue
        p = f.get("path")
        lang = f.get("language")
        if isinstance(p, str) and p and isinstance(lang, str) and lang:
            out[p] = lang
    return out


def _repo_paths_set(repo_index: dict[str, Any]) -> set[str]:
    s: set[str] = set()
    for f in repo_index.get("files", []) or []:
        if isinstance(f, dict):
            p = f.get("path")
            if isinstance(p, str) and p:
                s.add(p)
    return s


def _truncate_with_tail(text: str, max_chars: int) -> str:
    if not isinstance(text, str):
        return ""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = int(max_chars * 0.75)
    tail = max_chars - head
    if tail < 200:
        return text[:max_chars]
    return text[:head] + "\n/* …TRUNCATED… */\n" + text[-tail:]


def _select_files_for_architecture(
    *,
    file_contents_map: dict[str, str],
    repo_index: dict[str, Any],
    caps: SemanticCaps,
) -> dict[str, str]:
    lang_by_path = _language_by_path_from_repo_index(repo_index)
    keys = sorted(file_contents_map.keys())

    def score(p: str) -> int:
        pl = p.lower()
        s = 0

        if pl in ("backend/main.py", "frontend/middleware.ts", "frontend/middleware.js"):
            s += 600
        if pl in ("frontend/app/layout.tsx", "frontend/app/layout.ts"):
            s += 560

        if pl.startswith("backend/routers/"):
            s += 420
        if pl in ("backend/security.py", "backend/models.py", "backend/db.py", "backend/config.py"):
            s += 380
        if pl.startswith("backend/migrations/"):
            s += 180
        if pl.startswith("backend/scripts/"):
            s += 140

        if "/app/api/" in pl and pl.endswith(("/route.ts", "/route.js")):
            s += 360
        if "/app/" in pl and pl.endswith("/layout.tsx"):
            s += 320
        if "/app/" in pl and pl.endswith("/page.tsx"):
            s += 260
        if pl.startswith("frontend/lib/"):
            s += 240
        if pl.startswith("frontend/components/"):
            s += 180

        if "pinterestpotential" in pl or "pinterest-potential" in pl:
            s += 120

        if pl.endswith("readme.md") or pl == "readme.md":
            s += 260
        if pl.startswith("docs/"):
            s += 220
        if pl.endswith(".md"):
            s += 110

        if pl.endswith(("pyproject.toml", "alembic.ini", "package.json", "next.config.ts", "next.config.js")):
            s += 170
        if "eslint" in pl or pl.endswith(("makefile", "uv.lock", "package-lock.json", "tsconfig.json")):
            s += 100

        lang = lang_by_path.get(p, "")
        if lang in ("python", "typescript", "javascript"):
            s += 10

        return s

    ranked = sorted(keys, key=lambda p: (-score(p), p))

    out: dict[str, str] = {}
    total = 0

    for p in ranked:
        if len(out) >= caps.max_arch_files:
            break

        c = file_contents_map.get(p, "")
        if not isinstance(c, str):
            continue

        remaining = caps.max_arch_input_chars - total
        if remaining <= 0:
            break

        c2 = _truncate_with_tail(c, caps.max_arch_chars_per_file)
        if len(c2) > remaining:
            c2 = _truncate_with_tail(c2, remaining)

        if not c2:
            continue

        out[p] = c2
        total += len(c2)

    if len(out) < 12:
        for p in keys:
            if len(out) >= min(24, caps.max_arch_files):
                break
            if p in out:
                continue
            c = file_contents_map.get(p, "")
            if not isinstance(c, str):
                continue
            remaining = caps.max_arch_input_chars - total
            if remaining <= 0:
                break
            c2 = _truncate_with_tail(c, min(caps.max_arch_chars_per_file, remaining))
            if not c2:
                continue
            out[p] = c2
            total += len(c2)

    return out


def _build_architecture_payload(
    *,
    repo_url: str,
    resolved_commit: str,
    job_id: str,
    repo_index: dict[str, Any],
    file_contents_map: dict[str, str],
    caps: SemanticCaps,
) -> dict[str, Any]:
    pass1_imports = _extract_pass1_import_edges(repo_index)

    arch_files = _select_files_for_architecture(file_contents_map=file_contents_map, repo_index=repo_index, caps=caps)
    files_pack: list[dict[str, Any]] = [{"path": p, "content": c} for p, c in arch_files.items()]

    # Compact imports view for LLM: keep BOTH raw and resolved.
    pass1_view = {
        k: {
            "raw": sorted(list(v.get("raw", set()))),
            "resolved_internal": sorted(list(v.get("resolved_internal", set()))),
            "external": sorted(list(v.get("external", set()))),
        }
        for k, v in pass1_imports.items()
    }

    return {
        "repo": {"repo_url": repo_url, "resolved_commit": resolved_commit, "job_id": job_id},
        "rules": {
            "grounding": "Responsibilities must be backed by evidence_paths. If unsure, use 'unknown' and add an uncertainty.",
            "dependencies": (
                "Dependencies should reconcile with pass1.imports_by_file for the cited evidence_paths. "
                "Prefer literal module specifiers. If you use alias forms (e.g. @/...), they must map to the same internal file as a resolved import."
            ),
            "evidence_paths_constraint": "Every module evidence_paths MUST be a subset of pass2.files[].path.",
            "no_deterministic_fields": "Do NOT output read_plan, coverage, files_read, files_not_read; those are injected by the pipeline.",
        },
        "pass1": {
            "counts": repo_index.get("counts", {}),
            "path_aliases": repo_index.get("path_aliases", {}),
            "imports_by_file": pass1_view,
        },
        "pass2": {"files": files_pack},
        "output_contract": {
            "return_json_object_with_keys": ["modules", "uncertainties"],
            "modules_require": ["name", "type", "responsibilities", "dependencies", "evidence_paths"],
        },
    }


def _architecture_prompt_text(payload: dict[str, Any]) -> str:
    return (
        "Generate Pass 2 architecture semantics for this repo snapshot.\n"
        "Return ONLY a single JSON object (no markdown) with keys:\n"
        "  - modules: array of module objects\n"
        "  - uncertainties: array\n\n"
        "Hard requirements:\n"
        "1) Each module MUST include evidence_paths and they MUST be a subset of pass2.files[].path.\n"
        "2) Responsibilities MUST be grounded in evidence_paths; otherwise set responsibilities=['unknown'] and add an uncertainty.\n"
        "3) Dependencies MUST reconcile with pass1.imports_by_file based on evidence_paths. "
        "If you name a dependency as an alias path, it must map to the same internal file as a resolved import.\n"
        "4) Keep output concise. Do NOT include read_plan/coverage/files_read/files_not_read.\n\n"
        "INPUT PAYLOAD (JSON):\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


def _select_supporting_files_for_gaps_and_onboarding(
    file_contents_map: dict[str, str], *, max_files: int = 28, max_total_chars: int = 120_000
) -> dict[str, str]:
    keys = sorted(file_contents_map.keys())

    def score(p: str) -> int:
        p_low = p.lower()
        s = 0
        if p_low.endswith("readme.md") or p_low == "readme.md":
            s += 200
        if p_low.startswith("docs/") or "/docs/" in p_low:
            s += 160
        if p_low.endswith(".md"):
            s += 120
        if p_low.endswith(("pyproject.toml", "alembic.ini")):
            s += 110
        if p_low.endswith(("makefile", "uv.lock")):
            s += 90
        if "next.config" in p_low or "eslint" in p_low:
            s += 80
        if p_low.endswith(("backend/main.py", "backend/config.py", "backend/security.py")):
            s += 75
        if p_low.endswith(("frontend/app/layout.tsx", "frontend/middleware.ts")):
            s += 70
        if p_low.endswith((".ts", ".tsx")):
            s += 10
        if p_low.endswith((".py",)):
            s += 10
        return s

    ranked = sorted(keys, key=lambda p: (-score(p), p))

    out: dict[str, str] = {}
    total = 0
    for p in ranked:
        if len(out) >= max_files:
            break
        c = file_contents_map.get(p, "")
        if not isinstance(c, str):
            continue
        remaining = max_total_chars - total
        if remaining <= 0:
            break
        if len(c) > remaining:
            c = c[:remaining]
        out[p] = c
        total += len(c)

    return out


def _build_gaps_onboarding_payload(
    *,
    repo_url: str,
    resolved_commit: str,
    job_id: str,
    repo_index: dict[str, Any],
    onboarding_enabled: bool,
    arch_modules: list[dict[str, Any]],
    arch_uncertainties: list[dict[str, Any]],
    deterministic_gap_items: list[dict[str, Any]],
    file_contents_map: dict[str, str],
) -> dict[str, Any]:
    pass1_imports = _extract_pass1_import_edges(repo_index)
    support_files = _select_supporting_files_for_gaps_and_onboarding(file_contents_map)

    modules_summary: list[dict[str, Any]] = []
    for m in arch_modules:
        if not isinstance(m, dict):
            continue
        modules_summary.append(
            {
                "name": m.get("name"),
                "type": m.get("type"),
                "dependencies": m.get("dependencies", []),
                "evidence_paths": m.get("evidence_paths", []),
            }
        )

    pass1_view = {
        k: {
            "raw": sorted(list(v.get("raw", set()))),
            "resolved_internal": sorted(list(v.get("resolved_internal", set()))),
            "external": sorted(list(v.get("external", set()))),
        }
        for k, v in pass1_imports.items()
    }

    return {
        "repo": {"repo_url": repo_url, "resolved_commit": resolved_commit, "job_id": job_id},
        "rules": {
            "onboarding_enabled": onboarding_enabled,
            "no_redundant_dumping": "Do NOT restate full code listings. Keep items concise and actionable.",
        },
        "pass1": {
            "counts": repo_index.get("counts", {}),
            "path_aliases": repo_index.get("path_aliases", {}),
            "imports_by_file": pass1_view,
        },
        "architecture_summary": {
            "modules": modules_summary,
            "uncertainties": arch_uncertainties,
        },
        "deterministic_gaps_already_found": deterministic_gap_items,
        "supporting_files": [{"path": p, "content": c} for p, c in support_files.items()],
        "output_contract": {
            "return_json_object_with_keys": ["gaps", "onboarding_md"],
            "gaps": {"must_include_keys": ["generated_at", "job_id", "items"]},
            "onboarding_md": {"type": "string", "allow_empty_if_disabled": True},
        },
    }


def _gaps_onboarding_prompt_text(payload: dict[str, Any]) -> str:
    return (
        "Generate Pass 2 gaps + onboarding artifacts.\n"
        "Return ONLY a single JSON object (no markdown) with keys:\n"
        "  - gaps: object\n"
        "  - onboarding_md: string\n\n"
        "Hard requirements:\n"
        "1) gaps must include keys: generated_at, job_id, items (array).\n"
        "2) Do NOT duplicate deterministic gaps already listed in deterministic_gaps_already_found.\n"
        "3) If onboarding_enabled is false, set onboarding_md to an empty string.\n"
        "4) Keep it concise and self-auditing.\n\n"
        "INPUT PAYLOAD (JSON):\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


def _norm_dep_token(s: str) -> str:
    return (s or "").strip()


def _candidate_repo_files_for_noext(base: str) -> list[str]:
    """
    For dep tokens like "@/lib/x" (no ext), try common TS/JS resolution patterns.
    This does NOT touch filesystem; it matches against repo_index paths.
    """
    b = base.rstrip("/")
    if not b:
        return []
    exts = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".d.ts", ".json")
    out: list[str] = []
    for e in exts:
        out.append(b + e)
    for e in exts:
        out.append(b + "/index" + e)
    return out


def _resolve_dep_token_to_repo_path(
    dep: str,
    *,
    repo_paths: set[str],
    path_aliases: dict[str, Any],
) -> str | None:
    """
    Resolve a dependency token (usually emitted by LLM) into a canonical repo-relative file path,
    using Pass1 alias info + repo inventory.

    Supports:
      - "@/x" via alias rules or fallback to "frontend/x"
      - "/x" from repo root
      - "frontend/..." direct
    """
    d = (dep or "").strip()
    if not d:
        return None

    # If it already looks like a repo path and exists, accept it.
    if d in repo_paths:
        return d

    # Strip possible extensions and try suffix-based matching
    # (We keep this conservative: only do it if it unambiguously matches via candidates below.)

    # Rooted repo path
    if d.startswith("/"):
        base = d.lstrip("/")
        if base in repo_paths:
            return base
        if "." in os.path.basename(base):
            return base if base in repo_paths else None
        for cand in _candidate_repo_files_for_noext(base):
            if cand in repo_paths:
                return cand
        return None

    # Alias prefixes from pass1.path_aliases
    alias_prefixes = path_aliases.get("alias_prefixes")
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
            if d.startswith(ap):
                tail = d[len(ap):].lstrip("/")
                for tp in targets:
                    if not isinstance(tp, str) or not tp:
                        continue
                    base = (tp + tail).replace("\\", "/").lstrip("./")
                    if base in repo_paths:
                        return base
                    if "." in os.path.basename(base):
                        if base in repo_paths:
                            return base
                        continue
                    for cand in _candidate_repo_files_for_noext(base):
                        if cand in repo_paths:
                            return cand

    # Common Next fallback: "@/..." -> "frontend/..."
    if d.startswith("@/"):
        base = ("frontend/" + d[2:].lstrip("/")).replace("\\", "/").lstrip("./")
        if base in repo_paths:
            return base
        if "." in os.path.basename(base):
            return base if base in repo_paths else None
        for cand in _candidate_repo_files_for_noext(base):
            if cand in repo_paths:
                return cand
        return None

    # If it looks like a direct internal path without extension
    if d.startswith(("frontend/", "backend/")):
        if d in repo_paths:
            return d
        if "." in os.path.basename(d):
            return d if d in repo_paths else None
        for cand in _candidate_repo_files_for_noext(d):
            if cand in repo_paths:
                return cand
        return None

    return None


def _dep_supported_by_imports(
    dep: str,
    *,
    imports_raw: set[str],
    imports_resolved_internal: set[str],
    repo_paths: set[str],
    path_aliases: dict[str, Any],
) -> bool:
    """
    Option B reconciliation:
      - accept exact/raw hierarchical matches (old behavior)
      - OR resolve dep token -> repo path and see if it matches evidence resolved imports
    """
    d = _norm_dep_token(dep)
    if not d:
        return False

    # 1) exact / hierarchical raw match (keeps prior behavior for package deps etc.)
    if d in imports_raw:
        return True
    for imp in imports_raw:
        if not imp:
            continue
        if "." in d or "." in imp:
            if imp.startswith(d + ".") or d.startswith(imp + "."):
                return True
        if "/" in d or "/" in imp:
            if imp.startswith(d + "/") or d.startswith(imp + "/"):
                return True

    # 2) resolved internal path match
    resolved = _resolve_dep_token_to_repo_path(d, repo_paths=repo_paths, path_aliases=path_aliases)
    if resolved and resolved in imports_resolved_internal:
        return True

    # 3) if dep resolved, also allow matching by no-ext stem equality (very common)
    if resolved:
        stem = re.sub(r"\.(d\.ts|ts|tsx|js|jsx|mjs|cjs|json)$", "", resolved)
        for ri in imports_resolved_internal:
            ri_stem = re.sub(r"\.(d\.ts|ts|tsx|js|jsx|mjs|cjs|json)$", "", ri)
            if ri_stem == stem:
                return True

    return False


def _post_enforce_architecture_constraints(
    *,
    modules: list[Any],
    uncertainties: list[Any],
    allowed_paths: set[str],
    pass1_imports: dict[str, dict[str, set[str]]],
    repo_paths: set[str],
    path_aliases: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    out_modules: list[dict[str, Any]] = []
    out_uncertainties: list[dict[str, Any]] = []
    deterministic_gap_items: list[dict[str, Any]] = []

    if isinstance(uncertainties, list):
        for u in uncertainties:
            if isinstance(u, dict):
                out_uncertainties.append(u)

    for i, m in enumerate(modules if isinstance(modules, list) else []):
        if not isinstance(m, dict):
            continue
        mm = dict(m)

        name = mm.get("name")
        if not isinstance(name, str) or not name.strip():
            mm["name"] = f"unknown_module_{i}"

        mtype = mm.get("type")
        if not isinstance(mtype, str) or not mtype.strip():
            mm["type"] = "unknown"

        ev = mm.get("evidence_paths")
        if not isinstance(ev, list):
            ev = []
        ev = [p for p in ev if isinstance(p, str) and p in allowed_paths]
        mm["evidence_paths"] = ev

        resp = mm.get("responsibilities")
        if not isinstance(resp, list):
            resp = []
        resp = [r for r in resp if isinstance(r, str) and r.strip()]

        if not ev:
            mm["responsibilities"] = ["unknown"]
            out_uncertainties.append(
                {
                    "type": "ungrounded_module",
                    "description": f"Module '{mm.get('name','unknown')}' lacks evidence_paths in files_read; responsibilities set to unknown.",
                    "files_involved": [],
                    "suggested_questions": ["Which files define this module's responsibilities? Add them to read_plan."],
                }
            )
        else:
            mm["responsibilities"] = resp or ["unknown"]
            if not resp:
                out_uncertainties.append(
                    {
                        "type": "empty_responsibilities",
                        "description": f"Module '{mm.get('name','unknown')}' had no responsibilities listed; set to unknown.",
                        "files_involved": ev,
                        "suggested_questions": ["What does this module do? Add explicit responsibilities."],
                    }
                )

        deps = mm.get("dependencies")
        if not isinstance(deps, list):
            deps = []
        deps = [_norm_dep_token(d) for d in deps if isinstance(d, str) and d.strip()]
        mm["dependencies"] = deps

        # Dependency mismatch detection (Option B-aware)
        if deps and ev and pass1_imports:
            evidence_raw: set[str] = set()
            evidence_resolved_internal: set[str] = set()

            for p in ev:
                info = pass1_imports.get(p)
                if not info:
                    continue
                evidence_raw |= info.get("raw", set())
                evidence_resolved_internal |= info.get("resolved_internal", set())

            # Framework implicit exceptions
            has_tsx_jsx = any(str(p).lower().endswith((".tsx", ".jsx")) for p in ev)
            for d in deps:
                if d == "react" and has_tsx_jsx:
                    continue
                if d == "next" and any(imp.startswith("next/") for imp in evidence_raw):
                    continue

                supported = _dep_supported_by_imports(
                    d,
                    imports_raw=evidence_raw,
                    imports_resolved_internal=evidence_resolved_internal,
                    repo_paths=repo_paths,
                    path_aliases=path_aliases,
                )
                if not supported:
                    deterministic_gap_items.append(
                        {
                            "type": "dependency_mismatch",
                            "severity": "warning",
                            "module": mm.get("name", "unknown"),
                            "dependency": d,
                            "evidence_paths": ev,
                            "description": "Dependency not supported by Pass 1 imports (raw or resolved internal) from the module evidence files.",
                        }
                    )

        out_modules.append(mm)

    return out_modules, out_uncertainties, deterministic_gap_items


def _normalize_gaps_object(gaps: Any, *, job_id: str) -> dict[str, Any]:
    if not isinstance(gaps, dict):
        gaps = {}
    out = dict(gaps)
    out.setdefault("generated_at", None)
    out["job_id"] = job_id
    items = out.get("items")
    if not isinstance(items, list):
        items = []
    cleaned: list[dict[str, Any]] = []
    for it in items:
        if isinstance(it, dict):
            cleaned.append(it)
    out["items"] = cleaned
    return out


def _dedupe_gap_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        key = json.dumps(
            {
                "type": it.get("type"),
                "severity": it.get("severity"),
                "module": it.get("module"),
                "dependency": it.get("dependency"),
                "description": it.get("description"),
                "evidence_paths": it.get("evidence_paths"),
                "files_involved": it.get("files_involved"),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _fix_false_missing_route_gap_items(items: list[dict[str, Any]], allowed_paths: set[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        t = it.get("type")
        files_involved = it.get("files_involved")
        present: list[str] = []
        if isinstance(files_involved, list):
            present = [p for p in files_involved if isinstance(p, str) and p in allowed_paths]

        if t == "missing_route" and present:
            it2 = dict(it)
            it2["type"] = "route_implementation_check"
            it2["severity"] = it2.get("severity") or "medium"
            it2["description"] = (
                (it2.get("description") or "").strip()
                + " (Referenced route file(s) exist in snapshot; verify if implementation is placeholder or not wired.)"
            ).strip()
            out.append(it2)
        else:
            out.append(it)
    return out


def generate_pass2_semantic_artifacts(
    *,
    repo_url: str,
    resolved_commit: str,
    job_id: str,
    repo_index: dict[str, Any],
    files_read: list[dict[str, Any]],
    files_not_read: list[dict[str, Any]],
    file_contents_map: dict[str, str],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    caps = _semantic_caps_from_env()

    arch_payload = _build_architecture_payload(
        repo_url=repo_url,
        resolved_commit=resolved_commit,
        job_id=job_id,
        repo_index=repo_index,
        file_contents_map=file_contents_map,
        caps=caps,
    )
    arch_prompt = _architecture_prompt_text(arch_payload)

    arch_obj = _openai_call_json(
        prompt=arch_prompt,
        model=caps.model,
        max_output_tokens=caps.max_output_tokens,
        system="You are a precise code analyst. Output JSON only.",
    )

    modules = arch_obj.get("modules", [])
    uncertainties = arch_obj.get("uncertainties", [])

    allowed_paths = set(file_contents_map.keys())
    pass1_imports = _extract_pass1_import_edges(repo_index)
    repo_paths = _repo_paths_set(repo_index)
    path_aliases = repo_index.get("path_aliases", {}) if isinstance(repo_index.get("path_aliases", {}), dict) else {}

    enforced_modules, enforced_uncertainties, deterministic_gap_items = _post_enforce_architecture_constraints(
        modules=modules,
        uncertainties=uncertainties,
        allowed_paths=allowed_paths,
        pass1_imports=pass1_imports,
        repo_paths=repo_paths,
        path_aliases=path_aliases,
    )

    arch_out: dict[str, Any] = {
        "modules": enforced_modules,
        "uncertainties": enforced_uncertainties,
    }

    gaps_payload = _build_gaps_onboarding_payload(
        repo_url=repo_url,
        resolved_commit=resolved_commit,
        job_id=job_id,
        repo_index=repo_index,
        onboarding_enabled=caps.onboarding_enabled,
        arch_modules=enforced_modules,
        arch_uncertainties=enforced_uncertainties,
        deterministic_gap_items=deterministic_gap_items,
        file_contents_map=file_contents_map,
    )
    gaps_prompt = _gaps_onboarding_prompt_text(gaps_payload)

    gaps_obj = _openai_call_json(
        prompt=gaps_prompt,
        model=caps.model,
        max_output_tokens=caps.max_output_tokens,
        system="You are a precise repo auditor. Output JSON only.",
    )

    gaps_raw = _normalize_gaps_object(gaps_obj.get("gaps"), job_id=job_id)
    onboarding_md = gaps_obj.get("onboarding_md") or ""
    if not caps.onboarding_enabled:
        onboarding_md = ""

    merged_items = list(deterministic_gap_items)
    llm_items = gaps_raw.get("items", [])
    if isinstance(llm_items, list):
        for it in llm_items:
            if isinstance(it, dict):
                merged_items.append(it)

    merged_items = _fix_false_missing_route_gap_items(merged_items, allowed_paths)

    gaps_out = dict(gaps_raw)
    gaps_out["items"] = _dedupe_gap_items(merged_items)

    if not isinstance(onboarding_md, str):
        raise Pass2SemanticError("onboarding_md is not a string after processing.")
    if not isinstance(gaps_out, dict):
        raise Pass2SemanticError("gaps output has invalid type.")
    if not isinstance(arch_out, dict):
        raise Pass2SemanticError("architecture output has invalid type.")

    return arch_out, gaps_out, onboarding_md
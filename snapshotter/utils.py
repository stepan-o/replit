import hashlib
import os
import re
from datetime import datetime, timezone


def utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def sha256_bytes(b: bytes) -> str:
    h = hashlib.sha256()
    h.update(b)
    return h.hexdigest()


def is_probably_binary(data: bytes, *, sample_size: int = 8192, min_utf8_ratio: float = 0.85) -> bool:
    """
    Deterministic binary heuristic:
      - If sample contains NUL bytes -> binary.
      - Else, if UTF-8 "ignore" decoding preserves too little -> binary.

    min_utf8_ratio is:
      preserved_bytes / sample_bytes
    where preserved_bytes is len(decoded.encode("utf-8")) after decoding with errors="ignore".
    """
    if not data:
        return False

    sample = data[:sample_size]

    if b"\x00" in sample:
        return True

    # If it decodes as UTF-8 cleanly, treat as text.
    try:
        sample.decode("utf-8")
        return False
    except UnicodeDecodeError:
        pass

    decoded = sample.decode("utf-8", errors="ignore")
    preserved = len(decoded.encode("utf-8"))
    ratio = preserved / max(1, len(sample))
    return ratio < min_utf8_ratio


def repo_slug_from_url(repo_url: str) -> str:
    # "https://github.com/org/repo.git" -> "org__repo"
    s = repo_url.rstrip("/")
    s = re.sub(r"\.git$", "", s)
    parts = s.split("/")
    if len(parts) >= 2:
        return f"{parts[-2]}__{parts[-1]}"
    return re.sub(r"[^a-zA-Z0-9]+", "_", s).strip("_").lower()


def getenv(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v is not None else default
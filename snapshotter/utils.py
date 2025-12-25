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
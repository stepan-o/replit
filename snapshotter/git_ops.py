import os
import subprocess
from pathlib import Path

def run(cmd: list[str], cwd: str | None = None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\nSTDOUT:\n{p.stdout}\nSTDERR:\n{p.stderr}")
    return p.stdout.strip()

def clone_and_checkout(repo_url: str, ref: str, workdir: str) -> str:
    Path(workdir).mkdir(parents=True, exist_ok=True)
    repo_dir = os.path.join(workdir, "repo")
    if os.path.exists(repo_dir):
        # clean slate
        run(["rm", "-rf", repo_dir])

    run(["git", "clone", "--depth", "1", repo_url, repo_dir], cwd=workdir)

    # If ref is not the default branch, try fetching it.
    # (Works for branches/tags; commits may need full fetch later.)
    try:
        run(["git", "fetch", "--depth", "1", "origin", ref], cwd=repo_dir)
        run(["git", "checkout", ref], cwd=repo_dir)
    except Exception:
        # If this fails, still proceed on default branch; resolved_commit will reveal reality.
        pass

    resolved_commit = run(["git", "rev-parse", "HEAD"], cwd=repo_dir)
    return resolved_commit
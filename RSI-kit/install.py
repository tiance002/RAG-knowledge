"""Create an isolated, version-pinned Git checkout for the RSI handoff."""
import argparse
from pathlib import Path
import subprocess
import sys

REV = "09a3146d04e9c0c9f2cd1feed842c86e8af2ef74"
BASE = "90d86ce87e8c76c8c45f5f296160ddf53de71d54"
REMOTE = "https://github.com/tiance002/RAG-knowledge.git"
FILES = ("scripts/run_rsi_offline.py", "scripts/rsi_candidates.py", "scripts/rsi_feedback.py",
         "scripts/demo_rsi_inheritance.ps1", "experiments/r0/synthetic.json", "experiments/r1/e1c-readiness.json")

def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

def verify(path):
    if git(path, "rev-parse", "HEAD") != REV:
        raise ValueError("Unexpected checkout. Use a new destination.")
    git(path, "merge-base", "--is-ancestor", BASE, REV)
    for name in FILES:
        if not (path / name).is_file():
            raise ValueError("Missing: " + name)
    git(path, "diff", "--quiet", REV, "--", "scripts", "backend/app", "experiments")
    print("Checkout verified:", path, REV)

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dest", required=True, type=Path)
    p.add_argument("--source", type=Path, help="Existing local clone containing the pinned commit")
    p.add_argument("--check", action="store_true", help="Read-only check of existing checkout")
    args = p.parse_args()
    target = args.dest.resolve()
    if not args.check:
        if args.source:
            source = args.source.resolve(strict=True)
            git(source, "cat-file", "-e", REV + "^{commit}")
            if source == target or source in target.parents:
                raise ValueError("Destination must be outside the source repository.")
        target.mkdir(parents=True, exist_ok=False)
        git(target, "init", "--quiet")
        git(target, "config", "core.autocrlf", "false")
        git(target, "remote", "add", "origin", REMOTE)
        git(target, "fetch", "--no-tags", str(source) if args.source else "origin", REV)
        git(target, "checkout", "-q", "-b", "dots/rsi-work", REV)
    verify(target)

if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print("Setup stopped:", error, file=sys.stderr)
        sys.exit(1)

"""Reject private DLT data already tracked/staged, even after git add -f.

Read-only. Never removes files or rewrites Git history. Invoke before a commit
or push and in CI. This checks names, not arbitrary secrets embedded in source.
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath


def blocked_path(filename: str) -> bool:
    path = PurePosixPath(filename.replace("\\", "/").lower())
    if "artifacts" in path.parts or path.parts[:2] == ("examples", "demo-output"):
        return True
    if path.suffix in {".dlt", ".jsonl", ".fibex"}:
        # Exact allowlist, never allow every database under tests/.
        # qdlt/tests/testfile.dlt is a 71-byte fixture committed upstream
        # before this policy existed; blocking it would fail every run.
        allowed = {"tests/nonverbose.fibex", "qdlt/tests/testfile.dlt"}
        return filename.replace("\\", "/") not in allowed
    return False


def check_repository(repository: Path) -> dict:
    root = repository.resolve()
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                            capture_output=True, timeout=10, check=False)
    if result.returncode:
        raise ValueError("Not a Git repository; cannot verify tracked/staged files")
    root = Path(result.stdout.decode("utf-8", errors="strict").strip()).resolve()
    result = subprocess.run(["git", "-C", str(root), "ls-files", "--cached", "-z"],
                            capture_output=True, timeout=10, check=False)
    if result.returncode:
        raise ValueError("Git could not enumerate tracked/staged files")
    names = result.stdout.decode("utf-8", errors="strict").split("\x00")
    names = [name for name in names if name]
    blocked = sorted({name for name in names if blocked_path(name)})
    return {"schema_version": 1, "status": "failed" if blocked else "passed",
            "repository": str(root), "tracked_files": len(names),
            "blocked_count": len(blocked), "blocked_files": blocked[:20],
            "scope": "Git index paths only; existing history and embedded secrets not audited"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args(argv)
    try:
        report = check_repository(args.repository)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({"schema_version": 1, "status": "error", "error": str(exc)},
                         ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 1 if report["blocked_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

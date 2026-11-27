"""Generate SYNTHETIC logs and exercise read/summary/check without an ECU."""

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from fixtures import write_sample


def main() -> int:
    parser = argparse.ArgumentParser(description="Synthetic DLT CLI demo (not a real ECU test)")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "examples" / "demo-output")
    args = parser.parse_args()
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    sample = directory / "sample.dlt"
    if sample.exists():
        parser.error(f"Refusing to overwrite existing sample: {sample}; use a new --output-dir")
    write_sample(sample)
    artifacts = {"sample": str(sample)}
    for command, filename, extra in (
        ("read", "records.jsonl", []),
        ("summary", "summary.json", []),
        ("check", "report.json", ["--rules", str(ROOT / "examples" / "smoke.rules.json")]),
    ):
        output = directory / filename
        result = subprocess.run([sys.executable, str(ROOT / "dlt_cli.py"), command,
                                 str(sample), "--output", str(output), *extra],
                                capture_output=True, encoding="utf-8", timeout=60)
        if result.returncode:
            sys.stderr.write(result.stderr)
            return result.returncode
        artifacts[command] = str(output)
    print(json.dumps({"synthetic_demo": True, "passed": True, "artifacts": artifacts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
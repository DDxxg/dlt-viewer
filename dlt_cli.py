"""Standalone entry point; run dlt-cli.ps1 or Python 3.10+ dlt_cli.py."""

import sys

if sys.version_info < (3, 10):
    sys.stderr.write('{"schema_version":1,"status":"error","error":"Python 3.10+ required"}\n')
    raise SystemExit(2)

from dlt_ai.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
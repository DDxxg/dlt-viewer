"""Machine-readable CLI entry points. Native diagnostic output never reaches stdout."""

import argparse
import json
import math
import os
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .capture import capture
from .native import NativeReader
from .records import CliError, LEVELS, Matcher
from .rules import evaluate, load_rules
from .workflow import run_workflow


def emit(value: Any, stream: TextIO = sys.stdout) -> None:
    stream.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")


@contextmanager
def destination(filename: str | None, protected: list[str]):
    if filename:
        path = Path(filename).resolve()
        if path in {Path(item).resolve() for item in protected if item}:
            raise CliError("Output must not overwrite an input/rules/configuration file")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                             dir=path.parent, prefix=".dlt-ai-", delete=False) as stream:
                temporary = Path(stream.name)
                yield stream
            os.replace(temporary, path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
    else:
        yield sys.stdout


def positive_number(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return result


def nonnegative_number(value: str) -> int:
    result = int(value)
    if result < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return result


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliError(f"Invalid CLI arguments: {message}")


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(description="DLT AI CLI: UTF-8 JSONL, summaries, assertions and TCP capture")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Check native CLI and Python environment")
    doctor.add_argument("--commander")
    doctor.add_argument("--timeout", type=positive_number, default=10)
    for name, help_text in (("read", "Export filtered records as JSONL"),
                            ("summary", "Summarize logs with bounded evidence"),
                            ("check", "Evaluate JSON test assertions; exit 0=pass, 1=fail, 2=error")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("logfiles", nargs="+")
        command.add_argument("--commander", help="Path to dlt-commander executable")
        command.add_argument("--timeout", type=positive_number, default=120, help="Native export timeout per file")
        command.add_argument("--fibex", help="FIBEX file/directory for Non Verbose Mode Plugin")
        command.add_argument("--native-filter", help="Native .dlf filter file")
        command.add_argument("--output", "-o", help="UTF-8 output file; default stdout")
        if name != "check":
            for field in ("ecu", "apid", "ctid", "type"):
                command.add_argument("--" + field, action="append", help="Exact match; repeat for OR")
            command.add_argument("--level", choices=list(LEVELS), action="append")
            command.add_argument("--max-level", choices=list(LEVELS), help="Inclusive severity, e.g. warn includes fatal/error/warn")
            command.add_argument("--contains", help="Payload substring")
            command.add_argument("--regex", help="Payload regular expression")
            command.add_argument("--ignore-case", action="store_true", help="Case-insensitive payload matching only")
        if name == "read":
            command.add_argument("--limit", type=nonnegative_number, default=0, help="0=all; use a limit for AI context budgets")
        if name == "summary":
            command.add_argument("--evidence-limit", type=nonnegative_number, default=10)
        if name == "check":
            command.add_argument("--rules", required=True, help="JSON assertions document")
            command.add_argument("--evidence-limit", type=nonnegative_number, default=3)
    for name in ("capture", "run"):
        command = commands.add_parser(name, help=("Capture a bounded DLTv1 TCP stream" if name == "capture"
                                                 else "Capture + optional test trigger + decode + assert"))
        command.add_argument("--host", required=True, help="Remote ECU/daemon hostname or IP (not local bind address)")
        command.add_argument("--port", type=int, default=3490, help="Remote ECU/daemon port; default 3490")
        command.add_argument("--local-host", help="Local IPv4/IPv6 bind address; default route-selected interface")
        command.add_argument("--local-port", type=nonnegative_number, default=0,
                     help="Local TCP source port, 0-65535; default 0=automatic")
        command.add_argument("--output", "-o", required=True, help="New capture .dlt file; never overwritten")
        command.add_argument("--duration", type=positive_number, default=10)
        command.add_argument("--connect-timeout", type=positive_number, default=5)
        command.add_argument("--max-messages", type=nonnegative_number, default=0)
        command.add_argument("--ecu", default="ECU1", help="Fallback storage ECU ID, 1-4 ASCII characters")
        if name == "run":
            command.add_argument("--rules", required=True)
            command.add_argument("--report", help="Atomic UTF-8 JSON report; default stdout")
            trigger = command.add_mutually_exclusive_group()
            trigger.add_argument("--trigger-json", help="Test command as JSON argv array; runs after TCP is connected")
            trigger.add_argument("--trigger-file", help="UTF-8 JSON argv file (recommended for Windows/AI)")
            command.add_argument("--trigger-timeout", type=positive_number, default=30)
            command.add_argument("--commander")
            command.add_argument("--timeout", type=positive_number, default=120)
            command.add_argument("--fibex")
            command.add_argument("--native-filter")
            command.add_argument("--evidence-limit", type=nonnegative_number, default=3)
    return parser


def execute(args: argparse.Namespace) -> int:
    if args.command == "capture":
        emit(capture(args.host, args.port, args.output, args.duration,
                     args.connect_timeout, args.max_messages, args.ecu,
                     local_host=args.local_host, local_port=args.local_port))
        return 0
    reader = NativeReader(args.commander, args.timeout,
                          getattr(args, "fibex", None), getattr(args, "native_filter", None))
    if args.command in ("check", "run") and reader.native_filter:
        raise CliError("--native-filter is not permitted for check/run; use rule match "
                       "to avoid hiding failures in the original log")
    if args.command == "doctor":
        probe = reader.probe()
        emit({"schema_version": 1, "status": "ok", "cli_version": __version__,
              "python": sys.version.split()[0], "python_executable": sys.executable,
              "commander": str(reader.commander),
              "capability_probe": probe, "decoder": "native_utf8_text",
              "supported_input": "DLTv1 storage files", "external_python_dependencies": []})
        return 0
    if args.command == "run":
        report = run_workflow(args, reader)
        with destination(args.report, [args.output, args.rules, str(reader.commander),
                                       args.native_filter, args.fibex, args.trigger_file]) as stream:
            emit(report, stream)
        return 0 if report["passed"] else 1
    records = reader.records(args.logfiles)
    protected = args.logfiles + [str(reader.commander), args.fibex, args.native_filter,
                                getattr(args, "rules", None)]
    if args.command == "check":
        spec = load_rules(args.rules)
        report = evaluate(records, spec, args.evidence_limit)
        with destination(args.output, protected) as stream:
            emit(report, stream)
        return 0 if report["passed"] else 1
    matcher = Matcher(**{key: getattr(args, key) for key in
                         ("ecu", "apid", "ctid", "level", "type", "contains", "regex",
                          "ignore_case", "max_level")})
    if args.command == "read":
        with destination(args.output, protected) as stream:
            matched = 0
            for record in records:
                if matcher.matches(record):
                    emit(record.to_dict(), stream)
                    matched += 1
                    if args.limit and matched >= args.limit:
                        break
        return 0
    scanned = matched = undecoded = 0
    levels: Counter[str] = Counter()
    types: Counter[str] = Counter()
    contexts: Counter[tuple[str, str, str]] = Counter()
    sources: Counter[str] = Counter()
    evidence = []
    for record in records:
        scanned += 1
        if not matcher.matches(record):
            continue
        matched += 1
        undecoded += int(record.type != "control" and not record.decoded)
        levels[record.level] += 1
        types[record.type] += 1
        contexts[(record.ecu, record.apid, record.ctid)] += 1
        sources[record.source] += 1
        if record.type == "log" and LEVELS.get(record.level, 99) <= 3 and len(evidence) < args.evidence_limit:
            evidence.append(record.to_dict())
    report = {"schema_version": 1, "scanned_messages": scanned, "matched_messages": matched,
              "undecoded_messages": undecoded, "levels": dict(levels), "types": dict(types),
              "sources": dict(sources), "contexts": [
                  {"ecu": ecu, "apid": apid, "ctid": ctid, "count": count}
                  for (ecu, apid, ctid), count in contexts.most_common()], "problem_evidence": evidence}
    with destination(args.output, protected) as stream:
        emit(report, stream)
    return 0


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 10):
        sys.stderr.write("Python 3.10 or newer is required\n")
        return 2
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    try:
        args = build_parser().parse_args(argv)
        return execute(args)
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        emit({"schema_version": 1, "status": "error", "error": "Interrupted"}, sys.stderr)
        return 130
    except (CliError, OSError, UnicodeError, ValueError) as exc:
        emit({"schema_version": 1, "status": "error", "error": str(exc)}, sys.stderr)
        return 2
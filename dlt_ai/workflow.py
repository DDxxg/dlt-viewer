"""Connect -> capture concurrently with a test trigger -> decode -> assert."""

import json
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .capture import capture
from .native import NativeReader
from .records import CliError
from .rules import evaluate, load_rules


def parse_trigger(text: str | None) -> list[str] | None:
    if text is None:
        return None
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise CliError(f"--trigger-json must be a JSON argv array: {exc}") from exc
    if (not isinstance(value, list) or not value or
            not all(isinstance(item, str) and item and "\x00" not in item for item in value)):
        raise CliError("--trigger-json must be a nonempty array of nonempty strings")
    return value


def run_workflow(args: Any, reader: NativeReader) -> dict[str, Any]:
    spec = load_rules(args.rules)
    trigger_text = (Path(args.trigger_file).read_text(encoding="utf-8-sig")
                    if getattr(args, "trigger_file", None) else args.trigger_json)
    trigger = parse_trigger(trigger_text)
    protected = {Path(args.rules).resolve(), reader.commander}
    if getattr(args, "trigger_file", None):
        protected.add(Path(args.trigger_file).resolve())
    if reader.native_filter:
        protected.add(reader.native_filter)
    if args.report and Path(args.report).resolve() in protected | {Path(args.output).resolve()}:
        raise CliError("Report must not overwrite capture/rules/native configuration")
    ready, stop = threading.Event(), threading.Event()
    trigger_result = None
    # Trigger is an argv array, never shell=True. Native chatter and trigger
    # chatter must not contaminate the single JSON report on stdout.
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(capture, args.host, args.port, args.output,
                             args.duration, args.connect_timeout, args.max_messages,
                             args.ecu, on_ready=ready.set, stop=stop,
                             local_host=getattr(args, "local_host", None),
                             local_port=getattr(args, "local_port", 0))
        try:
            if not ready.wait(args.connect_timeout + 1):
                if future.done():
                    future.result()
                raise CliError("Capture did not become ready; test trigger was not executed")
            if trigger:
                with tempfile.TemporaryFile() as diagnostics:
                    timed_out = False
                    trigger_started_unix_ns = time.time_ns()
                    try:
                        result = subprocess.run(trigger, stdout=diagnostics,
                                                stderr=subprocess.STDOUT,
                                                timeout=args.trigger_timeout, check=False)
                        returncode = result.returncode
                    except subprocess.TimeoutExpired:
                        timed_out, returncode = True, None
                        stop.set()
                    size = diagnostics.tell()
                    diagnostics.seek(max(0, size - 4000))
                    tail = diagnostics.read().decode("utf-8", errors="replace")
                    trigger_result = {"argv": trigger, "returncode": returncode,
                                      "timed_out": timed_out, "output_tail": tail,
                                      "started_unix_ns": trigger_started_unix_ns,
                                      "finished_unix_ns": time.time_ns()}
            try:
                capture_result = future.result()
            except (CliError, OSError) as exc:
                if trigger_result and trigger_result["timed_out"]:
                    raise CliError(f"Test trigger timed out; capture failed: {exc}") from exc
                raise
        except BaseException:
            stop.set()
            raise
    report = evaluate(reader.records([args.output]), spec, args.evidence_limit)
    report["capture"] = capture_result
    report["trigger"] = trigger_result
    if trigger_result and (trigger_result["timed_out"] or trigger_result["returncode"] != 0):
        report["passed"] = False
        report["status"] = "failed"
        report["failures"].append("Test trigger timed out or returned a nonzero exit code")
    if (trigger_result and
            trigger_result["finished_unix_ns"] > capture_result["finished_unix_ns"]):
        report["passed"] = False
        report["status"] = "failed"
        report["failures"].append("Capture ended before test trigger completed; observation window is incomplete")
    if capture_result["stop_reason"] == "duration" and capture_result["discarded_tail_bytes"]:
        report["passed"] = False
        report["status"] = "failed"
        report["failures"].append("Capture deadline interrupted a message; increase capture duration")
    return report
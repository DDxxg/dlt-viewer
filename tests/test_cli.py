"""Unit and integration tests using the REAL bundled DLT Commander and local TCP."""

import argparse
import json
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from dlt_ai.capture import capture
from dlt_ai.cli import main
from dlt_ai.native import ROOT, NativeReader, validate_dlt
from dlt_ai.records import CliError, Matcher, parse_native_line
from dlt_ai.rules import evaluate, load_rules
from dlt_ai.workflow import parse_trigger, run_workflow
from fixtures import message, storage, write_sample


class LocalDaemon:
    def __init__(self, data: bytes, hold: bool = False, host: str = "127.0.0.1"):
        self.data, self.hold = data, hold
        self.peer = None
        self.server = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
        self.server.bind((host, 0))
        self.server.listen(1)
        self.server.settimeout(5)
        self.port = self.server.getsockname()[1]
        self.release = threading.Event()
        self.connected = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def serve(self):
        try:
            connection, self.peer = self.server.accept()
            with connection:
                self.connected.set()
                # Deliberately cross header/payload boundaries on writes.
                for index in range(0, len(self.data), 3):
                    connection.sendall(self.data[index:index + 3])
                if self.hold:
                    self.release.wait(5)
        except (OSError, socket.timeout):
            pass

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.release.set()
        self.server.close()
        self.thread.join(5)


class TemporaryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dlt tests 中文 ")
        self.directory = Path(self.temp.name)
        self.sample = self.directory / "sample.dlt"
        write_sample(self.sample)
        self.spec = load_rules(str(ROOT / "examples" / "smoke.rules.json"))

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *arguments):
        return subprocess.run([sys.executable, str(ROOT / "dlt_cli.py"), *map(str, arguments)],
                              cwd=self.directory, capture_output=True, timeout=20,
                              encoding="utf-8")


class RecordTests(unittest.TestCase):
    def test_empty_ids_and_payload_spacing(self):
        record = parse_native_line("0 2023/11/15 06:13:20.123456 0.0000 0    0 log info verbose 1   text\r\n", "test")
        self.assertEqual((record.ecu, record.apid, record.ctid), ("", "", ""))
        self.assertEqual(record.payload, "  text")

    def test_invalid_layout(self):
        for line in ("", "bad native output", "x 2023/11/15 06:13:20.123456 0 0 E A C 0 log info verbose 1 text"):
            with self.subTest(line=line), self.assertRaises(CliError):
                parse_native_line(line, "test")

    def test_filters(self):
        record = parse_native_line("0 2023/11/15 06:13:20.123456 0 0 ECU1 TEST MAIN 0 log error verbose 1 Fault_42", "test")
        self.assertTrue(Matcher(apid=["OTHER", "TEST"], max_level="warn").matches(record))
        self.assertTrue(Matcher(regex=r"fault_\d+", ignore_case=True).matches(record))
        self.assertTrue(Matcher(contains="FAULT", ignore_case=True).matches(record))
        self.assertFalse(Matcher(ctid=["OTHER"]).matches(record))
        self.assertFalse(Matcher(max_level="fatal").matches(record))
        self.assertFalse(Matcher(contains="fault").matches(record))

    def test_invalid_regex(self):
        with self.assertRaises(CliError):
            Matcher(regex="[")


class FramingTests(TemporaryTest):
    def test_valid_count(self):
        self.assertEqual(validate_dlt(self.sample), 5)

    def test_corruption_rejected(self):
        original = self.sample.read_bytes()
        cases = [original[:-1], original + b"garbage", b"DLT\x02" + original[4:],
                 original[:16] + b"\x35\x00\x00\x04" + original[20:],
                 original[:8] + struct.pack("<I", 1000000) + original[12:]]
        for value in cases:
            with self.subTest(value=value[:20]):
                self.sample.write_bytes(value)
                with self.assertRaises(CliError):
                    validate_dlt(self.sample)


class NativeTests(TemporaryTest):
    def test_probe_real_export(self):
        report = NativeReader().probe()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["messages"], 1)

    def test_probe_rejects_missing_empty_or_wrong_exports(self):
        def no_output(reader, arguments):
            return ""

        def empty_output(reader, arguments):
            Path(arguments[arguments.index("-c") + 1]).write_text("", encoding="utf-8")
            return ""

        def wrong_payload(reader, arguments):
            Path(arguments[arguments.index("-c") + 1]).write_text(
                "0 2023/11/15 06:13:20.123456 1.2345 7 ECU1 TEST MAIN 0 log info verbose 1 wrong\n",
                encoding="utf-8")
            return "-u -c -signature"

        for implementation in (no_output, empty_output, wrong_payload):
            with self.subTest(implementation=implementation.__name__), patch.object(NativeReader, "run", implementation):
                with self.assertRaises(CliError):
                    NativeReader().probe()

    def test_real_native_unicode_and_escaping(self):
        records = list(NativeReader().records([str(self.sample)]))
        self.assertEqual(len(records), 5)
        self.assertEqual(records[2].payload, "中文测试完成")
        self.assertEqual(records[1].payload, 'temperature=42, unit="C"')
        self.assertEqual(records[3].payload, "first line second line")
        self.assertEqual(records[2].timestamp, 2.0)
        self.assertEqual((records[0].ecu, records[0].apid, records[0].ctid), ("ECU1", "TEST", "MAIN"))

    def test_nonverbose_not_assumed_decoded(self):
        self.sample.write_bytes(storage(message("", verbose=False)))
        record = next(NativeReader().records([str(self.sample)]))
        self.assertFalse(record.decoded)
        self.assertIn("1234", record.payload)

    def test_native_empty_ids(self):
        frame = message("empty ids").replace(b"TESTMAIN", bytes(8)).replace(b"ECU1", bytes(4))
        self.sample.write_bytes(storage(frame).replace(b"ECU1", bytes(4)))
        record = next(NativeReader().records([str(self.sample)]))
        self.assertEqual((record.ecu, record.apid, record.ctid), ("", "", ""))
        self.assertEqual(record.payload, "empty ids")

    def test_empty_file(self):
        self.sample.write_bytes(b"")
        self.assertEqual(list(NativeReader().records([str(self.sample)])), [])

    def test_all_inputs_validated_before_output(self):
        bad = self.directory / "bad.dlt"
        bad.write_bytes(b"invalid")
        with self.assertRaises(CliError):
            next(NativeReader().records([str(self.sample), str(bad)]))

    def test_filter_uses_real_native_tool(self):
        records = list(NativeReader(native_filter=str(ROOT / "filters" / "error_fatal_messages.dlf"))
                       .records([str(self.sample)]))
        self.assertEqual(records, [])

    def test_missing_native(self):
        with self.assertRaises(CliError):
            NativeReader(commander=str(self.directory / "missing.exe"))

    def test_timeout(self):
        with patch("dlt_ai.native.subprocess.run", side_effect=subprocess.TimeoutExpired("native", 1)):
            with self.assertRaises(CliError):
                NativeReader().run(["-h"])

    def test_native_export_count_mismatch(self):
        def fake_run(reader, arguments):
            Path(arguments[arguments.index("-c") + 1]).write_text("", encoding="utf-8")
            return "success"
        with patch.object(NativeReader, "run", fake_run), self.assertRaises(CliError):
            list(NativeReader().records([str(self.sample)]))


class RuleTests(TemporaryTest):
    def test_smoke_passes(self):
        report = evaluate(NativeReader().records([str(self.sample)]), self.spec)
        self.assertTrue(report["passed"])
        self.assertEqual([rule["count"] for rule in report["rules"]], [1, 1, 0])

    def test_error_and_missing_marker_fail(self):
        self.sample.write_bytes(storage(message("BOOT_OK", level=2)))
        report = evaluate(NativeReader().records([str(self.sample)]), self.spec)
        self.assertFalse(report["passed"])
        self.assertEqual(len(report["failures"]), 3)

    def test_empty_log_fails(self):
        self.assertFalse(evaluate([], self.spec)["passed"])

    def test_nonverbose_fails_closed(self):
        self.sample.write_bytes(storage(message("", verbose=False)))
        self.spec["min_messages"] = 1
        self.spec["rules"] = [{"name": "some data", "match": {}, "min_count": 1}]
        self.assertFalse(evaluate(NativeReader().records([str(self.sample)]), self.spec)["passed"])
        self.spec["require_decoded"] = False
        self.assertTrue(evaluate(NativeReader().records([str(self.sample)]), self.spec)["passed"])

    def test_bounded_evidence(self):
        self.spec["rules"] = [{"name": "all", "match": {}, "min_count": 1}]
        report = evaluate(NativeReader().records([str(self.sample)]), self.spec, 2)
        self.assertEqual(report["rules"][0]["count"], 5)
        self.assertEqual(len(report["rules"][0]["evidence"]), 2)

    def test_invalid_schema_rejected(self):
        cases = [dict(self.spec, typo=1), dict(self.spec, schema_version=True),
                 dict(self.spec, require_decoded="yes"), dict(self.spec, min_messages=True),
                 dict(self.spec, rules=[]), dict(self.spec, rules=[self.spec["rules"][0]] * 2),
                 dict(self.spec, rules=[{"name": "bad", "match": {}, "min_count": 2, "max_count": 1}]),
                 dict(self.spec, rules=[{"name": "bad", "match": {"max_level": []}, "max_count": 0}]),
                 dict(self.spec, rules=[{"name": "bad", "match": {"contains": None}, "max_count": 0}]),
                 dict(self.spec, rules=[{"name": "bad", "match": {"apid": []}, "min_count": 1}]),
                 dict(self.spec, rules=[{"name": "bad", "match": {"level": "erorr"}, "max_count": 0}]),
                 dict(self.spec, rules=[{"name": "bad", "match": {}, "max_count": None}])]
        path = self.directory / "rules.json"
        for spec in cases:
            with self.subTest(spec=spec):
                path.write_text(json.dumps(spec), encoding="utf-8")
                with self.assertRaises(CliError):
                    load_rules(str(path))

    def test_duplicate_json_keys_rejected(self):
        path = self.directory / "rules.json"
        path.write_text('{"schema_version": 1, "schema_version": 1}', encoding="utf-8")
        with self.assertRaises(CliError):
            load_rules(str(path))


class CliTests(TemporaryTest):
    def test_doctor_stdout_is_json(self):
        result = self.cli("doctor")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "ok")
        self.assertEqual(json.loads(result.stdout)["capability_probe"]["status"], "passed")

    def test_doctor_succeeds_with_silent_native_console(self):
        real_run = NativeReader.run
        calls = []

        def silent_run(reader, arguments):
            calls.append(arguments)
            real_run(reader, arguments)
            return ""

        with patch.object(NativeReader, "run", silent_run), patch("dlt_ai.cli.emit") as emit:
            self.assertEqual(main(["doctor"]), 0)
        self.assertEqual(emit.call_args.args[0]["capability_probe"]["status"], "passed")
        self.assertTrue(calls)
        self.assertTrue(all("-u" in args and "-c" in args and "-h" not in args for args in calls))

    def test_doctor_probe_failure_is_json_error(self):
        with patch.object(NativeReader, "probe", side_effect=CliError("bad probe")), patch("dlt_ai.cli.emit") as emit:
            self.assertEqual(main(["doctor"]), 2)
        self.assertEqual(emit.call_args.args[0]["status"], "error")

    def test_read_is_clean_jsonl_with_limits(self):
        result = self.cli("read", self.sample, "--limit", "3")
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[2]["payload"], "中文测试完成")
        self.assertTrue(rows[0]["decoded"])

    def test_unicode_filter(self):
        result = self.cli("read", self.sample, "--contains", "中文")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["index"], 2)

    def test_summary(self):
        result = self.cli("summary", self.sample, "--apid", "TEST")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["matched_messages"], 5)
        self.assertEqual(report["levels"], {"info": 5})

    def test_check_exit_codes_and_atomic_report(self):
        output = self.directory / "report.json"
        result = self.cli("check", self.sample, "--rules", ROOT / "examples" / "smoke.rules.json", "-o", output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertTrue(json.loads(output.read_text(encoding="utf-8"))["passed"])
        self.sample.write_bytes(b"")
        result = self.cli("check", self.sample, "--rules", ROOT / "examples" / "smoke.rules.json")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(json.loads(result.stdout)["passed"])
        self.sample.write_bytes(b"invalid")
        result = self.cli("check", self.sample, "--rules", ROOT / "examples" / "smoke.rules.json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertEqual(json.loads(result.stderr)["status"], "error")

    def test_no_overwriting_input_or_default_native(self):
        before = self.sample.read_bytes()
        result = self.cli("read", self.sample, "-o", self.sample)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.sample.read_bytes(), before)
        result = self.cli("read", self.sample, "-o", ROOT / "dlt-commander.exe")
        self.assertEqual(result.returncode, 2)

    def test_failed_export_does_not_replace_existing_output(self):
        output = self.directory / "records.jsonl"
        output.write_text("previous result", encoding="utf-8")
        self.sample.write_bytes(b"invalid")
        result = self.cli("read", self.sample, "-o", output)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(output.read_text(encoding="utf-8"), "previous result")
        self.assertEqual(list(self.directory.glob(".dlt-ai-*")), [])

    def test_multi_file_source_tracking(self):
        result = self.cli("summary", self.sample, self.sample)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["matched_messages"], 10)

    def test_bad_regex_error_is_json(self):
        result = self.cli("read", self.sample, "--regex", "[")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["status"], "error")

    def test_argument_errors_are_json(self):
        for arguments in (("read",), ("unknown",), ("read", str(self.sample), "--limit", "-1"),
                          ("capture", "--host", "localhost", "-o", "test.dlt", "--duration", "nan")):
            with self.subTest(arguments=arguments):
                result = self.cli(*arguments)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stderr)["status"], "error")

    def test_native_filter_cannot_hide_test_errors(self):
        result = self.cli("check", self.sample, "--rules", ROOT / "examples" / "smoke.rules.json",
                          "--native-filter", ROOT / "filters" / "error_fatal_messages.dlf")
        self.assertEqual(result.returncode, 2)
        self.assertIn("hiding failures", json.loads(result.stderr)["error"])


class CaptureTests(TemporaryTest):
    def unused_local_port(self):
        # Release immediately before capture; the peer observation proves the
        # subsequent connection really used this port rather than an auto port.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            return reservation.getsockname()[1]

    def test_fragmented_tcp_roundtrip(self):
        data = b"DLS\x01" + message("BOOT_OK") + b"DLS\x01" + message("TEST_DONE", counter=1)
        output = self.directory / "capture.dlt"
        with LocalDaemon(data) as server:
            result = capture("127.0.0.1", server.port, str(output), duration=2)
        self.assertEqual(result["messages"], 2)
        self.assertEqual(result["stop_reason"], "peer_closed")
        self.assertEqual((result["local_host"], result["local_port"]), server.peer)
        self.assertEqual(validate_dlt(output), 2)
        self.assertTrue(evaluate(NativeReader().records([str(output)]), self.spec)["passed"])

    def test_explicit_local_address_and_port(self):
        output = self.directory / "bound.dlt"
        with LocalDaemon(message("BOOT_OK")) as server:
            local_port = self.unused_local_port()
            result = capture("127.0.0.1", server.port, str(output), duration=2,
                             local_host="127.0.0.1", local_port=local_port)
        self.assertEqual(server.peer, ("127.0.0.1", local_port))
        self.assertEqual((result["local_host"], result["local_port"]), server.peer)
        self.assertEqual(validate_dlt(output), 1)

    def test_local_port_without_local_host(self):
        with LocalDaemon(message("BOOT_OK")) as server:
            local_port = self.unused_local_port()
            result = capture("127.0.0.1", server.port, str(self.directory / "port-only.dlt"),
                             local_port=local_port)
        self.assertEqual(server.peer, ("127.0.0.1", local_port))
        self.assertEqual(result["local_port"], local_port)

    def test_wildcard_local_host_with_automatic_port(self):
        with LocalDaemon(message("BOOT_OK")) as server:
            result = capture("127.0.0.1", server.port, str(self.directory / "auto.dlt"),
                             local_host="0.0.0.0", local_port=0)
        self.assertEqual(result["local_host"], "127.0.0.1")
        self.assertGreater(result["local_port"], 0)
        self.assertEqual((result["local_host"], result["local_port"]), server.peer)

    @unittest.skipUnless(socket.has_ipv6, "IPv6 unavailable")
    def test_ipv6_local_binding(self):
        try:
            daemon = LocalDaemon(message("BOOT_OK"), host="::1")
        except OSError as exc:
            self.skipTest(f"IPv6 loopback unavailable: {exc}")
        with daemon as server:
            result = capture("::1", server.port, str(self.directory / "ipv6.dlt"),
                             local_host="::1")
        self.assertEqual(result["local_host"], "::1")
        self.assertEqual((result["local_host"], result["local_port"]), server.peer[:2])

    def test_cli_capture_local_binding(self):
        with LocalDaemon(message("BOOT_OK")) as server:
            local_port = self.unused_local_port()
            result = self.cli("capture", "--host", "127.0.0.1", "--port", server.port,
                              "--local-host", "127.0.0.1", "--local-port", local_port,
                              "--output", self.directory / "cli-bound.dlt")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(server.peer, ("127.0.0.1", local_port))
        self.assertEqual((report["local_host"], report["local_port"]), server.peer)

    @unittest.skipUnless(sys.platform == "win32", "Windows PowerShell launcher")
    def test_powershell_capture_local_binding(self):
        output = self.directory / "powershell-bound.dlt"
        with LocalDaemon(message("BOOT_OK")) as server:
            local_port = self.unused_local_port()
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-File", str(ROOT / "dlt-cli.ps1"),
                 "capture", "--host", "127.0.0.1", "--port", str(server.port),
                 "--local-host", "127.0.0.1", "--local-port", str(local_port), "--output", str(output)],
                cwd=self.directory, capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(server.peer, ("127.0.0.1", local_port))
        self.assertEqual((report["local_host"], report["local_port"]), server.peer)
        self.assertEqual(validate_dlt(output), 1)

    def test_invalid_local_binding_rejected_before_connection(self):
        output = self.directory / "invalid-bind.dlt"
        for kwargs in ({"local_port": -1}, {"local_port": 65536}, {"local_port": True},
                       {"local_port": 1.5}, {"local_host": ""}, {"local_host": "not-an-ip"}):
            with self.subTest(kwargs=kwargs), patch("dlt_ai.capture.socket.create_connection") as connect:
                with self.assertRaises(CliError):
                    capture("127.0.0.1", 3490, str(output), **kwargs)
                connect.assert_not_called()
                self.assertFalse(output.exists())

    def test_cli_invalid_local_port_is_json_error(self):
        for port in ("-1", "65536", "not-a-port"):
            with self.subTest(port=port):
                result = self.cli("capture", "--host", "127.0.0.1", "--local-port", port,
                                  "--output", self.directory / "invalid-bind.dlt")
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertEqual(json.loads(result.stderr)["status"], "error")

    def test_occupied_local_port_fails_without_fallback_or_trigger(self):
        marker = self.directory / "must-not-exist.txt"
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            port = occupied.getsockname()[1]
            for command in ("capture", "run"):
                output = self.directory / (command + "-occupied.dlt")
                options = [command, "--host", "127.0.0.1", "--port", str(port),
                           "--local-host", "127.0.0.1", "--local-port", str(port), "--output", str(output)]
                if command == "run":
                    trigger = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
                    options += ["--rules", str(ROOT / "examples" / "smoke.rules.json"),
                                "--trigger-json", json.dumps(trigger), "--connect-timeout", "0.1"]
                with self.subTest(command=command):
                    result = self.cli(*options)
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertIn("TCP connect/bind failed", json.loads(result.stderr)["error"])
                    self.assertFalse(output.exists())
                    self.assertFalse(marker.exists())

    def test_duration_is_bounded(self):
        output = self.directory / "capture.dlt"
        with LocalDaemon(message("BOOT_OK"), hold=True) as server:
            result = capture("127.0.0.1", server.port, str(output), duration=0.2)
        self.assertEqual(result["stop_reason"], "duration")
        self.assertLess(result["elapsed_seconds"], 2)

    def test_max_messages(self):
        output = self.directory / "capture.dlt"
        with LocalDaemon(message("BOOT_OK") * 3, hold=True) as server:
            result = capture("127.0.0.1", server.port, str(output), duration=2, max_messages=1)
        self.assertEqual(result["messages"], 1)
        self.assertEqual(result["stop_reason"], "message_limit")

    def test_truncated_and_empty_peer_fail(self):
        for data in (message("BOOT_OK")[:-1], b"", b"HTTP invalid"):
            output = self.directory / (str(len(data)) + ".dlt")
            with LocalDaemon(data) as server, self.assertRaises(CliError):
                capture("127.0.0.1", server.port, str(output), duration=2)

    def test_capture_does_not_overwrite(self):
        with self.assertRaises(CliError):
            capture("127.0.0.1", 3490, str(self.sample))

    def test_invalid_capture_options(self):
        output = str(self.directory / "capture.dlt")
        for kwargs in ({"duration": 0}, {"connect_timeout": float("nan")}, {"fallback_ecu": "中文"},
                       {"fallback_ecu": "TOOLONG"}, {"max_messages": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(CliError):
                capture("127.0.0.1", 3490, output, **kwargs)

    def test_run_complete_closed_loop(self):
        output = self.directory / "workflow.dlt"
        with LocalDaemon(message("BOOT_OK") + message("TEST_DONE", counter=1), hold=True) as server:
            local_port = self.unused_local_port()
            trigger = json.dumps([sys.executable, "-c", "print('trigger ran')"])
            result = self.cli("run", "--host", "127.0.0.1", "--port", server.port,
                              "--local-host", "127.0.0.1", "--local-port", local_port,
                              "--duration", "0.4", "--output", output,
                              "--rules", ROOT / "examples" / "smoke.rules.json",
                              "--trigger-json", trigger)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["passed"])
        self.assertEqual(report["capture"]["messages"], 2)
        self.assertEqual(server.peer, ("127.0.0.1", local_port))
        self.assertEqual((report["capture"]["local_host"], report["capture"]["local_port"]), server.peer)
        self.assertIn("trigger ran", report["trigger"]["output_tail"])

    def test_trigger_failure_fails_good_logs(self):
        with LocalDaemon(message("BOOT_OK") + message("TEST_DONE", counter=1)) as server:
            result = self.cli("run", "--host", "127.0.0.1", "--port", server.port,
                              "--output", self.directory / "workflow.dlt",
                              "--rules", ROOT / "examples" / "smoke.rules.json",
                              "--trigger-json", json.dumps([sys.executable, "-c", "raise SystemExit(7)"]))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(json.loads(result.stdout)["trigger"]["returncode"], 7)

    def test_trigger_timeout_report(self):
        with LocalDaemon(message("BOOT_OK"), hold=True) as server:
            result = self.cli("run", "--host", "127.0.0.1", "--port", server.port,
                              "--output", self.directory / "workflow.dlt", "--duration", "3",
                              "--rules", ROOT / "examples" / "smoke.rules.json", "--trigger-timeout", "0.2",
                              "--trigger-json", json.dumps([sys.executable, "-c", "import threading; threading.Event().wait(5)"]))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(json.loads(result.stdout)["trigger"]["timed_out"])

    def test_trigger_file_and_atomic_workflow_report(self):
        trigger = self.directory / "trigger.json"
        trigger.write_text(json.dumps([sys.executable, "-c", "print('file trigger')"]), encoding="utf-8")
        report = self.directory / "report.json"
        with LocalDaemon(message("BOOT_OK") + message("TEST_DONE", counter=1), hold=True) as server:
            result = self.cli("run", "--host", "127.0.0.1", "--port", server.port,
                              "--duration", "0.4",
                              "--output", self.directory / "workflow.dlt",
                              "--rules", ROOT / "examples" / "smoke.rules.json",
                              "--trigger-file", trigger, "--report", report)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("file trigger", json.loads(report.read_text(encoding="utf-8"))["trigger"]["output_tail"])

    def test_capture_ending_before_trigger_cannot_pass(self):
        with LocalDaemon(message("BOOT_OK") + message("TEST_DONE", counter=1)) as server:
            result = self.cli("run", "--host", "127.0.0.1", "--port", server.port,
                              "--output", self.directory / "workflow.dlt",
                              "--rules", ROOT / "examples" / "smoke.rules.json",
                              "--trigger-json", json.dumps([sys.executable, "-c", "import threading; threading.Event().wait(0.2)"]))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("observation window is incomplete", " ".join(json.loads(result.stdout)["failures"]))

    def test_trigger_runs_only_after_connection_ready(self):
        args = argparse.Namespace(host="127.0.0.1", port=0, output=str(self.directory / "workflow.dlt"),
                                  duration=2, connect_timeout=0.2, max_messages=0, ecu="ECU1",
                                  rules=str(ROOT / "examples" / "smoke.rules.json"), report=None,
                                  trigger_json='["must-not-run"]', trigger_timeout=1, evidence_limit=3)
        with patch("dlt_ai.workflow.subprocess.run") as trigger, self.assertRaises(CliError):
            run_workflow(args, NativeReader())
        trigger.assert_not_called()

    def test_invalid_trigger(self):
        for trigger in ("not json", '"shell text"', "[]", '["x", null]', '["x", ""]'):
            with self.subTest(trigger=trigger), self.assertRaises(CliError):
                parse_trigger(trigger)


if __name__ == "__main__":
    unittest.main()
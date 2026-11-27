"""Offline evidence auditing must detect source/JSONL mismatch."""

import json
import tempfile
import unittest
from pathlib import Path

from dlt_ai.native import ROOT, NativeReader
from dlt_ai.records import CliError
from examples.audit_records import audit
from fixtures import message, storage


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.logfile = Path(self.temp.name) / "sample.dlt"
        self.records = Path(self.temp.name) / "records.jsonl"
        self.fibex = ROOT / "tests" / "nonverbose.fibex"
        self.logfile.write_bytes(storage(message("", verbose=False)))
        self.row = next(NativeReader(fibex=str(self.fibex)).records([str(self.logfile)])).to_dict()
        self.write_rows([self.row])

    def tearDown(self):
        self.temp.cleanup()

    def write_rows(self, rows):
        self.records.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def test_full_source_audit(self):
        report = audit(self.logfile, self.records, self.fibex)
        self.assertEqual(report["messages"], 1)
        self.assertEqual(report["undecoded_application_messages"], 0)
        self.assertEqual(report["message_ids"][0]["name"], "TEST_VALUE")
        self.assertEqual(len(report["provenance"]["dlt"]["sha256"]), 64)

    def test_metadata_tampering_rejected(self):
        for field, value in (("source_offset", 1), ("counter", 1), ("message_id", 9999),
                             ("timestamp", 123), ("apid", "FAKE"), ("type", "control")):
            with self.subTest(field=field):
                self.write_rows([{**self.row, field: value}])
                with self.assertRaises(CliError):
                    audit(self.logfile, self.records, self.fibex)

    def test_missing_or_extra_rows_rejected(self):
        for rows in ([], [self.row, self.row]):
            self.write_rows(rows)
            with self.assertRaises(CliError):
                audit(self.logfile, self.records, self.fibex)


if __name__ == "__main__":
    unittest.main()
"""FIBEX integration and binary metadata preservation regression tests."""

import struct
import tempfile
import unittest
from pathlib import Path

from dlt_ai.native import ROOT, NativeReader, preserve_source_frames
from dlt_ai.records import CliError
from dlt_ai.rules import evaluate
from fixtures import message, storage


CONTROL = bytes.fromhex("3700001f5a52303100000fc526000000000000000000000000230000000018")


class FibexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "sample.dlt"
        self.fibex = str(ROOT / "tests" / "nonverbose.fibex")

    def tearDown(self):
        self.temp.cleanup()

    def test_real_native_fibex_decode(self):
        self.path.write_bytes(storage(message("", verbose=False)))
        record = next(NativeReader(fibex=self.fibex).records([str(self.path)]))
        self.assertTrue(record.decoded)
        self.assertEqual(record.payload, "value= 513")
        self.assertEqual(record.message_id, 1234)
        self.assertEqual(record.source_offset, 0)

    def test_real_nonverbose_without_extended_header(self):
        frame = message("", verbose=False)
        flags, counter, length = struct.unpack_from(">BBH", frame)
        without_extended = struct.pack(">BBH", flags & ~1, counter, length - 10) + frame[4:12] + frame[22:]
        self.path.write_bytes(storage(without_extended))
        record = next(NativeReader(fibex=self.fibex).records([str(self.path)]))
        self.assertTrue(record.decoded)
        self.assertEqual(record.message_id, 1234)
        self.assertEqual(record.payload, "value= 513")

    def test_control_preserved_with_fibex(self):
        self.path.write_bytes(storage(CONTROL) + storage(message("", verbose=False)))
        before = next(NativeReader().records([str(self.path)]))
        records = list(NativeReader(fibex=self.fibex).records([str(self.path)]))
        self.assertEqual(records[0], before)
        self.assertEqual(records[0].type, "control")
        self.assertEqual(records[0].timestamp, 0.4037)
        self.assertEqual(records[0].service_id, 35)
        spec = {"require_decoded": True, "rules": [{"name": "all", "match": {}, "min_count": 2}]}
        self.assertTrue(evaluate(records, spec)["passed"])

    def test_original_verbose_unicode_preserved(self):
        self.path.write_bytes(storage(message("中文")) + storage(message("", verbose=False)))
        before = next(NativeReader().records([str(self.path)]))
        after = next(NativeReader(fibex=self.fibex).records([str(self.path)]))
        self.assertEqual(after, before)

    def test_unknown_message_remains_undecoded(self):
        frame = message("", verbose=False).replace(struct.pack("<I", 1234), struct.pack("<I", 987654))
        self.path.write_bytes(storage(frame))
        before = next(NativeReader().records([str(self.path)]))
        after = next(NativeReader(fibex=self.fibex).records([str(self.path)]))
        self.assertEqual(after, before)
        self.assertEqual(after.message_id, 987654)
        self.assertFalse(after.decoded)

    def test_binary_preservation_byte_exact(self):
        original = storage(CONTROL) + storage(message("中文"))
        self.path.write_bytes(original)
        broken = Path(self.temp.name) / "broken.dlt"
        broken.write_bytes(storage(message("wrong")) + storage(message("also wrong")))
        output = Path(self.temp.name) / "preserved.dlt"
        preserve_source_frames(self.path, broken, output)
        self.assertEqual(output.read_bytes(), original)

    def test_count_or_order_mismatch_is_error(self):
        self.path.write_bytes(storage(CONTROL))
        broken = Path(self.temp.name) / "broken.dlt"
        for data in (b"", storage(CONTROL) * 2, storage(CONTROL, counter=2)):
            with self.subTest(data=data):
                broken.write_bytes(data)
                output = Path(self.temp.name) / (str(len(data)) + ".dlt")
                with self.assertRaises(CliError):
                    preserve_source_frames(self.path, broken, output)

    def test_source_file_never_repaired_in_place(self):
        self.path.write_bytes(storage(CONTROL))
        with self.assertRaises(CliError):
            preserve_source_frames(self.path, self.path, self.path)


if __name__ == "__main__":
    unittest.main()
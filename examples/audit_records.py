"""Offline evidence audit of exported JSONL against original DLT + FIBEX.

Does not infer ECU software PASS. Sequence gaps are observed missing values,
not proof of network loss. Streams distinguish ECU/context/session/Message ID
and rawData length, avoiding false duplicates for different RAW test variants.
"""

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dlt_ai.cli import destination
from dlt_ai.native import storage_frames
from dlt_ai.records import CliError


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else digest(stream)


def digest(stream) -> str:
    result = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        result.update(chunk)
    return result.hexdigest()


def audit(logfile: Path, records: Path, fibex: Path) -> dict:
    ns = {"fx": "http://www.asam.net/xml/fbx", "ho": "http://www.asam.net/xml"}
    catalog = {}
    for frame in ET.parse(fibex).getroot().findall(".//fx:FRAME", ns):
        match = re.fullmatch(r"ID_(\d+)", frame.get("ID", ""))
        if match:
            catalog[int(match[1])] = {
                "name": frame.findtext("ho:SHORT-NAME", namespaces=ns),
                "description": frame.findtext("ho:DESC", namespaces=ns),
                "apid": frame.findtext("fx:MANUFACTURER-EXTENSION/APPLICATION_ID", namespaces=ns),
                "ctid": frame.findtext("fx:MANUFACTURER-EXTENSION/CONTEXT_ID", namespaces=ns),
                "configured_level": frame.findtext("fx:MANUFACTURER-EXTENSION/MESSAGE_INFO", namespaces=ns),
            }
    provenance = {name: {"path": str(path.resolve()), "bytes": path.stat().st_size,
                         "sha256": sha256(path)}
                  for name, path in (("dlt", logfile), ("fibex", fibex), ("jsonl", records))}
    levels, contexts = Counter(), Counter()
    messages, streams = {}, {}
    controls, errors, gaps = [], [], []
    first = last = None
    raw = storage_frames(logfile)
    total = undecoded = 0
    try:
        with records.open(encoding="utf-8") as stream:
            for index, line in enumerate(stream):
                record = json.loads(line)
                source = next(raw, None)
                if source is None or record["index"] != index or record["source_offset"] != source.offset:
                    raise CliError("JSONL count/order/offset does not match original DLT")
                if Path(record["source"]).resolve() != logfile.resolve():
                    raise CliError("JSONL source differs from original DLT")
                transport_ecu, transport_session, transport_timestamp = source.transport
                if (record["counter"] != source.frame[1] or record["session_id"] != transport_session or
                        record["timestamp"] != transport_timestamp / 10000 or
                        record["ecu"] != transport_ecu.rstrip(b"\x00").decode("ascii")):
                    raise CliError(f"JSONL transport metadata changed at index {index}")
                if (source.control != (record["type"] == "control") or
                        record["service_id"] != (source.identifier if source.control else None) or
                        record["message_id"] != (None if source.control else source.identifier)):
                    raise CliError(f"JSONL type/identifier changed at index {index}")
                total += 1
                first = first or record
                last = record
                levels[(record["type"], record["level"])] += 1
                contexts[(record["ecu"], record["apid"], record["ctid"])] += 1
                if source.control:
                    controls.append({key: record[key] for key in
                                     ("index", "source_offset", "timestamp", "service_id", "payload")})
                    continue
                undecoded += int(not record["decoded"])
                mid = record["message_id"]
                definition = catalog.get(mid, {})
                item = messages.setdefault(mid, {"message_id": mid, **definition, "count": 0,
                                                 "first_index": index, "first_payload": record["payload"][:200]})
                item["count"] += 1
                item["last_index"] = index
                if mid in catalog and (record["apid"], record["ctid"]) != (definition["apid"], definition["ctid"]):
                    raise CliError(f"Decoded context does not match FIBEX at index {index}")
                if record["level"] in {"fatal", "error"} and len(errors) < 10:
                    errors.append(record)
                sequence = re.search(r"\bseq=\s*(\d+)", record["payload"])
                if not sequence:
                    continue
                raw_value = re.search(r"\brawData=\s*((?:[0-9a-fA-F]{2}(?:\s+|$))+)", record["payload"])
                raw_length = len(raw_value[1].split()) if raw_value else None
                key = (record["ecu"], record["apid"], record["ctid"], record["session_id"], mid, raw_length)
                seq = int(sequence[1])
                stat = streams.setdefault(key, {"ecu": key[0], "apid": key[1], "ctid": key[2],
                                                "session_id": key[3], "message_id": mid,
                                                "raw_data_bytes": raw_length, "count": 0, "first_seq": seq,
                                                "last_seq": None, "first_timestamp": record["timestamp"],
                                                "gap_events": 0, "missing_seq_values": 0,
                                                "duplicate_seq_events": 0, "reverse_seq_events": 0})
                previous = stat["last_seq"]
                if previous is not None:
                    difference = (seq - previous) % 65536
                    if difference == 0:
                        stat["duplicate_seq_events"] += 1
                    elif difference > 32768:
                        stat["reverse_seq_events"] += 1
                    elif difference > 1:
                        stat["gap_events"] += 1
                        stat["missing_seq_values"] += difference - 1
                        if len(gaps) < 50:
                            gaps.append({"stream": {k: stat[k] for k in
                                                   ("ecu", "apid", "ctid", "session_id", "message_id", "raw_data_bytes")},
                                         "previous_seq": previous, "current_seq": seq,
                                         "observed_missing_values": difference - 1,
                                         "index": index, "source_offset": source.offset,
                                         "timestamp": record["timestamp"]})
                stat["last_seq"] = seq
                stat["last_timestamp"] = record["timestamp"]
                stat["count"] += 1
        if next(raw, None) is not None:
            raise CliError("JSONL omitted source messages")
    finally:
        raw.close()
    if total == 0:
        raise CliError("No records to audit")
    missing_ids = [{"message_id": mid, **catalog[mid]} for mid in sorted(set(catalog) - set(messages))]
    return {
        "schema_version": 1, "scope": "offline decode/coverage evidence; NOT ECU software acceptance",
        "provenance": provenance, "messages": total, "undecoded_application_messages": undecoded,
        "source_offset_count_order_transport_and_identifier_validation": "passed",
        "storage_time": {"first": first["time"], "last": last["time"],
                         "duration_seconds": (datetime.strptime(last["time"], "%Y/%m/%d %H:%M:%S.%f") -
                                              datetime.strptime(first["time"], "%Y/%m/%d %H:%M:%S.%f")).total_seconds(),
                         "timezone": "native local time, offset unspecified"},
        "levels": [{"type": kind, "level": level, "count": count}
                   for (kind, level), count in levels.items()],
        "contexts": [{"ecu": ecu, "apid": apid, "ctid": ctid, "count": count}
                     for (ecu, apid, ctid), count in contexts.items()],
        "fibex_defined_message_ids": len(catalog), "observed_message_ids": len(messages),
        "message_ids": sorted(messages.values(), key=lambda value: value["message_id"] or -1),
        "unobserved_fibex_definitions": missing_ids,
        "sequence_stream_partition": "ECU/APID/CTID/session_id/message_id/rawData_byte_length",
        "sequence_assumption": "seq is a uint16 counter expected to increment by one; confirm with sender",
        "sequence_streams": list(streams.values()), "sequence_gap_evidence": gaps,
        "error_evidence": errors, "control_evidence": controls,
        "interpretation_limits": [
            "FIBEX defines formats and levels, not test requirements or intended active channels",
            "ERROR test markers can be deliberate; no-error policy requires business confirmation",
            "Sequence gaps do not establish packet loss; verify source generation, filtering, buffering and capture",
            "A missing FIBEX definition in this file is lack of coverage, not proof of a failed ECU channel",
            "Unknown service IDs are preserved without guessing semantics",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logfile", type=Path, required=True)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--fibex", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = audit(args.logfile, args.records, args.fibex)
        with destination(str(args.output), list(map(str, (args.logfile, args.records, args.fibex)))) as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        print(json.dumps({"status": "audited", "output": str(args.output.resolve()),
                          "messages": report["messages"],
                          "undecoded_application_messages": report["undecoded_application_messages"]}))
        return 0
    except (CliError, OSError, ValueError, KeyError, ET.ParseError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
"""Reuse native decoding/plugins, but verify input and exported message counts."""

import re
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from .records import CliError, Record, parse_native_line


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class StorageFrame:
    offset: int
    storage: bytes
    frame: bytes

    @property
    def extended(self) -> bytes:
        flags = self.frame[0]
        position = 4 + 4 * sum(bool(flags & bit) for bit in (4, 8, 16))
        return self.frame[position:position + 10] if flags & 1 else b""

    @property
    def control(self) -> bool:
        return bool(self.extended) and (self.extended[0] & 14) >> 1 == 3

    @property
    def verbose(self) -> bool:
        return bool(self.extended) and bool(self.extended[0] & 1)

    @property
    def identifier(self) -> int | None:
        if self.verbose and not self.control:
            return None
        flags = self.frame[0]
        position = 4 + 4 * sum(bool(flags & bit) for bit in (4, 8, 16))
        position += 10 if flags & 1 else 0
        payload = self.frame[position:]
        return (int.from_bytes(payload[:4], "big" if flags & 2 else "little")
                if len(payload) >= 4 else None)

    @property
    def transport(self) -> tuple[bytes, int, int]:
        flags, position = self.frame[0], 4
        ecu = self.frame[position:position + 4] if flags & 4 else self.storage[12:16]
        position += 4 if flags & 4 else 0
        session = int.from_bytes(self.frame[position:position + 4], "big") if flags & 8 else 0
        position += 4 if flags & 8 else 0
        timestamp = int.from_bytes(self.frame[position:position + 4], "big") if flags & 16 else 0
        return ecu, session, timestamp

    @property
    def formatted_text(self) -> str | None:
        flags = self.frame[0]
        position = 4 + 4 * sum(bool(flags & bit) for bit in (4, 8, 16)) + 10
        payload = self.frame[position:]
        if not self.verbose or self.extended[1] != 1 or len(payload) < 6:
            return None
        endian = ">" if flags & 2 else "<"
        type_info, length = struct.unpack_from(endian + "IH", payload)
        if type_info != 0x8200 or len(payload) != 6 + length:
            return None
        return payload[6:].rstrip(b"\x00").decode("utf-8", errors="replace")


def fibex_identifiers(path: Path) -> set[int]:
    files = ([path] if path.is_file() else sorted(
        item for item in path.rglob("*") if item.is_file() and item.suffix.lower() in {".fibex", ".xml"}))
    identifiers: set[int] = set()
    for file in files:
        try:
            root = ET.parse(file).getroot()
        except (ET.ParseError, OSError) as exc:
            raise CliError(f"Cannot parse FIBEX catalog {file}: {exc}") from exc
        for frame in root.findall(".//{http://www.asam.net/xml/fbx}FRAME"):
            match = re.fullmatch(r"ID_(\d+)", frame.get("ID", ""))
            if match:
                identifiers.add(int(match[1]))
    if not identifiers:
        raise CliError("No DLT FRAME ID_<decimal> definitions found in FIBEX")
    return identifiers


def storage_frames(path: Path) -> Iterator[StorageFrame]:
    """Strict, bounded-memory DLTv1 storage framing check; no silent resync."""
    with path.open("rb") as stream:
        while True:
            offset = stream.tell()
            storage = stream.read(16)
            if not storage:
                break
            if len(storage) != 16 or storage[:4] != b"DLT\x01":
                raise CliError(f"Invalid DLTv1 storage header at byte {offset} in {path}; "
                               "raw streams/DLTv2 are not supported by this adapter")
            if struct.unpack_from("<I", storage, 8)[0] >= 1000000:
                raise CliError(f"Invalid storage microseconds at byte {offset} in {path}")
            header = stream.read(4)
            if len(header) != 4:
                raise CliError(f"Truncated standard header at byte {offset + 16} in {path}")
            htyp, _, length = struct.unpack(">BBH", header)
            minimum = 4 + 4 * sum(bool(htyp & flag) for flag in (4, 8, 16))
            minimum += 10 if htyp & 1 else 0
            if htyp >> 5 != 1 or length < minimum:
                raise CliError(f"Invalid DLTv1 length/version at byte {offset + 16} in {path}")
            body = stream.read(length - 4)
            if len(body) != length - 4:
                raise CliError(f"Truncated DLT message at byte {offset} in {path}")
            yield StorageFrame(offset, storage, header + body)


def validate_dlt(path: Path) -> int:
    return sum(1 for _ in storage_frames(path))


def preserve_source_frames(original: Path, decoded: Path, output: Path,
                           known_ids: set[int] | None = None) -> None:
    """-dd damages controls/unknown messages; retain their original binary frames.

    Also preserve already-verbose messages byte-for-byte. Only successfully
    decoded Non Verbose messages may replace originals, in the same order.
    """
    if output.resolve() in {original.resolve(), decoded.resolve()}:
        raise CliError("Decoded repair output must not overwrite either input")
    source_frames, decoded_frames = storage_frames(original), storage_frames(decoded)
    try:
        with output.open("xb") as stream:
            for source in source_frames:
                converted = next(decoded_frames, None)
                if converted is None:
                    raise CliError("FIBEX decoded export lost messages")
                if source.storage[:12] != converted.storage[:12] or source.frame[1] != converted.frame[1]:
                    raise CliError("FIBEX decoded export reordered or changed storage metadata")
                result = source
                fallback = (converted.formatted_text or "").strip() == f"[{source.identifier}]"
                if (not source.control and not source.verbose and converted.verbose and
                        source.identifier in (known_ids or set()) and not fallback):
                    # -dd may add optional fields with zero values; compare the
                    # actual ECU/session/timestamp, rather than header layouts.
                    if source.transport != converted.transport:
                        raise CliError("FIBEX decoding changed transport header fields")
                    if converted.control:
                        raise CliError("FIBEX decoding changed an application message into control")
                    result = StorageFrame(source.offset, source.storage, converted.frame)
                stream.write(result.storage + result.frame)
            if next(decoded_frames, None) is not None:
                raise CliError("FIBEX decoded export added messages")
    finally:
        source_frames.close()
        decoded_frames.close()


class NativeReader:
    def __init__(self, commander: str | None = None, timeout: float = 120,
                 fibex: str | None = None, native_filter: str | None = None) -> None:
        self.commander = Path(commander).resolve() if commander else ROOT / "dlt-commander.exe"
        self.timeout = timeout
        self.fibex = Path(fibex).resolve() if fibex else None
        self.native_filter = Path(native_filter).resolve() if native_filter else None
        if not self.commander.is_file():
            raise CliError(f"DLT Commander not found: {self.commander}")
        if timeout <= 0:
            raise CliError("--timeout must be positive")
        if self.fibex and (not self.fibex.exists() or "|" in str(self.fibex)):
            raise CliError("--fibex must be an existing path without '|'")
        if self.native_filter and (not self.native_filter.is_file() or
                                   self.native_filter.suffix.lower() != ".dlf"):
            raise CliError("--native-filter must be an existing .dlf file")
        self.known_ids = fibex_identifiers(self.fibex) if self.fibex else set()

    def probe(self) -> dict[str, str | int]:
        """Verify the export contract with synthetic input, not console help.

        Windows releases may print help only to a debugger, or no captured
        console output at all. Exit zero alone does not prove export works.
        This intentionally checks only verbose UTF-8 export, not FIBEX.
        """
        text = 'DLT_AI_DOCTOR 中文 UTF8 "ok"'
        encoded = text.encode("utf-8") + b"\x00"
        payload = struct.pack("<IH", 0x8200, len(encoded)) + encoded
        extra = b"ECU1" + struct.pack(">I", 12345)
        extended = struct.pack("BB4s4s", 0x41, 1, b"TEST", b"MAIN")
        frame = struct.pack(">BBH", 0x35, 7, 4 + len(extra) + len(extended) + len(payload))
        frame += extra + extended + payload
        storage = b"DLT\x01" + struct.pack("<II4s", 1700000000, 123456, b"ECU1")
        with tempfile.TemporaryDirectory(prefix="dlt-ai-doctor-") as directory:
            source = Path(directory) / "probe.dlt"
            source.write_bytes(storage + frame)
            records = list(self.records([str(source)]))
        if len(records) != 1:
            raise CliError("Native capability probe did not export exactly one message")
        record = records[0]
        if (record.payload != text or not record.decoded or record.type != "log" or
                record.level != "info" or (record.ecu, record.apid, record.ctid) != ("ECU1", "TEST", "MAIN") or
                record.counter != 7 or record.timestamp != 1.2345 or record.source_offset != 0):
            raise CliError("Native capability probe changed UTF-8 payload or DLT metadata")
        return {"status": "passed", "method": "synthetic_dltv1_utf8_export",
                "messages": 1, "scope": "verbose export only; FIBEX and ECU connectivity not tested"}

    def run(self, arguments: list[str]) -> str:
        # Capture native chatter to disk, not stdout or an unbounded memory buffer.
        with tempfile.TemporaryFile() as diagnostics:
            try:
                process = subprocess.run([str(self.commander), *arguments], cwd=ROOT,
                                         stdout=diagnostics, stderr=subprocess.STDOUT,
                                         timeout=self.timeout, check=False)
            except subprocess.TimeoutExpired as exc:
                raise CliError(f"Native export timed out after {self.timeout:g}s") from exc
            except OSError as exc:
                raise CliError(f"Cannot start DLT Commander: {exc}") from exc
            size = diagnostics.tell()
            diagnostics.seek(max(0, size - 16000))
            text = diagnostics.read().decode("utf-8", errors="replace")
            if process.returncode != 0:
                raise CliError(f"DLT Commander failed ({process.returncode}): {text[-4000:]}")
            return text

    def records(self, paths: list[str]) -> Iterator[Record]:
        # Validate every input before the first record is emitted.
        checked: list[tuple[Path, int]] = []
        for filename in paths:
            path = Path(filename).resolve()
            if not path.is_file() or path.suffix.lower() != ".dlt":
                raise CliError(f"Expected an existing .dlt file: {path}")
            checked.append((path, validate_dlt(path)))
        for path, expected in checked:
            if expected == 0:
                continue
            with tempfile.TemporaryDirectory(prefix="dlt-ai-") as directory:
                output = Path(directory) / "native.txt"
                native_input = path
                if self.fibex:
                    # Materialize decoded verbose DLT first, so "decoded" is a
                    # verified mode, not merely an assumption that a plugin ran.
                    native_input = Path(directory) / "decoded.dlt"
                    self.run(["-dd", "-b", f"Non Verbose Mode Plugin|fibex_path|{self.fibex}",
                              "-c", str(native_input), str(path)])
                    if not native_input.is_file() or validate_dlt(native_input) != expected:
                        raise CliError("FIBEX decoded export is missing or lost messages")
                    repaired = Path(directory) / "preserved.dlt"
                    preserve_source_frames(path, native_input, repaired, self.known_ids)
                    native_input = repaired
                arguments = ["-u", "-c", str(output)]
                if self.native_filter:
                    arguments.append(str(self.native_filter))
                arguments.append(str(native_input))
                diagnostics = self.run(arguments)
                if not output.is_file():
                    raise CliError(f"Native export did not create output: {diagnostics[-4000:]}")
                # Native Commander sometimes exits 0 despite an unsuccessful import.
                # Validate layout and count BEFORE exposing this file's records.
                actual = 0
                with output.open(encoding="utf-8-sig", newline="") as stream:
                    for line in stream:
                        parse_native_line(line, str(path))
                        actual += 1
                if (not self.native_filter and actual != expected) or actual > expected:
                    raise CliError(f"Native export lost messages: input={expected}, export={actual}")
                originals = storage_frames(path)
                try:
                    with output.open(encoding="utf-8-sig", newline="") as stream:
                        source_index = -1
                        previous_index = -1
                        for line in stream:
                            record = parse_native_line(line, str(path))
                            if record.index <= previous_index or record.index >= expected:
                                raise CliError("Native export has invalid/reordered source indices")
                            while source_index < record.index:
                                source = next(originals)
                                source_index += 1
                            previous_index = record.index
                            yield replace(record, source_offset=source.offset,
                                          message_id=None if source.control else source.identifier,
                                          service_id=source.identifier if source.control else None)
                finally:
                    originals.close()
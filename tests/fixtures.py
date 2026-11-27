"""Small synthetic DLTv1 messages for reproducible tests; no ECU required."""

import struct
from pathlib import Path


def message(text: str, *, level: int = 4, counter: int = 0,
            big_endian: bool = False, verbose: bool = True) -> bytes:
    endian = ">" if big_endian else "<"
    if verbose:
        encoded = text.encode("utf-8") + b"\x00"
        payload = struct.pack(endian + "IH", 0x8200, len(encoded)) + encoded
    else:
        payload = struct.pack(endian + "I", 1234) + b"\x01\x02"
    extended = struct.pack("BB4s4s", (level << 4) | int(verbose), 1,
                           b"TEST", b"MAIN")
    extra = b"ECU1" + struct.pack(">I", counter * 10000)
    header = struct.pack(">BBH", 0x35 | (2 if big_endian else 0), counter % 256,
                         4 + len(extra) + len(extended) + len(payload))
    return header + extra + extended + payload


def storage(frame: bytes, counter: int = 0) -> bytes:
    return b"DLT\x01" + struct.pack("<II4s", 1700000000 + counter, 123456, b"ECU1") + frame


def write_sample(path: Path) -> None:
    texts = ["BOOT_OK", 'temperature=42, unit="C"', "中文测试完成",
             "first line\nsecond line", "TEST_DONE"]
    with path.open("wb") as stream:
        for counter, text in enumerate(texts):
            stream.write(storage(message(text, counter=counter,
                                         big_endian=counter == 2), counter))
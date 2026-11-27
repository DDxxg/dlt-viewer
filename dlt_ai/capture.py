"""Bounded DLTv1 TCP client capture, with optional local address/port binding."""

import ipaddress
import math
import socket
import struct
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .records import CliError


def capture(host: str, port: int, output: str, duration: float = 10,
            connect_timeout: float = 5, max_messages: int = 0,
            fallback_ecu: str = "ECU1", *, on_ready: Callable[[], None] | None = None,
            stop: threading.Event | None = None, local_host: str | None = None,
            local_port: int = 0) -> dict[str, Any]:
    path = Path(output).resolve()
    if path.suffix.lower() != ".dlt":
        raise CliError("Capture output must end in .dlt")
    if (not math.isfinite(duration) or duration <= 0 or
            not math.isfinite(connect_timeout) or connect_timeout <= 0):
        raise CliError("Capture timeouts must be finite and positive")
    if not 1 <= port <= 65535 or max_messages < 0:
        raise CliError("Invalid port or message limit")
    if not isinstance(local_port, int) or isinstance(local_port, bool) or not 0 <= local_port <= 65535:
        raise CliError("--local-port must be an integer from 0 to 65535 (0=automatic)")
    if local_host is not None:
        try:
            ipaddress.ip_address(local_host)
        except ValueError as exc:
            raise CliError("--local-host must be a local IPv4/IPv6 address, not the ECU address or a hostname") from exc
    try:
        ecu = fallback_ecu.encode("ascii")
    except UnicodeEncodeError as exc:
        raise CliError("--ecu must contain 1-4 ASCII characters") from exc
    if not 1 <= len(ecu) <= 4 or b"\x00" in ecu:
        raise CliError("--ecu must contain 1-4 ASCII characters")
    ecu = ecu.ljust(4, b"\x00")
    if path.exists():
        raise CliError(f"Capture will not overwrite an existing file: {path}")
    buffer = bytearray()
    count = total_bytes = 0
    reason = "duration"
    started = time.monotonic()
    started_unix_ns = time.time_ns()
    source_address = (local_host or "", local_port) if local_host is not None or local_port else None
    try:
        connection = socket.create_connection((host, port), timeout=connect_timeout,
                                              source_address=source_address)
    except OSError as exc:
        raise CliError(f"TCP connect/bind failed: local={local_host or 'auto'}:{local_port}, "
                       f"remote={host}:{port}: {exc}") from exc
    with connection:
        actual_local = connection.getsockname()
        deadline = time.monotonic() + duration
        with path.open("xb") as stream:
            if on_ready:
                on_ready()
            while not max_messages or count < max_messages:
                if stop and stop.is_set():
                    reason = "cancelled"
                    break
                # Drain complete frames even when TCP coalesces many messages.
                if buffer[:4] == b"DLS\x01":
                    del buffer[:4]
                if len(buffer) >= 4:
                    htyp, _, length = struct.unpack_from(">BBH", buffer)
                    minimum = 4 + 4 * sum(bool(htyp & flag) for flag in (4, 8, 16))
                    minimum += 10 if htyp & 1 else 0
                    if htyp >> 5 != 1 or length < minimum:
                        raise CliError("Unsupported TCP framing/version; expected DLTv1 "
                                       "standard headers (optionally prefixed by DLS\\x01)")
                    if len(buffer) >= length:
                        frame = bytes(buffer[:length])
                        del buffer[:length]
                        now = time.time_ns()
                        seconds, nanos = divmod(now, 1000000000)
                        frame_ecu = frame[4:8] if htyp & 4 else ecu
                        storage = b"DLT\x01" + struct.pack("<II", seconds, nanos // 1000) + frame_ecu
                        stream.write(storage + frame)
                        total_bytes += len(storage) + len(frame)
                        count += 1
                        continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                connection.settimeout(min(remaining, 0.25) if stop else remaining)
                try:
                    chunk = connection.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    reason = "peer_closed"
                    if buffer:
                        raise CliError(f"TCP peer closed mid-message ({len(buffer)} trailing bytes); "
                                       f"{count} complete messages preserved in {path}")
                    break
                buffer.extend(chunk)
            if max_messages and count >= max_messages:
                reason = "message_limit"
    if count == 0:
        raise CliError(f"No complete DLT messages captured in {path}")
    finished_unix_ns = time.time_ns()
    return {"schema_version": 1, "status": "captured", "output": str(path),
            "host": host, "port": port, "local_host": actual_local[0],
            "local_port": actual_local[1], "messages": count, "bytes": total_bytes,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "started_unix_ns": started_unix_ns, "finished_unix_ns": finished_unix_ns,
            "stop_reason": reason, "discarded_tail_bytes": len(buffer),
            "storage_time_basis": "host_receive_time"}
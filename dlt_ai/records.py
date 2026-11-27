"""Stable record schema and shared filters."""

import re
from dataclasses import asdict, dataclass
from typing import Any


class CliError(Exception):
    """An input, transport or native-tool failure (exit code 2)."""


LEVELS = {"fatal": 1, "error": 2, "warn": 3, "info": 4, "debug": 5, "verbose": 6}


@dataclass(frozen=True)
class Record:
    source: str
    index: int
    time: str
    timestamp: float
    counter: int
    ecu: str
    apid: str
    ctid: str
    session_id: int
    type: str
    level: str
    mode: str
    argument_count: int
    payload: str
    source_offset: int | None = None
    message_id: int | None = None
    service_id: int | None = None

    @property
    def decoded(self) -> bool:
        return self.mode == "verbose"

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 1, **asdict(self), "decoded": self.decoded}


def parse_native_line(line: str, source: str) -> Record:
    # Native UTF-8 text has 13 fixed fields, each separated by ONE space.
    # split() without an explicit separator would lose empty ECU/APID/CTID.
    fields = line.rstrip("\r\n").split(" ", 13)
    if len(fields) == 13:
        fields.append("")
    if len(fields) != 14:
        raise CliError(f"Unsupported native text layout in {source}: {line[:160]!r}")
    try:
        return Record(source, int(fields[0]), fields[1] + " " + fields[2],
                      float(fields[3]), int(fields[4]), fields[5], fields[6],
                      fields[7], int(fields[8]), fields[9], fields[10], fields[11],
                      int(fields[12]), fields[13])
    except ValueError as exc:
        raise CliError(f"Invalid native text metadata in {source}: {line[:160]!r}") from exc


class Matcher:
    def __init__(self, *, ecu: list[str] | None = None, apid: list[str] | None = None,
                 ctid: list[str] | None = None, level: list[str] | None = None,
                 type: list[str] | None = None, contains: str | None = None,
                 regex: str | None = None, ignore_case: bool = False,
                 max_level: str | None = None) -> None:
        self.values = {"ecu": ecu, "apid": apid, "ctid": ctid,
                       "level": level, "type": type}
        self.contains = contains
        self.ignore_case = ignore_case
        self.max_level = max_level
        try:
            self.regex = re.compile(regex, re.IGNORECASE if ignore_case else 0) if regex else None
        except re.error as exc:
            raise CliError(f"Invalid regex: {exc}") from exc

    def matches(self, record: Record) -> bool:
        if any(values and getattr(record, key) not in values for key, values in self.values.items()):
            return False
        if self.max_level and (record.type != "log" or
                               LEVELS.get(record.level, 99) > LEVELS[self.max_level]):
            return False
        payload = record.payload.casefold() if self.ignore_case else record.payload
        needle = self.contains.casefold() if self.contains and self.ignore_case else self.contains
        return ((needle is None or needle in payload) and
                (self.regex is None or self.regex.search(record.payload) is not None))
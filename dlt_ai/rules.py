"""Deterministic, fail-closed assertions with bounded evidence."""

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .records import CliError, LEVELS, Matcher, Record


def nonnegative_integer(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CliError(f"{name} must be a nonnegative integer")
    return value


def rule_matcher(data: Any) -> Matcher:
    if not isinstance(data, dict):
        raise CliError("Rule match must be an object")
    allowed = {"ecu", "apid", "ctid", "level", "type", "contains", "regex",
               "ignore_case", "max_level"}
    if set(data) - allowed:
        raise CliError(f"Unknown match keys: {sorted(set(data) - allowed)}")
    kwargs = dict(data)
    for key in ("ecu", "apid", "ctid", "level", "type"):
        if key not in kwargs:
            continue
        value = kwargs[key]
        if isinstance(value, str):
            kwargs[key] = [value]
        elif not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
            raise CliError(f"match.{key} must be a string or a nonempty string array")
    for key in ("contains", "regex"):
        if key in kwargs and (not isinstance(kwargs[key], str) or not kwargs[key]):
            raise CliError(f"match.{key} must be a nonempty string")
    if "ignore_case" in kwargs and not isinstance(kwargs["ignore_case"], bool):
        raise CliError("match.ignore_case must be boolean")
    if "max_level" in kwargs and (not isinstance(kwargs["max_level"], str) or
                                   kwargs["max_level"] not in LEVELS):
        raise CliError(f"match.max_level must be one of {list(LEVELS)}")
    if any(level not in LEVELS for level in kwargs.get("level", [])):
        raise CliError(f"match.level must be one of {list(LEVELS)}")
    return Matcher(**kwargs)


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CliError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_rules(path: str) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)
    except (OSError, ValueError) as exc:
        raise CliError(f"Cannot load rules: {exc}") from exc
    if not isinstance(data, dict):
        raise CliError("Rules document must be an object")
    allowed = {"schema_version", "name", "min_messages", "require_decoded", "rules"}
    if set(data) - allowed:
        raise CliError(f"Unknown rules document keys: {sorted(set(data) - allowed)}")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise CliError("Rules schema_version must be 1")
    if not isinstance(data.get("name", "DLT test"), str):
        raise CliError("Rules name must be a string")
    nonnegative_integer(data.get("min_messages", 1), "min_messages")
    if not isinstance(data.get("require_decoded", True), bool):
        raise CliError("require_decoded must be boolean")
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise CliError("At least one assertion rule is required")
    names: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict) or set(rule) - {"name", "match", "min_count", "max_count"}:
            raise CliError("Each rule must contain only name/match/min_count/max_count")
        name = rule.get("name")
        if not isinstance(name, str) or not name or name in names:
            raise CliError("Rule names must be nonempty and unique")
        names.add(name)
        if "min_count" not in rule and "max_count" not in rule:
            raise CliError(f"Rule {name!r} requires min_count or max_count")
        minimum = nonnegative_integer(rule.get("min_count", 0), "min_count")
        maximum = rule.get("max_count")
        if "max_count" in rule:
            nonnegative_integer(maximum, "max_count")
            if maximum < minimum:
                raise CliError(f"Rule {name!r}: max_count is less than min_count")
        if "match" not in rule:
            raise CliError(f"Rule {name!r} requires match (use {{}} to match all records)")
        rule_matcher(rule["match"])
    return data


def evaluate(records: Iterable[Record], spec: dict[str, Any], evidence_limit: int = 3) -> dict[str, Any]:
    compiled = [(rule, rule_matcher(rule["match"])) for rule in spec["rules"]]
    counts = [0] * len(compiled)
    evidence: list[list[dict[str, Any]]] = [[] for _ in compiled]
    total = undecoded = 0
    for record in records:
        total += 1
        # Control messages are not application-text assertions.
        undecoded += int(record.type != "control" and not record.decoded)
        for index, (_, matcher) in enumerate(compiled):
            if matcher.matches(record):
                counts[index] += 1
                if len(evidence[index]) < evidence_limit:
                    evidence[index].append(record.to_dict())
    results = []
    for index, (rule, _) in enumerate(compiled):
        count = counts[index]
        minimum, maximum = rule.get("min_count", 0), rule.get("max_count")
        passed = count >= minimum and (maximum is None or count <= maximum)
        results.append({"name": rule["name"], "passed": passed, "count": count,
                        "min_count": minimum, "max_count": maximum, "evidence": evidence[index]})
    failures = []
    if total < spec.get("min_messages", 1):
        failures.append(f"Too few messages: {total} < {spec.get('min_messages', 1)}")
    if spec.get("require_decoded", True) and undecoded:
        failures.append(f"{undecoded} non-control messages are undecoded; supply FIBEX")
    failures += [f"Assertion failed: {result['name']}" for result in results if not result["passed"]]
    return {"schema_version": 1, "name": spec.get("name", "DLT test"),
            "status": "failed" if failures else "passed", "passed": not failures,
            "messages": total, "undecoded_messages": undecoded,
            "failures": failures, "rules": results}
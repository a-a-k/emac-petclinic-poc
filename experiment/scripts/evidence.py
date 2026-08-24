"""Parsing and exact-delta helpers shared by the EmaC evidence adapter."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


SAMPLE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})?\s+"
    r"(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?|[-+]?Inf|NaN)"
)
LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')


@dataclass(frozen=True)
class Sample:
    name: str
    labels: dict[str, str]
    value: float


def _decode_label(value: str) -> str:
    return json.loads('"' + value + '"')


def parse_prometheus(text: str) -> list[Sample]:
    samples: list[Sample] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = SAMPLE_RE.match(line)
        if not match:
            continue
        labels = {
            key: _decode_label(value)
            for key, value in LABEL_RE.findall(match.group("labels") or "")
        }
        samples.append(Sample(match.group("name"), labels, float(match.group("value"))))
    return samples


def load_snapshot(path: Path) -> list[Sample]:
    return parse_prometheus(path.read_text(encoding="utf-8"))


def _matches(sample: Sample, name_suffix: str, labels: dict[str, str]) -> bool:
    return sample.name.endswith(name_suffix) and all(sample.labels.get(k) == v for k, v in labels.items())


def total(samples: Iterable[Sample], name_suffix: str, **labels: str) -> float:
    return sum(sample.value for sample in samples if _matches(sample, name_suffix, labels))


def exact_delta(
    start: Iterable[Sample], end: Iterable[Sample], name_suffix: str, **labels: str
) -> int:
    value = total(end, name_suffix, **labels) - total(start, name_suffix, **labels)
    rounded = round(value)
    if not math.isclose(value, rounded, abs_tol=1e-9):
        raise ValueError(f"non-integral exact counter delta for {name_suffix}{labels}: {value}")
    if rounded < 0:
        raise ValueError(f"counter reset within measurement window for {name_suffix}{labels}: {value}")
    return int(rounded)


def circuitbreaker_counts(start: list[Sample], end: list[Sample], operator: str) -> dict[str, int]:
    calls_suffix = "resilience4j_circuitbreaker_calls_seconds_count"
    successful = exact_delta(start, end, calls_suffix, name=operator, kind="successful")
    failed = exact_delta(start, end, calls_suffix, name=operator, kind="failed")
    ignored = exact_delta(start, end, calls_suffix, name=operator, kind="ignored")
    not_permitted = exact_delta(
        start,
        end,
        "resilience4j_circuitbreaker_not_permitted_calls_total",
        name=operator,
    )
    return {
        "permittedSuccessful": successful,
        "permittedFailed": failed,
        "permittedIgnored": ignored,
        "notPermitted": not_permitted,
        "permitted": successful + failed + ignored,
        "decisions": successful + failed + ignored + not_permitted,
    }


def discover_metric_identity(
    samples: list[Sample], adapter: dict[str, object]
) -> dict[str, str]:
    """Recover service/instance identity from common metric tags, never filenames."""
    metrics = adapter["metrics"]
    labels = adapter["labels"]
    relevant_suffixes = tuple(str(value) for value in metrics.values())
    relevant = [
        sample for sample in samples if any(sample.name.endswith(suffix) for suffix in relevant_suffixes)
    ]
    result: dict[str, str] = {}
    for output_key, label_key in (
        ("serviceName", str(labels["serviceName"])),
        ("serviceInstanceId", str(labels["serviceInstanceId"])),
    ):
        values = {sample.labels[label_key] for sample in relevant if sample.labels.get(label_key)}
        if len(values) != 1:
            raise ValueError(
                f"expected one {label_key} on operator metrics, observed {sorted(values)}"
            )
        result[output_key] = next(iter(values))
    return result


def discover_operator_names(samples: list[Sample], adapter: dict[str, object]) -> list[str]:
    state_suffix = str(adapter["metrics"]["state"])
    operator_label = str(adapter["labels"]["operator"])
    return sorted(
        {
            sample.labels[operator_label]
            for sample in samples
            if sample.name.endswith(state_suffix) and sample.labels.get(operator_label)
        }
    )


def adapter_operator_state(
    samples: list[Sample], adapter: dict[str, object], operator: str
) -> str:
    state_suffix = str(adapter["metrics"]["state"])
    labels = adapter["labels"]
    active = [
        str(sample.labels[labels["state"]]).upper()
        for sample in samples
        if sample.name.endswith(state_suffix)
        and sample.labels.get(labels["operator"]) == operator
        and sample.labels.get(labels["state"])
        and sample.value >= 0.5
    ]
    if len(active) != 1:
        raise ValueError(f"expected one active state for discovered operator {operator}, observed {active}")
    return active[0]


def adapter_operator_counts(
    start: list[Sample], end: list[Sample], adapter: dict[str, object], operator: str
) -> dict[str, object]:
    metrics = adapter["metrics"]
    labels = adapter["labels"]
    semantics = adapter["semantics"]
    operator_label = str(labels["operator"])
    call_kind_label = str(labels["callKind"])
    kinds = [str(kind) for kind in semantics["permittedKinds"]]
    by_kind = {
        kind: exact_delta(
            start,
            end,
            str(metrics["calls"]),
            **{operator_label: operator, call_kind_label: kind},
        )
        for kind in kinds
    }
    not_permitted = exact_delta(
        start,
        end,
        str(metrics["notPermitted"]),
        **{operator_label: operator},
    )
    permitted = sum(by_kind.values())
    successful = by_kind[str(semantics["successfulKind"])]
    return {
        "permittedByKind": by_kind,
        "permittedSuccessful": successful,
        "permitted": permitted,
        "notPermitted": not_permitted,
        "decisions": permitted + not_permitted,
    }


def circuitbreaker_state(samples: list[Sample], operator: str) -> str:
    active: list[str] = []
    for sample in samples:
        if not sample.name.endswith("resilience4j_circuitbreaker_state"):
            continue
        if sample.labels.get("name") != operator or sample.value < 0.5:
            continue
        state = sample.labels.get("state")
        if state:
            active.append(state.upper())
    if len(active) != 1:
        raise ValueError(f"expected one active state for {operator}, observed {active}")
    return active[0]


def timelimiter_timeouts(start: list[Sample], end: list[Sample], operator: str) -> int:
    return exact_delta(
        start,
        end,
        "resilience4j_timelimiter_calls_total",
        name=operator,
        kind="timeout",
    )


def http_server_availability(start: list[Sample], end: list[Sample], uri_contains: str) -> dict[str, float | int | None]:
    suffix = "http_server_requests_seconds_count"
    start_map = _series_map(start, suffix)
    end_map = _series_map(end, suffix)
    total_count = 0
    success_count = 0
    matched_uris: set[str] = set()
    for key, end_value in end_map.items():
        labels = dict(key)
        uri = labels.get("uri", labels.get("http_route", ""))
        if uri_contains not in uri or uri.startswith("/actuator"):
            continue
        delta = end_value - start_map.get(key, 0.0)
        if delta < -1e-9:
            raise ValueError(f"HTTP counter reset for URI {uri}")
        count = int(round(delta))
        matched_uris.add(uri)
        total_count += count
        status = labels.get("status", "")
        outcome = labels.get("outcome", "").upper()
        if status.startswith("2") or outcome == "SUCCESS":
            success_count += count
    return {
        "successful": success_count,
        "total": total_count,
        "availability": success_count / total_count if total_count else None,
        "matchedUris": sorted(matched_uris),
    }


def _series_map(samples: Iterable[Sample], name_suffix: str) -> dict[tuple[tuple[str, str], ...], float]:
    result: dict[tuple[tuple[str, str], ...], float] = {}
    for sample in samples:
        if sample.name.endswith(name_suffix):
            result[tuple(sorted(sample.labels.items()))] = sample.value
    return result

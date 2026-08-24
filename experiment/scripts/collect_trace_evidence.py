#!/usr/bin/env python3
"""Freeze Jaeger evidence and normalize only execution/identity facts used by EmaC."""

from __future__ import annotations

import argparse
import gzip
import json
import urllib.parse
import urllib.request
from pathlib import Path


def tags_as_dict(tags: list[dict[str, object]] | None) -> dict[str, object]:
    return {str(tag.get("key")): tag.get("value") for tag in tags or []}


def contains_any(values: list[object], needles: tuple[str, ...]) -> bool:
    text = " ".join(str(value).lower() for value in values)
    return any(needle in text for needle in needles)


def normalize_trace(trace: dict[str, object]) -> dict[str, object] | None:
    processes = trace.get("processes", {})
    gateway_processes: dict[str, str] = {}
    services: set[str] = set()
    for process_id, process in processes.items():
        service_name = str(process.get("serviceName", ""))
        services.add(service_name)
        if service_name != "api-gateway":
            continue
        resource = tags_as_dict(process.get("tags"))
        instance = str(resource.get("service.instance.id", ""))
        gateway_processes[str(process_id)] = instance

    if not gateway_processes:
        return None

    journey = False
    visits_client = False
    customers_client = False
    observed_instances: set[str] = set()
    for span in trace.get("spans", []):
        process_id = str(span.get("processID", ""))
        if process_id not in gateway_processes:
            continue
        instance = gateway_processes[process_id]
        if instance:
            observed_instances.add(instance)
        tags = tags_as_dict(span.get("tags"))
        values = [span.get("operationName", ""), *tags.values()]
        if contains_any(values, ("/api/gateway/owners/", "/api/gateway/owners/{ownerid}")):
            journey = True
        span_kind = str(tags.get("span.kind", "")).lower()
        if span_kind != "client":
            continue
        if contains_any(values, ("visits-service", "visits-proxy", "/pets/visits")):
            visits_client = True
        if contains_any(values, ("customers-service", "/owners/6")):
            customers_client = True

    if not journey or len(observed_instances) != 1:
        return None
    instance = next(iter(observed_instances))
    return {
        "traceId": trace.get("traceID"),
        "instance": instance,
        "gatewayCustomersClientSpan": customers_client,
        "gatewayVisitsClientSpan": visits_client,
        "visitsServerProcess": "visits-service" in services,
    }


def query_jaeger(base_url: str, start_us: int, end_us: int, limit: int) -> dict[str, object]:
    query = urllib.parse.urlencode(
        {
            "service": "api-gateway",
            "start": start_us,
            "end": end_us,
            "limit": limit,
        }
    )
    with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/traces?{query}", timeout=60) as response:
        return json.load(response)


def collect(base_url: str, start_us: int, end_us: int, output_dir: Path, limit: int) -> dict[str, object]:
    raw = query_jaeger(base_url, start_us, end_us, limit)
    normalized = [row for trace in raw.get("data", []) if (row := normalize_trace(trace))]
    by_instance: dict[str, dict[str, int]] = {}
    for row in normalized:
        aggregate = by_instance.setdefault(
            row["instance"],
            {
                "journeyTraces": 0,
                "withGatewayCustomersClientSpan": 0,
                "withGatewayVisitsClientSpan": 0,
                "withoutGatewayVisitsClientSpan": 0,
                "withVisitsServerProcess": 0,
            },
        )
        aggregate["journeyTraces"] += 1
        aggregate["withGatewayCustomersClientSpan"] += int(row["gatewayCustomersClientSpan"])
        aggregate["withGatewayVisitsClientSpan"] += int(row["gatewayVisitsClientSpan"])
        aggregate["withoutGatewayVisitsClientSpan"] += int(not row["gatewayVisitsClientSpan"])
        aggregate["withVisitsServerProcess"] += int(row["visitsServerProcess"])

    result = {
        "schemaVersion": "emac.normalized-traces/v1",
        "query": {"startUs": start_us, "endUs": end_us, "limit": limit},
        "returnedRawTraces": len(raw.get("data", [])),
        "normalizedJourneyTraces": len(normalized),
        "byInstance": by_instance,
        "traces": normalized,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(output_dir / "traces.raw.json.gz", "wt", encoding="utf-8") as handle:
        json.dump(raw, handle, separators=(",", ":"))
    (output_dir / "traces.normalized.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jaeger", default="http://localhost:16686")
    parser.add_argument("--start-us", type=int, required=True)
    parser.add_argument("--end-us", type=int, required=True)
    parser.add_argument("--limit", type=int, default=20000)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    collect(args.jaeger, args.start_us, args.end_us, args.output_dir, args.limit)


if __name__ == "__main__":
    main()


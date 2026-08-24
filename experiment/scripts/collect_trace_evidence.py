#!/usr/bin/env python3
"""Freeze Jaeger evidence and discover a generic instance-scoped edge graph."""

from __future__ import annotations

import argparse
import gzip
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path


def tags_as_dict(tags: list[dict[str, object]] | None) -> dict[str, object]:
    return {str(tag.get("key")): tag.get("value") for tag in tags or []}


def contains_any(values: list[object], needles: tuple[str, ...]) -> bool:
    text = " ".join(str(value).lower() for value in values)
    return any(needle.lower() in text for needle in needles)


def parent_span_id(span: dict[str, object]) -> str | None:
    for reference in span.get("references", []) or []:
        if str(reference.get("refType", "")).upper() == "CHILD_OF":
            value = str(reference.get("spanID", ""))
            return value or None
    return None


def clean_host(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = urllib.parse.urlparse(text if "://" in text else f"//{text}")
    host = parsed.hostname
    return host or text.split(":", 1)[0]


def target_from_tags(tags: dict[str, object]) -> str | None:
    for key in (
        "peer.service",
        "rpc.service",
        "server.address",
        "net.peer.name",
        "network.peer.address",
        "http.host",
    ):
        if tags.get(key):
            return clean_host(tags[key])
    for key in ("url.full", "http.url"):
        if tags.get(key):
            return clean_host(tags[key])
    return None


def operation_from_span(span: dict[str, object], tags: dict[str, object]) -> str:
    for key in ("http.route", "url.path", "http.target", "rpc.method"):
        if tags.get(key):
            return str(tags[key])
    for key in ("url.full", "http.url"):
        if tags.get(key):
            parsed = urllib.parse.urlparse(str(tags[key]))
            if parsed.path:
                return parsed.path
    return str(span.get("operationName", "unknown"))


def normalize_trace(
    trace: dict[str, object], contract: dict[str, object]
) -> dict[str, object] | None:
    entrypoint = contract["entrypoint"]
    entry_service = str(entrypoint["serviceName"])
    selectors = tuple(str(value) for value in entrypoint["operationContains"])

    processes: dict[str, dict[str, str]] = {}
    for process_id, process in (trace.get("processes", {}) or {}).items():
        resource = tags_as_dict(process.get("tags"))
        processes[str(process_id)] = {
            "serviceName": str(process.get("serviceName", "")),
            "serviceInstanceId": str(resource.get("service.instance.id", "")),
        }

    spans = list(trace.get("spans", []) or [])
    by_parent: dict[str, list[dict[str, object]]] = {}
    for span in spans:
        parent = parent_span_id(span)
        if parent:
            by_parent.setdefault(parent, []).append(span)

    journey = False
    entry_instances: set[str] = set()
    for span in spans:
        process = processes.get(str(span.get("processID", "")), {})
        if process.get("serviceName") != entry_service:
            continue
        tags = tags_as_dict(span.get("tags"))
        if contains_any([span.get("operationName", ""), *tags.values()], selectors):
            journey = True
            instance = process.get("serviceInstanceId", "")
            if instance:
                entry_instances.add(instance)

    if not journey or len(entry_instances) != 1:
        return None
    entry_instance = next(iter(entry_instances))

    edges: dict[str, dict[str, object]] = {}
    for span in spans:
        process = processes.get(str(span.get("processID", "")), {})
        if process.get("serviceName") != entry_service:
            continue
        tags = tags_as_dict(span.get("tags"))
        if str(tags.get("span.kind", "")).lower() != "client":
            continue

        child_services = {
            processes.get(str(child.get("processID", "")), {}).get("serviceName", "")
            for child in by_parent.get(str(span.get("spanID", "")), [])
        }
        child_services.discard("")
        child_services.discard(entry_service)
        target = next(iter(child_services)) if len(child_services) == 1 else target_from_tags(tags)
        if not target or target == entry_service:
            continue
        edge_id = f"{entry_service}=>{target}"
        edge = edges.setdefault(
            edge_id,
            {
                "edgeId": edge_id,
                "sourceService": entry_service,
                "targetService": target,
                "operations": set(),
            },
        )
        edge["operations"].add(operation_from_span(span, tags))

    normalized_edges = []
    for edge in edges.values():
        normalized_edges.append({**edge, "operations": sorted(edge["operations"])})
    normalized_edges.sort(key=lambda row: str(row["edgeId"]))
    return {
        "traceId": trace.get("traceID"),
        "entryService": entry_service,
        "entryInstance": entry_instance,
        "edges": normalized_edges,
    }


def query_jaeger(
    base_url: str, service: str, start_us: int, end_us: int, limit: int, timeout: int
) -> tuple[dict[str, object], bytes, float]:
    query = urllib.parse.urlencode(
        {"service": service, "start": start_us, "end": end_us, "limit": limit}
    )
    started = time.monotonic()
    with urllib.request.urlopen(
        f"{base_url.rstrip('/')}/api/traces?{query}", timeout=timeout
    ) as response:
        raw_bytes = response.read()
    return json.loads(raw_bytes), raw_bytes, time.monotonic() - started


def collect(
    base_url: str,
    start_us: int,
    end_us: int,
    output_dir: Path,
    limit: int,
    contract: dict[str, object],
    timeout: int = 300,
    chunk_seconds: int = 10,
) -> dict[str, object]:
    if chunk_seconds <= 0:
        raise ValueError("chunk_seconds must be positive")

    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "traces.raw.chunks"
    raw_dir.mkdir(parents=True, exist_ok=True)

    by_instance: dict[str, dict[str, object]] = {}
    normalized_count = 0
    returned_raw_traces = 0
    total_query_seconds = 0.0
    total_normalize_seconds = 0.0
    total_write_seconds = 0.0
    total_raw_bytes = 0
    max_chunk_raw_bytes = 0
    query_chunks: list[dict[str, object]] = []
    seen_trace_ids: set[str] = set()

    chunk_us = chunk_seconds * 1_000_000
    cursor = start_us
    chunk_index = 0
    while cursor <= end_us:
        chunk_index += 1
        chunk_end = min(end_us, cursor + chunk_us - 1)
        raw, raw_bytes, query_seconds = query_jaeger(
            base_url,
            str(contract["entrypoint"]["serviceName"]),
            cursor,
            chunk_end,
            limit,
            timeout,
        )
        raw_trace_count = len(raw.get("data", []))
        returned_raw_traces += raw_trace_count
        total_query_seconds += query_seconds
        total_raw_bytes += len(raw_bytes)
        max_chunk_raw_bytes = max(max_chunk_raw_bytes, len(raw_bytes))

        write_started = time.monotonic()
        raw_name = f"{chunk_index:04d}.json.gz"
        with gzip.open(raw_dir / raw_name, "wb", compresslevel=1) as handle:
            handle.write(raw_bytes)
        total_write_seconds += time.monotonic() - write_started

        normalize_started = time.monotonic()
        chunk_normalized = 0
        for trace in raw.get("data", []):
            trace_id = str(trace.get("traceID", ""))
            if trace_id and trace_id in seen_trace_ids:
                continue
            if trace_id:
                seen_trace_ids.add(trace_id)
            row = normalize_trace(trace, contract)
            if row is None:
                continue
            chunk_normalized += 1
            normalized_count += 1
            aggregate = by_instance.setdefault(
                str(row["entryInstance"]), {"journeyTraces": 0, "edges": {}}
            )
            aggregate["journeyTraces"] += 1
            for edge in row["edges"]:
                edge_aggregate = aggregate["edges"].setdefault(
                    edge["edgeId"],
                    {
                        "edgeId": edge["edgeId"],
                        "sourceService": edge["sourceService"],
                        "targetService": edge["targetService"],
                        "executions": 0,
                        "operations": set(),
                    },
                )
                edge_aggregate["executions"] += 1
                edge_aggregate["operations"].update(edge["operations"])
        total_normalize_seconds += time.monotonic() - normalize_started
        query_chunks.append(
            {
                "index": chunk_index,
                "startUs": cursor,
                "endUs": chunk_end,
                "rawFile": f"traces.raw.chunks/{raw_name}",
                "returnedRawTraces": raw_trace_count,
                "normalizedJourneyTraces": chunk_normalized,
                "querySeconds": query_seconds,
                "rawBytes": len(raw_bytes),
            }
        )
        cursor = chunk_end + 1

    for aggregate in by_instance.values():
        aggregate["edges"] = {
            edge_id: {**edge, "operations": sorted(edge["operations"])}
            for edge_id, edge in sorted(aggregate["edges"].items())
        }
    result = {
        "schemaVersion": "emac.discovered-trace-graph/v3",
        "query": {
            "service": contract["entrypoint"]["serviceName"],
            "startUs": start_us,
            "endUs": end_us,
            "limit": limit,
            "chunkSeconds": chunk_seconds,
            "chunks": query_chunks,
        },
        "timing": {
            "querySeconds": total_query_seconds,
            "normalizeSeconds": total_normalize_seconds,
            "rawGzipWriteSeconds": total_write_seconds,
            "rawBytes": total_raw_bytes,
            "maxChunkRawBytes": max_chunk_raw_bytes,
            "chunkCount": chunk_index,
        },
        "returnedRawTraces": returned_raw_traces,
        "normalizedJourneyTraces": normalized_count,
        "byInstance": by_instance,
        "perTraceRowsRetained": False,
    }
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
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--chunk-seconds", type=int, default=10)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    contract = json.loads(args.contract.read_text(encoding="utf-8"))
    collect(
        args.jaeger,
        args.start_us,
        args.end_us,
        args.output_dir,
        args.limit,
        contract,
        args.timeout,
        args.chunk_seconds,
    )


if __name__ == "__main__":
    main()

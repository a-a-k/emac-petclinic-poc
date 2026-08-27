#!/usr/bin/env python3
"""GitHub Actions orchestrator for the paired EmaC model-discovery experiment."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

from collect_trace_evidence import collect as collect_traces
from evidence import (
    circuitbreaker_counts,
    circuitbreaker_state,
    http_server_availability,
    load_snapshot,
    timelimiter_timeouts,
)


ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = ROOT / "experiment" / "protocol.json"
CONTRACT_PATH = ROOT / "experiment" / "journey-contract.json"
ORACLE_PATH = ROOT / "experiment" / "oracle-contract.json"
ADAPTERS_PATH = ROOT / "experiment" / "operator-adapters.json"
MANUAL_COMPOSITE_PATH = ROOT / "experiment" / "manual-composite.json"
ENV_FILE = Path(
    os.environ.get("EMAC_COMPOSE_ENV", str(ROOT / "experiment" / "images.lock.env"))
).resolve()
ROUTER_DATA = "http://localhost:18080"
ROUTER_CONTROL = "http://localhost:18475"
FAULT_CONTROL = "http://localhost:18474"
JAEGER = "http://localhost:16686"
METRIC_ENDPOINTS = {
    "metric-source-01": "http://localhost:18081/actuator/prometheus",
    "metric-source-02": "http://localhost:18082/actuator/prometheus",
    "metric-source-03": "http://localhost:18083/actuator/prometheus",
    "metric-source-04": "http://localhost:18084/actuator/prometheus",
}
SLOT_METRIC_SOURCES = {"A": "metric-source-01", "B": "metric-source-02"}
SERVICE_METRIC_SOURCES = {"customers": "metric-source-03", "visits": "metric-source-04"}
HEALTH_ENDPOINTS = {
    "config": "http://localhost:18888/actuator/health",
    "discovery": "http://localhost:18761/actuator/health",
    "customers": "http://localhost:18083/actuator/health",
    "visits": "http://localhost:18084/actuator/health",
    "vets": "http://localhost:18085/actuator/health",
    "admin": "http://localhost:19090/actuator/health",
    "slot-A": "http://localhost:18081/actuator/health",
    "slot-B": "http://localhost:18082/actuator/health",
    "router": f"{ROUTER_CONTROL}/stats",
    "fault-proxy": f"{FAULT_CONTROL}/status",
    "jaeger": f"{JAEGER}/api/services",
}
CHECKPOINT_PATH: Path | None = None


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def progress(event: str, **fields: object) -> None:
    suffix = " ".join(f"{key}={value}" for key, value in fields.items())
    print(f"EMAC_PROGRESS event={event}{' ' + suffix if suffix else ''}", flush=True)
    if CHECKPOINT_PATH is not None and event != "heartbeat":
        write_json(
            CHECKPOINT_PATH,
            {
                "event": event,
                "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "fields": fields,
            },
        )


def heartbeat(stop: threading.Event) -> None:
    while not stop.wait(30):
        progress("heartbeat", utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


def request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        response = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    body = response.read()
    return response.status, dict(response.headers.items()), body


def request_json(url: str, method: str = "GET") -> dict[str, object]:
    status, _headers, body = request(url, method=method)
    if status // 100 != 2:
        raise RuntimeError(f"{method} {url} returned {status}: {body[:200]!r}")
    return json.loads(body)


def wait_http(name: str, url: str, deadline_seconds: int = 300) -> None:
    deadline = time.monotonic() + deadline_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status, _headers, body = request(url, timeout=3)
            if status // 100 == 2:
                if name not in {"config", "router", "fault-proxy", "jaeger"}:
                    payload = json.loads(body)
                    if str(payload.get("status", "")).upper() != "UP":
                        raise RuntimeError(f"health status is {payload}")
                return
        except Exception as error:
            last_error = error
        time.sleep(2)
    raise TimeoutError(f"{name} did not become ready at {url}; last error: {last_error}")


def compose(*args: str, capture: bool = False) -> str:
    command = [
        "docker",
        "compose",
        "--env-file",
        str(ENV_FILE),
        "-f",
        str(ROOT / "compose.yaml"),
        *args,
    ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
    )
    return result.stdout or ""


def wait_stack(names: tuple[str, ...] | None = None) -> None:
    for name in names or tuple(HEALTH_ENDPOINTS):
        wait_http(name, HEALTH_ENDPOINTS[name])


def opaque_instance(seed: int, slot: str) -> str:
    digest = hashlib.sha256(f"{seed}:instance:{slot}".encode("utf-8")).hexdigest()[:16]
    return f"instance-{digest}"


def configure_runtime_identity(output: Path, seed: int) -> dict[str, object]:
    rng = random.Random(seed ^ 0x5EEDC0DE)
    minority_slot = rng.choice(["A", "B"])
    identities = {slot: opaque_instance(seed, slot) for slot in ("A", "B")}
    os.environ["GATEWAY_A_INSTANCE_ID"] = identities["A"]
    os.environ["GATEWAY_B_INSTANCE_ID"] = identities["B"]
    os.environ["MINORITY_GATEWAY"] = minority_slot
    assignment = {
        "schemaVersion": "emac.hidden-runtime-assignment/v1",
        "logicalSlots": identities,
        "minoritySlot": minority_slot,
        "minorityInstanceId": identities[minority_slot],
    }
    write_json(output / "ground-truth" / "runtime-assignment.json", assignment)
    return assignment


def verify_runtime_isolation(output: Path) -> None:
    expected = {
        "config-server": 0,
        "discovery-server": 0,
        "customers-service": 8,
        "visits-service": 1,
        "vets-service": 1,
        "admin-server": 0,
        "gateway-a": 0,
        "gateway-b": 0,
    }
    records: dict[str, object] = {}
    log_dir = output / "inputs" / "startup"
    log_dir.mkdir(parents=True, exist_ok=True)
    for service, expected_count in expected.items():
        service_log = compose("logs", "--no-color", service, capture=True)
        (log_dir / f"{service}.log").write_text(service_log, encoding="utf-8")
        marker = f"EMAC_AWS_DEMO_NEUTRALIZER count={expected_count}"
        replacements = service_log.count("EMAC_AWS_DEMO_NEUTRALIZED bean=")
        if marker not in service_log or replacements != expected_count:
            raise RuntimeError(
                f"{service} runtime isolation mismatch: marker={marker!r}, replacements={replacements}"
            )
        records[service] = {"expectedNeutralized": expected_count, "observedNeutralized": replacements}
    write_json(output / "inputs" / "runtime-isolation.json", records)


def snapshot(destination: Path, phase: str) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for source_id, url in METRIC_ENDPOINTS.items():
        status, _headers, body = request(url, timeout=30)
        if status != 200:
            raise RuntimeError(f"snapshot {source_id}/{phase} returned {status}")
        (destination / f"{source_id}.{phase}.prom").write_bytes(body)


def extract_visit_ids(payload: object) -> set[int]:
    result: set[int] = set()
    if isinstance(payload, dict):
        for collection_key in ("visits", "items"):
            if isinstance(payload.get(collection_key), list):
                for visit in payload[collection_key]:
                    if isinstance(visit, dict) and isinstance(visit.get("id"), int):
                        result.add(visit["id"])
        for value in payload.values():
            result.update(extract_visit_ids(value))
    elif isinstance(payload, list):
        for value in payload:
            result.update(extract_visit_ids(value))
    return result


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def one_request(
    run_id: str,
    request_path: str,
    oracle: dict[str, object],
    inspect_semantics: bool,
    timeout: float,
    target_slot: str | None = None,
) -> dict[str, object]:
    started = time.monotonic()
    headers = {"X-Experiment-Run-Id": run_id, "Connection": "close"}
    if target_slot:
        headers["X-Experiment-Target"] = target_slot
    try:
        status, response_headers, body = request(
            f"{ROUTER_DATA}{request_path}", headers=headers, timeout=timeout
        )
        row: dict[str, object] = {
            "status": status,
            "gatewaySlot": response_headers.get("X-Experiment-Gateway", "unknown"),
            "ordinal": response_headers.get("X-Experiment-Route-Ordinal"),
            "latencySeconds": time.monotonic() - started,
        }
        if inspect_semantics:
            try:
                payload = json.loads(body)
                row["ownerMatches"] = payload.get("id") == oracle["ownerId"]
                row["visitMatches"] = oracle["expectedVisitId"] in extract_visit_ids(payload)
            except Exception:
                row["ownerMatches"] = False
                row["visitMatches"] = False
        return row
    except Exception as error:
        return {
            "status": 0,
            "gatewaySlot": "unknown",
            "error": type(error).__name__,
            "latencySeconds": time.monotonic() - started,
            **({"ownerMatches": False, "visitMatches": False} if inspect_semantics else {}),
        }


def run_window(
    destination: Path,
    run_id: str,
    requests: int,
    rate: float,
    inspect_semantics: bool,
    protocol: dict[str, object],
    oracle: dict[str, object],
    forced_targets: list[str] | None = None,
) -> dict[str, object]:
    progress("window-start", run_id=run_id, requests=requests, rate=rate)
    request_json(f"{ROUTER_CONTROL}/reset", method="POST")
    measurement = protocol["measurement"]
    workers = int(measurement["loadWorkers"])
    max_in_flight = int(measurement["maxInFlight"])
    timeout = float(measurement["requestTimeoutSeconds"])
    start_us = time.time_ns() // 1_000
    start_mono = time.monotonic()
    rows: list[dict[str, object]] = []
    completed = 0

    def consume(done: set[Future[dict[str, object]]]) -> None:
        nonlocal completed
        for future in done:
            rows.append(future.result())
            completed += 1
            if completed % 1000 == 0 or completed == requests:
                progress("window-completed", run_id=run_id, requests=completed)

    pending: set[Future[dict[str, object]]] = set()
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for ordinal in range(requests):
            target_time = start_mono + ordinal / rate
            delay = target_time - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            while len(pending) >= max_in_flight:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                consume(done)
            target_slot = forced_targets[ordinal] if forced_targets else None
            pending.add(
                executor.submit(
                    one_request,
                    run_id,
                    str(oracle["requestPath"]),
                    oracle,
                    inspect_semantics,
                    timeout,
                    target_slot,
                )
            )
            if (ordinal + 1) % 1000 == 0 or ordinal + 1 == requests:
                progress("window-scheduled", run_id=run_id, requests=ordinal + 1)
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            consume(done)

    end_us = time.time_ns() // 1_000
    route_stats = request_json(f"{ROUTER_CONTROL}/stats")
    statuses = Counter(int(row["status"]) for row in rows)
    latencies = [float(row["latencySeconds"]) for row in rows]
    duration = (end_us - start_us) / 1_000_000
    summary: dict[str, object] = {
        "schemaVersion": "emac.load-summary/v2",
        "runId": run_id,
        "requested": requests,
        "completed": len(rows),
        "startUs": start_us,
        "endUs": end_us,
        "durationSeconds": duration,
        "requestedRatePerSecond": rate,
        "achievedRatePerSecond": len(rows) / duration if duration else None,
        "loadWorkers": workers,
        "maxInFlight": max_in_flight,
        "byGatewaySlot": {"A": int(route_stats["A"]), "B": int(route_stats["B"])},
        "httpStatus": {str(key): value for key, value in sorted(statuses.items())},
        "http2xx": sum(value for key, value in statuses.items() if 200 <= key < 300),
        "transportErrors": statuses[0],
        "latencySeconds": {
            "p50": percentile(latencies, 0.50),
            "p95": percentile(latencies, 0.95),
            "p99": percentile(latencies, 0.99),
            "max": max(latencies) if latencies else None,
        },
        "responseSemanticsInspected": inspect_semantics,
        "responseBodiesRetained": False,
    }
    if inspect_semantics:
        history = sum(bool(row["ownerMatches"] and row["visitMatches"]) for row in rows)
        owner_only = sum(bool(row["ownerMatches"]) for row in rows)
        summary["oracle"] = {
            "owner-history": {"successful": history, "reliability": history / requests},
            "owner-only": {"successful": owner_only, "reliability": owner_only / requests},
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(destination.with_suffix(".ndjson.gz"), "wt", encoding="utf-8", compresslevel=1) as handle:
            for row in sorted(rows, key=lambda item: int(item.get("ordinal") or 10**12)):
                handle.write(json.dumps(row, sort_keys=True) + "\n")
    write_json(destination, summary)
    progress(
        "window-frozen",
        run_id=run_id,
        completed=len(rows),
        duration=f"{duration:.3f}",
        achieved_rate=f"{summary['achievedRatePerSecond']:.3f}",
    )
    return summary


def sanitized_load_summary(load: dict[str, object]) -> dict[str, object]:
    allowed = (
        "schemaVersion",
        "runId",
        "requested",
        "completed",
        "startUs",
        "endUs",
        "durationSeconds",
        "requestedRatePerSecond",
        "achievedRatePerSecond",
        "httpStatus",
        "http2xx",
        "transportErrors",
        "latencySeconds",
    )
    return {
        **{key: load[key] for key in allowed},
        "routingIdentityVisible": False,
        "responseSemanticsInspected": False,
        "responseBodiesAvailableToDiscovery": False,
    }


def collect_window_traces(
    evidence_dir: Path,
    load: dict[str, object],
    requests: int,
    protocol: dict[str, object],
    contract: dict[str, object],
    label: str,
) -> dict[str, object]:
    progress("trace-query-start", window=label)
    result = collect_traces(
        JAEGER,
        int(load["startUs"]) - 100_000,
        int(load["endUs"]) + 100_000,
        evidence_dir,
        limit=requests + int(protocol["measurement"]["traceQueryLimitPadding"]),
        contract=contract,
        expected_run_id=label,
        timeout=int(protocol["measurement"]["traceQueryTimeoutSeconds"]),
        chunk_seconds=int(protocol["measurement"]["traceQueryChunkSeconds"]),
    )
    progress(
        "trace-query-complete",
        window=label,
        traces=result["normalizedJourneyTraces"],
        query_seconds=f"{result['timing']['querySeconds']:.3f}",
        raw_bytes=result["timing"]["rawBytes"],
        chunks=result["timing"]["chunkCount"],
    )
    return result


def run_script(script: str, *args: str) -> float:
    started = time.monotonic()
    subprocess.run(
        [sys.executable, str(ROOT / "experiment" / "scripts" / script), *args],
        cwd=ROOT,
        check=True,
    )
    return time.monotonic() - started


def run_bootstrap(
    condition_dir: Path,
    run_id: str,
    protocol: dict[str, object],
    contract: dict[str, object],
) -> tuple[dict[str, object], Path, int]:
    bootstrap = protocol["bootstrap"]
    evidence_dir = condition_dir / "bootstrap" / "evidence"
    snapshots = evidence_dir / "snapshots"
    snapshot(snapshots, "start")
    requests = int(bootstrap["requests"])
    forced_targets = ["A" if ordinal % 2 == 0 else "B" for ordinal in range(requests)]
    full_load_path = condition_dir / "ground-truth" / "routing" / "bootstrap-load.json"
    load = run_window(
        full_load_path,
        f"{run_id}-bootstrap",
        requests,
        float(bootstrap["ratePerSecond"]),
        False,
        protocol,
        read_json(ORACLE_PATH),
        forced_targets,
    )
    write_json(evidence_dir / "load-summary.json", sanitized_load_summary(load))
    time.sleep(int(protocol["measurement"]["drainSeconds"]))
    snapshot(snapshots, "end")
    time.sleep(int(protocol["measurement"]["collectorFlushSeconds"]))
    collect_window_traces(evidence_dir, load, requests, protocol, contract, f"{run_id}-bootstrap")

    model_path = condition_dir / "model" / "bootstrap-model.json"
    timings: dict[str, float] = {}
    timings["discoverySeconds"] = run_script(
        "discover_model.py",
        "bootstrap",
        "--evidence",
        str(evidence_dir),
        "--contract",
        str(CONTRACT_PATH),
        "--adapters",
        str(ADAPTERS_PATH),
        "--output",
        str(model_path),
    )
    model = read_json(model_path)
    frozen_ns = time.time_ns()
    write_json(
        condition_dir / "model" / "bootstrap-freeze.json",
        {
            "schemaVersion": "emac.bootstrap-freeze/v2",
            "modelVersion": model["modelVersion"],
            "discoverySeconds": timings["discoverySeconds"],
            "frozenAtUnixNs": frozen_ns,
        },
    )
    progress("bootstrap-model-frozen", run_id=run_id, model_version=model["modelVersion"])
    return model, model_path, frozen_ns


def direct_slot_request(run_id: str, slot: str, protocol: dict[str, object]) -> int:
    oracle = read_json(ORACLE_PATH)
    status, _headers, _body = request(
        f"{ROUTER_DATA}{oracle['requestPath']}",
        headers={"X-Experiment-Target": slot, "X-Experiment-Run-Id": run_id},
        timeout=float(protocol["measurement"]["requestTimeoutSeconds"]),
    )
    return status


def visits_functional_check() -> dict[str, object]:
    oracle = read_json(ORACLE_PATH)
    status, _headers, body = request("http://localhost:18084/pets/visits?petId=7", timeout=8)
    try:
        visit_ids = sorted(extract_visit_ids(json.loads(body)))
    except Exception:
        visit_ids = []
    return {
        "status": status,
        "visitIds": visit_ids,
        "healthy": status == 200 and oracle["expectedVisitId"] in visit_ids,
    }


def precondition(
    condition_dir: Path,
    condition: str,
    run_id: str,
    protocol: dict[str, object],
    assignment: dict[str, object],
) -> tuple[dict[str, object], int]:
    minority_slot = str(assignment["minoritySlot"])
    manipulation_dir = condition_dir / "ground-truth" / "manipulation"
    snapshot(manipulation_dir, "before")
    manipulation_started_ns = time.time_ns()
    fault_before = request_json(f"{FAULT_CONTROL}/status")
    if condition == "treatment":
        request_json(f"{FAULT_CONTROL}/fault/on", method="POST")
    statuses = Counter(
        direct_slot_request(f"{run_id}-precondition", minority_slot, protocol)
        for _ in range(int(protocol["preconditioning"]["requests"]))
    )
    fault_during = request_json(f"{FAULT_CONTROL}/status")
    request_json(f"{FAULT_CONTROL}/fault/off", method="POST")
    time.sleep(int(protocol["measurement"]["drainSeconds"]))
    snapshot(manipulation_dir, "after")
    fault_after = request_json(f"{FAULT_CONTROL}/status")

    source = SLOT_METRIC_SOURCES[minority_slot]
    before = load_snapshot(manipulation_dir / f"{source}.before.prom")
    after = load_snapshot(manipulation_dir / f"{source}.after.prom")
    operator = str(read_json(MANUAL_COMPOSITE_PATH)["operatorName"])
    counts = circuitbreaker_counts(before, after, operator)
    state = circuitbreaker_state(after, operator)
    result = {
        "condition": condition,
        "minoritySlot": minority_slot,
        "minorityInstanceId": assignment["minorityInstanceId"],
        "outerStatuses": {str(key): value for key, value in sorted(statuses.items())},
        "minorityGateway": {"finalState": state, **counts},
        "faultProxy": {"before": fault_before, "during": fault_during, "after": fault_after},
        "visitsAfterFaultDisabled": visits_functional_check(),
    }
    write_json(condition_dir / "ground-truth" / "manipulation.json", result)
    progress("preconditioning-frozen", condition=condition, run_id=run_id, final_state=state)
    return result, manipulation_started_ns


def restart_condition_stack() -> None:
    compose("restart", "jaeger")
    wait_http("jaeger", HEALTH_ENDPOINTS["jaeger"], 120)
    compose("restart", "otel-collector")
    compose("restart", "gateway-a", "gateway-b")
    wait_stack(("slot-A", "slot-B"))
    request_json(f"{FAULT_CONTROL}/fault/off", method="POST")
    request_json(f"{ROUTER_CONTROL}/reset", method="POST")
    time.sleep(3)


def reset_gateways_after_bootstrap(condition_dir: Path) -> tuple[int, int]:
    """Discard bootstrap breaker history while preserving runtime identities."""
    started_ns = time.time_ns()
    compose("restart", "gateway-a", "gateway-b")
    wait_stack(("slot-A", "slot-B"))
    request_json(f"{FAULT_CONTROL}/fault/off", method="POST")
    request_json(f"{ROUTER_CONTROL}/reset", method="POST")
    time.sleep(3)
    completed_ns = time.time_ns()
    write_json(
        condition_dir / "ground-truth" / "post-bootstrap-reset.json",
        {
            "schemaVersion": "emac.post-bootstrap-reset/v1",
            "startedNs": started_ns,
            "completedNs": completed_ns,
            "services": ["gateway-a", "gateway-b"],
            "purpose": "discard bootstrap circuit-breaker call history before manipulation",
        },
    )
    progress("post-bootstrap-gateway-reset", completed_ns=completed_ns)
    return started_ns, completed_ns


def run_discovery_pipeline(
    condition_dir: Path,
    evidence_dir: Path,
    bootstrap_model_path: Path,
    protocol: dict[str, object],
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
    int,
]:
    model_dir = condition_dir / "model"
    delta_path = model_dir / "typed-delta.json"
    reconciliation_path = model_dir / "reconciliation.json"
    effective_path = model_dir / "effective-model.json"
    compiled_path = model_dir / "compiled-estimates.json"
    manual_path = condition_dir / "baselines" / "manual-dynamic-composite.json"
    tolerance = str(protocol["measurement"]["operatorEdgeBindingToleranceFraction"])

    timings: dict[str, float] = {}
    timings["discoverySeconds"] = run_script(
        "discover_model.py",
        "delta",
        "--base-model",
        str(bootstrap_model_path),
        "--evidence",
        str(evidence_dir),
        "--adapters",
        str(ADAPTERS_PATH),
        "--tolerance-fraction",
        tolerance,
        "--output",
        str(delta_path),
    )
    timings["reconciliationSeconds"] = run_script(
        "reconcile_model_delta.py",
        "--base-model",
        str(bootstrap_model_path),
        "--candidate-delta",
        str(delta_path),
        "--output",
        str(reconciliation_path),
    )
    timings["modelApplicationSeconds"] = run_script(
        "apply_model_delta.py",
        "--base-model",
        str(bootstrap_model_path),
        "--delta",
        str(delta_path),
        "--reconciliation",
        str(reconciliation_path),
        "--output",
        str(effective_path),
    )
    timings["compilationSeconds"] = run_script(
        "compile_journeys.py",
        "--effective-model",
        str(effective_path),
        "--contract",
        str(CONTRACT_PATH),
        "--output",
        str(compiled_path),
    )
    timings["manualBaselineSeconds"] = run_script(
        "manual_composite.py",
        "--evidence",
        str(evidence_dir),
        "--contract",
        str(CONTRACT_PATH),
        "--manual-model",
        str(MANUAL_COMPOSITE_PATH),
        "--adapters",
        str(ADAPTERS_PATH),
        "--output",
        str(manual_path),
    )
    delta = read_json(delta_path)
    reconciliation = read_json(reconciliation_path)
    effective = read_json(effective_path)
    compiled = read_json(compiled_path)
    manual = read_json(manual_path)
    timings["emacPipelineSeconds"] = sum(
        timings[key]
        for key in (
            "discoverySeconds",
            "reconciliationSeconds",
            "modelApplicationSeconds",
            "compilationSeconds",
        )
    )
    write_json(
        model_dir / "pipeline-timing.json",
        {"schemaVersion": "emac.pipeline-timing/v1", **timings},
    )
    freeze_ns = time.time_ns()
    freeze = {
        "schemaVersion": "emac.pre-outcome-freeze/v4",
        "bootstrapModelVersion": read_json(bootstrap_model_path)["modelVersion"],
        "deltaVersion": delta["deltaVersion"],
        "reconciliationVersion": reconciliation["reconciliationVersion"],
        "reconciliationStatus": reconciliation["status"],
        "effectiveModelVersion": effective["modelVersion"],
        "compilationVersion": compiled["compilationVersion"],
        "pipelineTiming": timings,
        "frozenAtUnixNs": freeze_ns,
    }
    freeze_path = model_dir / "pre-outcome-freeze.json"
    write_json(freeze_path, freeze)
    progress(
        "emac-model-frozen",
        effective_model=effective["modelVersion"],
        compilation=compiled["compilationVersion"],
    )
    return delta, effective, compiled, manual, freeze_ns


def local_slis(evidence_dir: Path, load: dict[str, object]) -> dict[str, object]:
    snapshots = evidence_dir / "snapshots"
    result: dict[str, object] = {}
    for service, uri in (("customers", "/owners/"), ("visits", "/pets/visits")):
        source = SERVICE_METRIC_SOURCES[service]
        start = load_snapshot(snapshots / f"{source}.start.prom")
        end = load_snapshot(snapshots / f"{source}.end.prom")
        result[service] = http_server_availability(start, end, uri)
    result["gateway"] = {
        "successful": int(load["http2xx"]),
        "total": int(load["completed"]),
        "availability": int(load["http2xx"]) / int(load["completed"]),
        "source": "status-only load record; response bodies discarded",
    }
    return result


def expected_routing(requests: int, minority_slot: str) -> dict[str, int]:
    minority = requests // 100
    majority_slot = next(slot for slot in ("A", "B") if slot != minority_slot)
    return {majority_slot: requests - minority, minority_slot: minority}


def condition_validity(
    condition: str,
    bootstrap: dict[str, object],
    bootstrap_frozen_ns: int,
    reset_started_ns: int,
    reset_completed_ns: int,
    manipulation_started_ns: int,
    manipulation: dict[str, object],
    evidence_load: dict[str, object],
    outcome: dict[str, object],
    delta: dict[str, object],
    effective: dict[str, object],
    compiled: dict[str, object],
    freeze_ns: int,
    outcome_started_ns: int,
    elapsed_open_seconds: float,
    assignment: dict[str, object],
    protocol: dict[str, object],
) -> dict[str, object]:
    expected_state = "OPEN" if condition == "treatment" else "CLOSED"
    gateway = manipulation["minorityGateway"]
    identities = set(assignment["logicalSlots"].values())
    discovered_identities = {
        row["serviceInstanceId"]
        for row in bootstrap["instances"]
        if row["serviceName"] == "api-gateway"
    }
    route_expected = expected_routing(
        int(evidence_load["requested"]), str(assignment["minoritySlot"])
    )
    outcome_route_expected = expected_routing(
        int(outcome["requested"]), str(assignment["minoritySlot"])
    )
    observations = delta["observedOperators"]
    selected = str(delta["selectedOperator"])
    zero_timeouts = True
    for source in SLOT_METRIC_SOURCES.values():
        start = load_snapshot(Path(evidence_load["snapshotDir"]) / f"{source}.start.prom")
        end = load_snapshot(Path(evidence_load["snapshotDir"]) / f"{source}.end.prom")
        zero_timeouts = zero_timeouts and timelimiter_timeouts(start, end, selected) == 0
    trace_rows = delta["observedTraceGraph"]["byInstance"]
    counts_by_instance = {
        row["serviceInstanceId"]: int(row["counts"]["decisions"])
        for row in observations
        if row["operatorName"] == selected
    }
    minimum_coverage = float(protocol["measurement"]["minimumTraceCoverage"])
    trace_coverage = {
        instance: int(trace_rows.get(instance, {}).get("journeyTraces", 0)) / decisions
        if decisions
        else 0.0
        for instance, decisions in counts_by_instance.items()
    }
    treatment_binding = (
        len(delta["bindings"]) == 1
        and delta["bindings"][0]["serviceInstanceId"] == assignment["minorityInstanceId"]
        and delta["bindings"][0]["affectedEdge"]["sourceService"] == "api-gateway"
        and delta["bindings"][0]["affectedEdge"]["targetService"] == "visits-service"
    )
    treatment_delta = (
        len(delta["stateChanges"]) == 1
        and delta["stateChanges"][0]["serviceInstanceId"] == assignment["minorityInstanceId"]
        and delta["stateChanges"][0]["after"] == "OPEN"
    )
    chain_valid = (
        delta["baseModelVersion"] == bootstrap["modelVersion"]
        and effective["parentModelVersion"] == bootstrap["modelVersion"]
        and effective["candidateDeltaVersion"] == delta["deltaVersion"]
        and effective["appliedDeltaVersion"] == delta["deltaVersion"]
        and effective["reconciliationStatus"] == "identified"
        and compiled["effectiveModelVersion"] == effective["modelVersion"]
        and compiled["reconciliationVersion"] == effective["reconciliationVersion"]
        and compiled["status"] == "ASSESSED"
    )
    trace_query = delta["observedTraceGraph"]["query"]
    checks = {
        "bootstrapFrozenBeforeManipulation": bootstrap_frozen_ns < manipulation_started_ns,
        "cleanGatewayResetAfterBootstrap": (
            bootstrap_frozen_ns < reset_started_ns < reset_completed_ns < manipulation_started_ns
        ),
        "manipulationState": gateway["finalState"] == expected_state,
        "preconditionDecisions": gateway["decisions"] == protocol["preconditioning"]["requests"],
        "visitsHealthyAfterFault": manipulation["visitsAfterFaultDisabled"]["healthy"],
        "opaqueIdentitiesDiscovered": discovered_identities == identities,
        "logicalSlotNamesAbsentFromModel": "gateway-A" not in json.dumps(bootstrap) and "gateway-B" not in json.dumps(bootstrap),
        "exactRouting": evidence_load["byGatewaySlot"] == route_expected,
        "exactOutcomeRouting": outcome["byGatewaySlot"] == outcome_route_expected,
        "allEvidenceRequestsCompleted": evidence_load["completed"] == evidence_load["requested"],
        "allOutcomeRequestsCompleted": outcome["completed"] == outcome["requested"],
        "breakerOpenBudget": elapsed_open_seconds < protocol["measurement"]["breakerOpenBudgetSeconds"],
        "zeroTimeLimiterTimeouts": zero_timeouts,
        "counterDenominatorMatchesEligible": delta["runtimeParameters"]["decisions"] == evidence_load["requested"],
        "traceCoverageAtLeastMinimum": all(value >= minimum_coverage for value in trace_coverage.values()),
        "traceRunIdFilterEnforced": (
            trace_query.get("runIdFilterEnforced") is True
            and trace_query.get("expectedRunId") == evidence_load["runId"]
        ),
        "modelApplicationChain": chain_valid,
        "freezePrecedesOutcome": freeze_ns < outcome_started_ns,
    }
    if condition == "treatment":
        checks.update(
            {
                "nominalTreatmentCounts": (
                    gateway["permittedFailed"]
                    == protocol["preconditioning"]["expectedTreatmentFailedCalls"]
                    and gateway["notPermitted"]
                    == protocol["preconditioning"]["expectedTreatmentNotPermittedCalls"]
                ),
                "exactStateDeltaRecovery": treatment_delta,
                "uniqueOperatorEdgeBindingRecovery": treatment_binding,
            }
        )
    else:
        checks.update(
            {
                "nominalControlCounts": (
                    gateway["permittedSuccessful"] == protocol["preconditioning"]["requests"]
                    and gateway["notPermitted"] == 0
                ),
                "noFalseStateDelta": not delta["stateChanges"],
                "noFalseOperatorEdgeBinding": not delta["bindings"],
            }
        )
    return {"valid": all(checks.values()), "checks": checks, "traceCoverage": trace_coverage}


def run_condition(
    pair_dir: Path,
    condition: str,
    pair_id: str,
    phase: str,
    protocol: dict[str, object],
    contract: dict[str, object],
    assignment: dict[str, object],
) -> dict[str, object]:
    condition_dir = pair_dir / condition
    condition_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"{pair_id}-{condition}"
    condition_start = time.monotonic()
    progress("condition-prepare", condition=condition, run_id=run_id)
    restart_condition_stack()

    bootstrap, bootstrap_path, bootstrap_frozen_ns = run_bootstrap(
        condition_dir, run_id, protocol, contract
    )
    reset_started_ns, reset_completed_ns = reset_gateways_after_bootstrap(condition_dir)
    manipulation, manipulation_started_ns = precondition(
        condition_dir, condition, run_id, protocol, assignment
    )
    post_precondition = time.monotonic()

    measurement = protocol["measurement"][phase]
    evidence_requests = int(measurement["evidenceRequests"])
    outcome_requests = int(measurement["outcomeRequests"])
    rate = float(measurement["ratePerSecond"])
    evidence_dir = condition_dir / "evidence"
    snapshots = evidence_dir / "snapshots"
    snapshot(snapshots, "start")
    full_evidence_path = condition_dir / "ground-truth" / "routing" / "evidence-load.json"
    evidence_load = run_window(
        full_evidence_path,
        f"{run_id}-evidence",
        evidence_requests,
        rate,
        False,
        protocol,
        read_json(ORACLE_PATH),
    )
    evidence_load["snapshotDir"] = str(snapshots.resolve())
    write_json(evidence_dir / "load-summary.json", sanitized_load_summary(evidence_load))
    time.sleep(int(protocol["measurement"]["drainSeconds"]))
    snapshot(snapshots, "end")
    time.sleep(int(protocol["measurement"]["collectorFlushSeconds"]))
    collect_window_traces(
        evidence_dir, evidence_load, evidence_requests, protocol, contract, f"{run_id}-evidence"
    )

    delta, effective, compiled, manual, freeze_ns = run_discovery_pipeline(
        condition_dir, evidence_dir, bootstrap_path, protocol
    )
    outcome_started_ns = time.time_ns()
    outcome = run_window(
        condition_dir / "outcome" / "oracle-summary.json",
        f"{run_id}-outcome",
        outcome_requests,
        rate,
        True,
        protocol,
        read_json(ORACLE_PATH),
    )
    elapsed_open = time.monotonic() - post_precondition

    comparison: dict[str, object] = {}
    for journey_id in contract["journeys"]:
        actual = float(outcome["oracle"][journey_id]["reliability"])
        discovered = float(compiled["estimates"][journey_id]["modelDiscoveredEstimate"])
        frozen = float(compiled["estimates"][journey_id]["frozenModelEstimate"])
        manual_estimate = float(manual["estimates"][journey_id])
        target = float(compiled["estimates"][journey_id]["target"])
        comparison[journey_id] = {
            "heldOutReliability": actual,
            "modelDiscoveredAbsoluteError": abs(discovered - actual),
            "manualDynamicAbsoluteError": abs(manual_estimate - actual),
            "frozenAbsoluteError": abs(frozen - actual),
            "modelDiscoveredTargetSideError": (discovered >= target) != (actual >= target),
            "manualDynamicTargetSideError": (manual_estimate >= target) != (actual >= target),
            "frozenTargetSideError": (frozen >= target) != (actual >= target),
        }

    slis = local_slis(evidence_dir, evidence_load)
    validity = condition_validity(
        condition,
        bootstrap,
        bootstrap_frozen_ns,
        reset_started_ns,
        reset_completed_ns,
        manipulation_started_ns,
        manipulation,
        evidence_load,
        outcome,
        delta,
        effective,
        compiled,
        freeze_ns,
        outcome_started_ns,
        elapsed_open,
        assignment,
        protocol,
    )
    write_json(condition_dir / "validity.json", validity)
    result = {
        "condition": condition,
        "runId": run_id,
        "durationSeconds": time.monotonic() - condition_start,
        "elapsedSincePreconditioningSeconds": elapsed_open,
        "validity": validity,
        "comparison": comparison,
        "discovery": {
            "selectedOperator": delta["selectedOperator"],
            "stateChanges": delta["stateChanges"],
            "operatorBindings": delta["bindings"],
            "runtimeParameters": delta["runtimeParameters"],
            "effectiveModelVersion": effective["modelVersion"],
        },
        "localAvailabilitySlis": slis,
    }
    write_json(condition_dir / "result.json", result)
    progress("condition-complete", condition=condition, run_id=run_id, valid=validity["valid"])
    return result


def paired_sli_balance(results: dict[str, dict[str, object]], tolerance_pp: float) -> dict[str, object]:
    tolerance = tolerance_pp / 100.0
    services: dict[str, object] = {}
    valid = True
    for service in ("gateway", "customers", "visits"):
        control = results["control"]["localAvailabilitySlis"][service]["availability"]
        treatment = results["treatment"]["localAvailabilitySlis"][service]["availability"]
        difference = abs(control - treatment) if control is not None and treatment is not None else None
        balanced = difference is not None and difference <= tolerance
        services[service] = {
            "control": control,
            "treatment": treatment,
            "absoluteDifference": difference,
            "tolerance": tolerance,
            "balanced": balanced,
        }
        valid = valid and balanced
    return {"valid": valid, "services": services}


def run_pair(
    root: Path,
    phase: str,
    ordinal: int,
    seed: int,
    protocol: dict[str, object],
    assignment: dict[str, object],
) -> dict[str, object]:
    pair_id = f"{phase}-pair-{ordinal:02d}"
    pair_dir = root / phase / f"pair-{ordinal:02d}"
    order = ["control", "treatment"]
    random.Random(seed).shuffle(order)
    progress("pair-start", pair_id=pair_id, order=",".join(order))
    write_json(pair_dir / "schedule.json", {"pairId": pair_id, "seed": seed, "conditionOrder": order})
    contract = read_json(CONTRACT_PATH)
    results: dict[str, dict[str, object]] = {}
    for condition in order:
        results[condition] = run_condition(
            pair_dir, condition, pair_id, phase, protocol, contract, assignment
        )
        write_json(pair_dir / "partial-pair-result.json", {"completedConditions": results})
    balance = paired_sli_balance(
        results, float(protocol["measurement"]["localAvailabilityTolerancePercentagePoints"])
    )
    valid = balance["valid"] and all(row["validity"]["valid"] for row in results.values())
    pair_result = {
        "pairId": pair_id,
        "seed": seed,
        "conditionOrder": order,
        "valid": valid,
        "localSliBalance": balance,
        "conditions": results,
    }
    write_json(pair_dir / "pair-result.json", pair_result)
    progress("pair-complete", pair_id=pair_id, valid=valid)
    return pair_result


def summarize(confirmatory: list[dict[str, object]], all_pairs: list[dict[str, object]]) -> dict[str, object]:
    valid_confirmatory = [pair for pair in confirmatory if pair["valid"]]
    treatments = [pair["conditions"]["treatment"] for pair in valid_confirmatory]
    controls = [pair["conditions"]["control"] for pair in valid_confirmatory]
    exact_treatment = sum(
        row["validity"]["checks"].get("exactStateDeltaRecovery", False)
        and row["validity"]["checks"].get("uniqueOperatorEdgeBindingRecovery", False)
        for row in treatments
    )
    false_control = sum(
        bool(row["discovery"]["stateChanges"] or row["discovery"]["operatorBindings"])
        for row in controls
    )
    treatment_check_counts = {
        name: sum(bool(row["validity"]["checks"].get(name, False)) for row in treatments)
        for name in (
            "metricsOnlyStateRecovery",
            "tracesOnlyEdgeRecovery",
            "fullFusionTypedRecovery",
            "ambiguityReplayRefusesBinding",
            "contradictionReplayRefusesBinding",
        )
    }
    control_check_counts = {
        name: sum(bool(row["validity"]["checks"].get(name, False)) for row in controls)
        for name in (
            "metricsOnlyNoFalseStateDelta",
            "tracesOnlyNoFalseSuppression",
            "fullFusionNoFalseDelta",
        )
    }
    sampling_summary = {}
    for rate in ("0.1", "0.01"):
        treatment_sampling = [
            row.get("robustness", {})
            .get("traceSampling", {})
            .get(rate, {})
            .get("discovery", {})
            for row in treatments
        ]
        control_sampling = [
            row.get("robustness", {})
            .get("traceSampling", {})
            .get(rate, {})
            .get("discovery", {})
            for row in controls
        ]
        sampling_summary[rate] = {
            "treatments": {
                "recovered": sum(row.get("status") == "recovered" for row in treatment_sampling),
                "unresolved": sum(row.get("status") == "unresolved" for row in treatment_sampling),
                "falseBindings": sum(bool(row.get("falseBinding")) for row in treatment_sampling),
                "denominator": len(treatments),
            },
            "controls": {
                "noDrift": sum(row.get("status") == "no-drift" for row in control_sampling),
                "falseBindings": sum(bool(row.get("falseBinding")) for row in control_sampling),
                "denominator": len(controls),
            },
        }
    return {
        "schemaVersion": "emac.discovery-report/v4",
        "attemptedPairs": len(all_pairs),
        "confirmatoryPairsRetained": len(valid_confirmatory),
        "invalidAttemptsRetained": len([pair for pair in all_pairs if not pair["valid"]]),
        "exactTreatmentModelRecovery": {"numerator": exact_treatment, "denominator": len(treatments)},
        "falseDiscoveryInControls": {"numerator": false_control, "denominator": len(controls)},
        "evidenceSourceAblations": {
            "treatments": {
                name: {"numerator": count, "denominator": len(treatments)}
                for name, count in treatment_check_counts.items()
            },
            "controls": {
                name: {"numerator": count, "denominator": len(controls)}
                for name, count in control_check_counts.items()
            },
        },
        "robustness": {
            "traceSampling": sampling_summary,
            "identityRedaction": {
                "globalQPreserved": sum(
                    bool(row["validity"]["checks"].get("identityRedactionPreservesGlobalQ"))
                    for row in treatments
                ),
                "bindingUnresolved": sum(
                    bool(
                        row["validity"]["checks"].get(
                            "identityRedactionLeavesBindingUnresolved"
                        )
                    )
                    for row in treatments
                ),
                "denominator": len(treatments),
            },
        },
        "ownerHistoryErrors": {
            name: [row["comparison"]["owner-history"][key] for row in treatments + controls]
            for name, key in (
                ("modelDiscovery", "modelDiscoveredAbsoluteError"),
                ("manualDynamic", "manualDynamicAbsoluteError"),
                ("frozen", "frozenAbsoluteError"),
            )
        },
        "frozenTargetSideErrorsInTreatments": sum(
            row["comparison"]["owner-history"]["frozenTargetSideError"] for row in treatments
        ),
    }


def write_markdown(path: Path, report: dict[str, object]) -> None:
    recovery = report["exactTreatmentModelRecovery"]
    false = report["falseDiscoveryInControls"]
    ablations = report["evidenceSourceAblations"]["treatments"]
    robustness = report["robustness"]["traceSampling"]
    lines = [
        "# EmaC runtime-model discovery PoC",
        "",
        f"- Valid confirmatory pairs: {report['confirmatoryPairsRetained']}",
        f"- Exact treatment model recovery: {recovery['numerator']}/{recovery['denominator']}",
        f"- False discovery in controls: {false['numerator']}/{false['denominator']}",
        f"- Frozen target-side errors in treatments: {report['frozenTargetSideErrorsInTreatments']}",
        (
            "- Metrics-only state recovery (edge unresolved): "
            f"{ablations['metricsOnlyStateRecovery']['numerator']}/"
            f"{ablations['metricsOnlyStateRecovery']['denominator']}"
        ),
        (
            "- Traces-only edge recovery (operator unresolved): "
            f"{ablations['tracesOnlyEdgeRecovery']['numerator']}/"
            f"{ablations['tracesOnlyEdgeRecovery']['denominator']}"
        ),
        (
            "- Ambiguous binding correctly refused: "
            f"{ablations['ambiguityReplayRefusesBinding']['numerator']}/"
            f"{ablations['ambiguityReplayRefusesBinding']['denominator']}"
        ),
        (
            "- Contradictory evidence correctly refused: "
            f"{ablations['contradictionReplayRefusesBinding']['numerator']}/"
            f"{ablations['contradictionReplayRefusesBinding']['denominator']}"
        ),
        (
            "- 10% trace replay (recovered/unresolved/false): "
            f"{robustness['0.1']['treatments']['recovered']}/"
            f"{robustness['0.1']['treatments']['unresolved']}/"
            f"{robustness['0.1']['treatments']['falseBindings']}"
        ),
        (
            "- 1% trace replay (recovered/unresolved/false): "
            f"{robustness['0.01']['treatments']['recovered']}/"
            f"{robustness['0.01']['treatments']['unresolved']}/"
            f"{robustness['0.01']['treatments']['falseBindings']}"
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def capture_environment(output: Path) -> None:
    inputs = output / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    for source in (
        PROTOCOL_PATH,
        CONTRACT_PATH,
        ADAPTERS_PATH,
        MANUAL_COMPOSITE_PATH,
        ENV_FILE,
        ROOT / "experiment" / "upstream.lock",
    ):
        shutil.copy2(source, inputs / source.name)
    ground_truth = output / "ground-truth"
    ground_truth.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ORACLE_PATH, ground_truth / ORACLE_PATH.name)
    (inputs / "compose.resolved.yml").write_text(compose("config", capture=True), encoding="utf-8")
    subprocess.run(
        ["docker", "ps", "--no-trunc"],
        check=True,
        text=True,
        stdout=(inputs / "docker-ps.txt").open("w", encoding="utf-8"),
    )
    inspections = {}
    for service in compose("ps", "--services", capture=True).splitlines():
        container_id = compose("ps", "-q", service, capture=True).strip()
        if container_id:
            raw = subprocess.run(
                ["docker", "inspect", container_id], check=True, text=True, capture_output=True
            ).stdout
            inspections[service] = json.loads(raw)[0]
    write_json(inputs / "container-inspect.json", inspections)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=("pilot", "confirmatory"), required=True)
    parser.add_argument("--pair-ordinal", type=int, required=True)
    parser.add_argument("--seed", type=int, default=824026)
    parser.add_argument("--allow-invalid", action="store_true")
    parser.add_argument("--defer-secondary", action="store_true")
    args = parser.parse_args()

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    global CHECKPOINT_PATH
    CHECKPOINT_PATH = output / "checkpoint.json"
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(target=heartbeat, args=(heartbeat_stop,), daemon=True)
    heartbeat_thread.start()
    protocol = read_json(PROTOCOL_PATH)
    assignment = configure_runtime_identity(output, args.seed)
    try:
        progress("stack-up-start")
        compose("up", "--no-build", "-d")
        wait_stack()
        progress("stack-ready")
        verify_runtime_isolation(output)
        progress("runtime-isolation-verified")
        capture_environment(output)
        pair = run_pair(
            output, args.phase, args.pair_ordinal, args.seed, protocol, assignment
        )
        if args.defer_secondary:
            pair["primaryValid"] = pair["valid"]
            pair["secondaryAnalysisStatus"] = "pending"
            pair["valid"] = False
            write_json(
                output
                / args.phase
                / f"pair-{args.pair_ordinal:02d}"
                / "pair-result.json",
                pair,
            )
        confirmatory = [pair] if args.phase == "confirmatory" else []
        report = summarize(confirmatory, [pair])
        report.update(
            {
                "phase": args.phase,
                "pairOrdinal": args.pair_ordinal,
                "requestedConfirmatoryPairs": 1 if args.phase == "confirmatory" else 0,
                "replacementPairsUsed": 0,
            }
        )
        write_json(output / "report.json", report)
        write_markdown(output / "report.md", report)
        progress(
            "pair-job-complete",
            phase=args.phase,
            pair_ordinal=args.pair_ordinal,
            valid=pair["valid"],
            secondary_status=pair.get("secondaryAnalysisStatus", "not-deferred"),
        )
        if not pair["valid"] and not args.allow_invalid and not args.defer_secondary:
            raise SystemExit(f"{args.phase} pair {args.pair_ordinal} failed validity checks")
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)


if __name__ == "__main__":
    main()

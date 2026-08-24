#!/usr/bin/env python3
"""GitHub Actions orchestrator for the complete paired EmaC PetClinic PoC."""

from __future__ import annotations

import argparse
import gzip
import json
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from collect_trace_evidence import collect as collect_traces
from evidence import circuitbreaker_counts, circuitbreaker_state, load_snapshot


ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = ROOT / "experiment" / "protocol.json"
MODEL_PATH = ROOT / "experiment" / "journey-model.json"
ENV_FILE = ROOT / "experiment" / "images.lock.env"
ROUTER_DATA = "http://localhost:18080"
ROUTER_CONTROL = "http://localhost:18475"
FAULT_CONTROL = "http://localhost:18474"
JAEGER = "http://localhost:16686"
PROM_ENDPOINTS = {
    "gateway-A": "http://localhost:18081/actuator/prometheus",
    "gateway-B": "http://localhost:18082/actuator/prometheus",
    "customers": "http://localhost:18083/actuator/prometheus",
    "visits": "http://localhost:18084/actuator/prometheus",
}
HEALTH_ENDPOINTS = {
    "config": "http://localhost:18888/actuator/health",
    "discovery": "http://localhost:18761/actuator/health",
    "customers": "http://localhost:18083/actuator/health",
    "visits": "http://localhost:18084/actuator/health",
    "vets": "http://localhost:18085/actuator/health",
    "admin": "http://localhost:19090/actuator/health",
    "gateway-A": "http://localhost:18081/actuator/health",
    "gateway-B": "http://localhost:18082/actuator/health",
    "router": f"{ROUTER_CONTROL}/stats",
    "fault-proxy": f"{FAULT_CONTROL}/status",
    "jaeger": f"{JAEGER}/api/services",
}


def progress(event: str, **fields: object) -> None:
    suffix = " ".join(f"{key}={value}" for key, value in fields.items())
    print(f"EMAC_PROGRESS event={event}{' ' + suffix if suffix else ''}", flush=True)


def heartbeat(stop: threading.Event) -> None:
    while not stop.wait(30):
        progress("heartbeat", utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
    selected = names or tuple(HEALTH_ENDPOINTS)
    for name in selected:
        wait_http(name, HEALTH_ENDPOINTS[name])


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
        if marker not in service_log:
            raise RuntimeError(
                f"{service} did not emit required runtime-isolation marker {marker!r}"
            )
        replacements = service_log.count("EMAC_AWS_DEMO_NEUTRALIZED bean=")
        if replacements != expected_count:
            raise RuntimeError(
                f"{service} neutralized {replacements} AWS demo beans; expected {expected_count}"
            )
        records[service] = {
            "expectedNeutralized": expected_count,
            "observedNeutralized": replacements,
            "marker": marker,
        }
    write_json(output / "inputs" / "runtime-isolation.json", records)


def snapshot(destination: Path, phase: str) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name, url in PROM_ENDPOINTS.items():
        status, _headers, body = request(url, timeout=30)
        if status != 200:
            raise RuntimeError(f"snapshot {name}/{phase} returned {status}")
        (destination / f"{name}.{phase}.prom").write_bytes(body)


def extract_visit_ids(payload: object) -> set[int]:
    result: set[int] = set()
    if isinstance(payload, dict):
        for collection_key in ("visits", "items"):
            if collection_key in payload and isinstance(payload[collection_key], list):
                for visit in payload[collection_key]:
                    if isinstance(visit, dict) and isinstance(visit.get("id"), int):
                        result.add(visit["id"])
        for value in payload.values():
            result.update(extract_visit_ids(value))
    elif isinstance(payload, list):
        for value in payload:
            result.update(extract_visit_ids(value))
    return result


def one_request(run_id: str, inspect_semantics: bool) -> dict[str, object]:
    try:
        status, headers, body = request(
            f"{ROUTER_DATA}/api/gateway/owners/6",
            headers={"X-Experiment-Run-Id": run_id, "Connection": "close"},
            timeout=8,
        )
        row: dict[str, object] = {
            "status": status,
            "gateway": headers.get("X-Experiment-Gateway", "unknown"),
            "ordinal": headers.get("X-Experiment-Route-Ordinal"),
        }
        if inspect_semantics:
            try:
                payload = json.loads(body)
                row["ownerMatches"] = payload.get("id") == 6
                row["visitMatches"] = 1 in extract_visit_ids(payload)
            except Exception:
                row["ownerMatches"] = False
                row["visitMatches"] = False
        return row
    except Exception as error:
        return {
            "status": 0,
            "gateway": "unknown",
            "error": type(error).__name__,
            **({"ownerMatches": False, "visitMatches": False} if inspect_semantics else {}),
        }


def run_window(
    destination: Path,
    run_id: str,
    requests: int,
    rate: float,
    inspect_semantics: bool,
) -> dict[str, object]:
    progress("window-start", run_id=run_id, requests=requests, rate=rate)
    request_json(f"{ROUTER_CONTROL}/reset", method="POST")
    start_us = time.time_ns() // 1_000
    start_mono = time.monotonic()
    rows: list[dict[str, object]] = []
    futures = []
    with ThreadPoolExecutor(max_workers=64) as executor:
        for ordinal in range(requests):
            target = start_mono + ordinal / rate
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            futures.append(executor.submit(one_request, run_id, inspect_semantics))
            if (ordinal + 1) % 1000 == 0:
                progress("window-scheduled", run_id=run_id, requests=ordinal + 1)
        for future in as_completed(futures):
            rows.append(future.result())
    end_us = time.time_ns() // 1_000
    route_stats = request_json(f"{ROUTER_CONTROL}/stats")
    statuses = Counter(int(row["status"]) for row in rows)
    summary: dict[str, object] = {
        "schemaVersion": "emac.load-summary/v1",
        "runId": run_id,
        "requested": requests,
        "completed": len(rows),
        "startUs": start_us,
        "endUs": end_us,
        "durationSeconds": (end_us - start_us) / 1_000_000,
        "ratePerSecond": rate,
        "byGateway": {"A": int(route_stats["A"]), "B": int(route_stats["B"])},
        "httpStatus": {str(key): value for key, value in sorted(statuses.items())},
        "http2xx": sum(value for key, value in statuses.items() if 200 <= key < 300),
        "transportErrors": statuses[0],
        "responseBodiesRetained": inspect_semantics,
    }
    if inspect_semantics:
        history = sum(bool(row["ownerMatches"] and row["visitMatches"]) for row in rows)
        owner_only = sum(bool(row["ownerMatches"]) for row in rows)
        summary["oracle"] = {
            "owner-history": {"successful": history, "reliability": history / requests},
            "owner-only": {"successful": owner_only, "reliability": owner_only / requests},
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(destination.with_suffix(".ndjson.gz"), "wt", encoding="utf-8") as handle:
            for row in sorted(rows, key=lambda item: int(item.get("ordinal") or 10**12)):
                handle.write(json.dumps(row, sort_keys=True) + "\n")
    write_json(destination, summary)
    progress("window-frozen", run_id=run_id, completed=len(rows))
    return summary


def direct_b_request(run_id: str) -> int:
    status, _headers, _body = request(
        f"{ROUTER_DATA}/api/gateway/owners/6",
        headers={"X-Experiment-Target": "B", "X-Experiment-Run-Id": run_id},
        timeout=8,
    )
    return status


def visits_functional_check() -> dict[str, object]:
    status, _headers, body = request("http://localhost:18084/pets/visits?petId=7", timeout=8)
    try:
        payload = json.loads(body)
        visit_ids = sorted(extract_visit_ids(payload))
    except Exception:
        visit_ids = []
    return {"status": status, "visitIds": visit_ids, "healthy": status == 200 and 1 in visit_ids}


def prepare_condition(condition_dir: Path, condition: str, run_id: str, protocol: dict[str, object]) -> dict[str, object]:
    progress("condition-prepare", condition=condition, run_id=run_id)
    compose("restart", "jaeger")
    wait_http("jaeger", HEALTH_ENDPOINTS["jaeger"], 120)
    compose("restart", "otel-collector")
    compose("restart", "gateway-a", "gateway-b")
    wait_stack(("gateway-A", "gateway-B"))
    request_json(f"{FAULT_CONTROL}/fault/off", method="POST")
    request_json(f"{ROUTER_CONTROL}/reset", method="POST")
    time.sleep(3)

    manipulation_dir = condition_dir / "ground-truth" / "manipulation"
    snapshot(manipulation_dir, "before")
    fault_before = request_json(f"{FAULT_CONTROL}/status")
    if condition == "treatment":
        request_json(f"{FAULT_CONTROL}/fault/on", method="POST")
    statuses = Counter(
        direct_b_request(f"{run_id}-precondition")
        for _ in range(int(protocol["preconditioning"]["requests"]))
    )
    fault_during = request_json(f"{FAULT_CONTROL}/status")
    request_json(f"{FAULT_CONTROL}/fault/off", method="POST")
    time.sleep(int(protocol["measurement"]["drainSeconds"]))
    snapshot(manipulation_dir, "after")
    fault_after = request_json(f"{FAULT_CONTROL}/status")

    before = load_snapshot(manipulation_dir / "gateway-B.before.prom")
    after = load_snapshot(manipulation_dir / "gateway-B.after.prom")
    counts = circuitbreaker_counts(before, after, "getOwnerDetails")
    state = circuitbreaker_state(after, "getOwnerDetails")
    visits_check = visits_functional_check()
    result = {
        "condition": condition,
        "outerStatuses": {str(key): value for key, value in sorted(statuses.items())},
        "gatewayB": {"finalState": state, **counts},
        "faultProxy": {"before": fault_before, "during": fault_during, "after": fault_after},
        "visitsAfterFaultDisabled": visits_check,
    }
    write_json(condition_dir / "ground-truth" / "manipulation.json", result)
    progress("preconditioning-frozen", condition=condition, run_id=run_id, final_state=state)
    return result


def run_emac(evidence_dir: Path, freeze_path: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "experiment" / "scripts" / "emac_evaluate.py"),
            "--evidence",
            str(evidence_dir),
            "--model",
            str(MODEL_PATH),
            "--output",
            str(freeze_path),
        ],
        cwd=ROOT,
        check=True,
    )


def condition_validity(
    condition: str,
    manipulation: dict[str, object],
    load: dict[str, object],
    outcome: dict[str, object],
    freeze: dict[str, object],
    elapsed_open_seconds: float,
    protocol: dict[str, object],
) -> dict[str, object]:
    gateway_b = manipulation["gatewayB"]
    expected_state = "OPEN" if condition == "treatment" else "CLOSED"
    checks = {
        "manipulationState": gateway_b["finalState"] == expected_state,
        "preconditionDecisions": gateway_b["decisions"] == protocol["preconditioning"]["requests"],
        "visitsHealthyAfterFault": manipulation["visitsAfterFaultDisabled"]["healthy"],
        "exactRouting": load["byGateway"] == {"A": 9900, "B": 100},
        "exactOutcomeRouting": outcome["byGateway"] == {"A": 9900, "B": 100},
        "allEvidenceRequestsCompleted": load["completed"] == protocol["measurement"]["evidenceRequests"],
        "allOutcomeRequestsCompleted": outcome["completed"] == protocol["measurement"]["outcomeRequests"],
        "breakerOpenBudget": elapsed_open_seconds < protocol["measurement"]["breakerOpenBudgetSeconds"],
        "zeroTimeLimiterTimeouts": all(
            row["timeLimiterTimeouts"] == 0
            for row in freeze["provenance"]["observed"]["instances"].values()
        ),
        "counterDenominatorMatchesEligible": sum(
            row["decisions"] for row in freeze["provenance"]["observed"]["instances"].values()
        )
        == load["requested"],
        "traceCoverageAtLeast99Percent": all(
            row["traceCoverage"] >= 0.99
            for row in freeze["provenance"]["observed"]["traceCorroboration"].values()
        ),
        "traceMetricSuppressionAgreement": all(
            row["withinPredeclaredOnePercentTolerance"]
            for row in freeze["provenance"]["observed"]["traceCorroboration"].values()
        ),
    }
    if condition == "treatment":
        checks["nominalTreatmentCounts"] = (
            gateway_b["permittedFailed"] == protocol["preconditioning"]["expectedTreatmentFailedCalls"]
            and gateway_b["notPermitted"] == protocol["preconditioning"]["expectedTreatmentNotPermittedCalls"]
        )
    else:
        checks["nominalControlCounts"] = (
            gateway_b["permittedSuccessful"] == protocol["preconditioning"]["requests"]
            and gateway_b["notPermitted"] == 0
        )
    return {"valid": all(checks.values()), "checks": checks}


def run_condition(
    pair_dir: Path,
    condition: str,
    pair_id: str,
    protocol: dict[str, object],
) -> dict[str, object]:
    condition_dir = pair_dir / condition
    condition_dir.mkdir(parents=True, exist_ok=True)
    run_id = f"{pair_id}-{condition}"
    condition_start = time.monotonic()
    manipulation = prepare_condition(condition_dir, condition, run_id, protocol)
    post_precondition = time.monotonic()

    evidence_dir = condition_dir / "evidence"
    snapshots = evidence_dir / "snapshots"
    snapshot(snapshots, "start")
    evidence_load = run_window(
        evidence_dir / "load-summary.json",
        f"{run_id}-evidence",
        int(protocol["measurement"]["evidenceRequests"]),
        float(protocol["measurement"]["ratePerSecond"]),
        inspect_semantics=False,
    )
    time.sleep(int(protocol["measurement"]["drainSeconds"]))
    snapshot(snapshots, "end")
    time.sleep(int(protocol["measurement"]["collectorFlushSeconds"]))
    collect_traces(
        JAEGER,
        int(evidence_load["startUs"]) - 100_000,
        int(evidence_load["endUs"]) + 100_000,
        evidence_dir,
        limit=20000,
    )
    progress("trace-evidence-frozen", condition=condition, run_id=run_id)

    freeze_path = condition_dir / "model" / "evidence-freeze.json"
    run_emac(evidence_dir, freeze_path)
    freeze_mtime_ns = freeze_path.stat().st_mtime_ns
    freeze = read_json(freeze_path)
    progress("emac-model-frozen", condition=condition, run_id=run_id)

    outcome_started_ns = time.time_ns()
    outcome = run_window(
        condition_dir / "outcome" / "oracle-summary.json",
        f"{run_id}-outcome",
        int(protocol["measurement"]["outcomeRequests"]),
        float(protocol["measurement"]["ratePerSecond"]),
        inspect_semantics=True,
    )
    elapsed_open = time.monotonic() - post_precondition
    estimates = freeze["estimates"]
    comparison: dict[str, object] = {}
    for journey_id in ("owner-history", "owner-only"):
        actual = outcome["oracle"][journey_id]["reliability"]
        emac = estimates[journey_id]["evidenceReconciledEstimate"]
        frozen = estimates[journey_id]["frozenEstimate"]
        target = estimates[journey_id]["target"]
        comparison[journey_id] = {
            "heldOutReliability": actual,
            "evidenceReconciledAbsoluteError": abs(emac - actual),
            "frozenAbsoluteError": abs(frozen - actual),
            "evidenceReconciledTargetSideError": (emac >= target) != (actual >= target),
            "frozenTargetSideError": (frozen >= target) != (actual >= target),
        }

    validity = condition_validity(
        condition, manipulation, evidence_load, outcome, freeze, elapsed_open, protocol
    )
    validity["checks"]["freezePrecedesOutcome"] = freeze_mtime_ns < outcome_started_ns
    validity["valid"] = validity["valid"] and validity["checks"]["freezePrecedesOutcome"]
    write_json(condition_dir / "validity.json", validity)
    result = {
        "condition": condition,
        "runId": run_id,
        "durationSeconds": time.monotonic() - condition_start,
        "elapsedSincePreconditioningSeconds": elapsed_open,
        "validity": validity,
        "comparison": comparison,
        "typedDelta": freeze["provenance"]["derived"]["typedDelta"],
        "localAvailabilitySlis": freeze["localAvailabilitySlis"],
    }
    write_json(condition_dir / "result.json", result)
    progress(
        "condition-complete",
        condition=condition,
        run_id=run_id,
        valid=validity["valid"],
    )
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
) -> dict[str, object]:
    pair_id = f"{phase}-pair-{ordinal:02d}"
    pair_dir = root / phase / f"pair-{ordinal:02d}"
    order = ["control", "treatment"]
    random.Random(seed).shuffle(order)
    progress("pair-start", pair_id=pair_id, order=",".join(order))
    write_json(pair_dir / "schedule.json", {"pairId": pair_id, "seed": seed, "conditionOrder": order})
    results: dict[str, dict[str, object]] = {}
    for condition in order:
        results[condition] = run_condition(pair_dir, condition, pair_id, protocol)
    balance = paired_sli_balance(
        results,
        float(protocol["measurement"]["localAvailabilityTolerancePercentagePoints"]),
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
    exact_treatment_delta = sum(
        len(row["typedDelta"]) == 1
        and row["typedDelta"][0]["path"].endswith("runtimeState[gateway-B]")
        and row["typedDelta"][0]["after"] == "OPEN"
        for row in treatments
    )
    false_control_delta = sum(bool(row["typedDelta"]) for row in controls)
    return {
        "schemaVersion": "emac.poc-report/v1",
        "attemptedPairs": len(all_pairs),
        "confirmatoryPairsRetained": len(valid_confirmatory),
        "invalidAttemptsRetained": len([pair for pair in all_pairs if not pair["valid"]]),
        "exactTreatmentDeltaRecovery": {
            "numerator": exact_treatment_delta,
            "denominator": len(treatments),
        },
        "falseDeltaInControls": {"numerator": false_control_delta, "denominator": len(controls)},
        "ownerHistoryErrors": {
            "evidenceReconciled": [
                row["comparison"]["owner-history"]["evidenceReconciledAbsoluteError"]
                for row in treatments + controls
            ],
            "frozen": [
                row["comparison"]["owner-history"]["frozenAbsoluteError"]
                for row in treatments + controls
            ],
        },
        "staticTargetSideErrorsInTreatments": sum(
            row["comparison"]["owner-history"]["frozenTargetSideError"] for row in treatments
        ),
        "semanticControl": [
            {
                "condition": row["condition"],
                "ownerHistoryEstimate": row["comparison"]["owner-history"],
                "ownerOnlyEstimate": row["comparison"]["owner-only"],
            }
            for row in treatments + controls
        ],
    }


def write_markdown(path: Path, report: dict[str, object]) -> None:
    delta = report["exactTreatmentDeltaRecovery"]
    false = report["falseDeltaInControls"]
    lines = [
        "# EmaC PetClinic mechanism-feasibility PoC",
        "",
        f"- Valid confirmatory pairs: {report['confirmatoryPairsRetained']}",
        f"- Exact treatment delta recovery: {delta['numerator']}/{delta['denominator']}",
        f"- False delta in controls: {false['numerator']}/{false['denominator']}",
        f"- Static target-side errors in treatments: {report['staticTargetSideErrorsInTreatments']}",
        "",
        "The JSON report and per-condition directories contain raw boundary snapshots,",
        "compressed traces, the pre-outcome model freeze, hidden manipulation records,",
        "held-out oracle outcomes, validity checks, pinned inputs, and Compose logs.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def capture_environment(output: Path) -> None:
    inputs = output / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    for source in (PROTOCOL_PATH, MODEL_PATH, ENV_FILE, ROOT / "experiment" / "upstream.lock"):
        shutil.copy2(source, inputs / source.name)
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
    parser.add_argument("--pilot-pairs", type=int, default=1)
    parser.add_argument("--confirmatory-pairs", type=int, default=5)
    parser.add_argument("--max-replacement-pairs", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20270824)
    parser.add_argument("--stack-already-up", action="store_true")
    args = parser.parse_args()

    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(target=heartbeat, args=(heartbeat_stop,), daemon=True)
    heartbeat_thread.start()

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    protocol = read_json(PROTOCOL_PATH)
    try:
        if not args.stack_already_up:
            progress("stack-up-start")
            compose("up", "--build", "-d")
        wait_stack()
        progress("stack-ready")
        verify_runtime_isolation(output)
        progress("runtime-isolation-verified")
        capture_environment(output)

        all_pairs: list[dict[str, object]] = []
        for ordinal in range(1, args.pilot_pairs + 1):
            pair = run_pair(output, "pilot", ordinal, args.seed + ordinal, protocol)
            all_pairs.append(pair)

        confirmatory: list[dict[str, object]] = []
        ordinal = 0
        invalid = 0
        while len([pair for pair in confirmatory if pair["valid"]]) < args.confirmatory_pairs:
            ordinal += 1
            if ordinal > args.confirmatory_pairs + args.max_replacement_pairs:
                break
            pair = run_pair(output, "confirmatory", ordinal, args.seed + 10_000 + ordinal, protocol)
            confirmatory.append(pair)
            all_pairs.append(pair)
            if not pair["valid"]:
                invalid += 1

        report = summarize(confirmatory, all_pairs)
        report["requestedConfirmatoryPairs"] = args.confirmatory_pairs
        report["replacementPairsUsed"] = invalid
        write_json(output / "report.json", report)
        write_markdown(output / "report.md", report)
        progress(
            "protocol-complete",
            valid_confirmatory=report["confirmatoryPairsRetained"],
            requested=args.confirmatory_pairs,
        )
        if report["confirmatoryPairsRetained"] < args.confirmatory_pairs:
            raise SystemExit("confirmatory run did not produce the predeclared number of valid pairs")
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)


if __name__ == "__main__":
    main()

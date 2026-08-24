#!/usr/bin/env python3
"""Evidence-only EmaC adapter: metrics identify state; traces corroborate suppression."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from evidence import (
    circuitbreaker_counts,
    circuitbreaker_state,
    http_server_availability,
    load_snapshot,
    timelimiter_timeouts,
)


INSTANCES = ("gateway-A", "gateway-B")


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def target_side(value: float, target: float) -> str:
    return "above-or-equal" if value >= target else "below"


def evaluate(evidence_dir: Path, model_path: Path) -> dict[str, object]:
    model = read_json(model_path)
    load_summary = read_json(evidence_dir / "load-summary.json")
    trace_summary = read_json(evidence_dir / "traces.normalized.json")
    operator = model["declared"]["operator"]["id"]

    by_instance: dict[str, dict[str, object]] = {}
    for instance in INSTANCES:
        start = load_snapshot(evidence_dir / "snapshots" / f"{instance}.start.prom")
        end = load_snapshot(evidence_dir / "snapshots" / f"{instance}.end.prom")
        counts = circuitbreaker_counts(start, end, operator)
        state = circuitbreaker_state(end, operator)
        timeouts = timelimiter_timeouts(start, end, operator)
        eligible = int(load_summary["byGateway"][instance[-1]])
        by_instance[instance] = {
            "eligible": eligible,
            "runtimeState": state,
            "timeLimiterTimeouts": timeouts,
            **counts,
        }

    eligible = sum(int(row["eligible"]) for row in by_instance.values())
    permitted = sum(int(row["permitted"]) for row in by_instance.values())
    permitted_successful = sum(int(row["permittedSuccessful"]) for row in by_instance.values())
    decisions = sum(int(row["decisions"]) for row in by_instance.values())
    not_permitted = sum(int(row["notPermitted"]) for row in by_instance.values())

    if not eligible or not decisions or not permitted:
        raise ValueError(
            f"undefined estimate: eligible={eligible}, decisions={decisions}, permitted={permitted}"
        )
    a_prefix = decisions / eligible
    q = permitted / decisions
    a_visits = permitted_successful / permitted
    arithmetic_check = permitted_successful / eligible

    journeys: dict[str, dict[str, object]] = {}
    for journey_id, declaration in model["declared"]["journeys"].items():
        a_fallback = 1.0 if declaration["fallbackSatisfies"] else 0.0
        reconciled = a_prefix * (q * a_visits + (1.0 - q * a_visits) * a_fallback)
        frozen = a_prefix * (a_visits + (1.0 - a_visits) * a_fallback)
        journeys[journey_id] = {
            "fallbackSatisfies": declaration["fallbackSatisfies"],
            "target": declaration["target"],
            "evidenceReconciledEstimate": reconciled,
            "frozenEstimate": frozen,
            "reconciledTargetSide": target_side(reconciled, declaration["target"]),
            "frozenTargetSide": target_side(frozen, declaration["target"]),
        }

    deltas: list[dict[str, object]] = []
    derived_impacts: list[dict[str, object]] = []
    for instance, row in by_instance.items():
        before = model["initialRuntimeState"][instance]
        after = row["runtimeState"]
        if after != before:
            deltas.append(
                {
                    "path": f"operator[{operator}].runtimeState[{instance}]",
                    "before": before,
                    "after": after,
                    "evidence": "resilience4j_circuitbreaker_state",
                }
            )
            derived_impacts.append(
                {
                    "instance": instance,
                    "affectedDeclaredEdge": model["declared"]["operator"]["primaryEdge"],
                    "activatedDeclaredFallback": model["declared"]["operator"]["fallback"],
                }
            )

    trace_by_instance = trace_summary["byInstance"]
    corroboration: dict[str, object] = {}
    for instance, row in by_instance.items():
        trace_row = trace_by_instance.get(instance, {})
        missing_visits = int(trace_row.get("withoutGatewayVisitsClientSpan", 0))
        trace_total = int(trace_row.get("journeyTraces", 0))
        metric_suppressed = int(row["notPermitted"])
        tolerance = max(1, round(int(row["eligible"]) * 0.01))
        corroboration[instance] = {
            "journeyTraces": trace_total,
            "traceCoverage": trace_total / int(row["eligible"]) if row["eligible"] else 0.0,
            "withoutGatewayVisitsClientSpan": missing_visits,
            "metricNotPermitted": metric_suppressed,
            "absoluteDifference": abs(missing_visits - metric_suppressed),
            "withinPredeclaredOnePercentTolerance": abs(missing_visits - metric_suppressed) <= tolerance,
        }

    service_sli: dict[str, object] = {}
    for service, uri in (
        ("customers", "/owners/"),
        ("visits", "/pets/visits"),
    ):
        start = load_snapshot(evidence_dir / "snapshots" / f"{service}.start.prom")
        end = load_snapshot(evidence_dir / "snapshots" / f"{service}.end.prom")
        service_sli[service] = http_server_availability(start, end, uri)
    service_sli["gateway"] = {
        "successful": int(load_summary["http2xx"]),
        "total": int(load_summary["completed"]),
        "availability": int(load_summary["http2xx"]) / int(load_summary["completed"]),
        "source": "status-only load record; response bodies are discarded",
    }

    version_material = {
        "sourceModel": model,
        "typedDelta": deltas,
        "q": q,
        "aPrefix": a_prefix,
        "aVisits": a_visits,
    }
    model_version = hashlib.sha256(
        json.dumps(version_material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    return {
        "schemaVersion": "emac.evidence-freeze/v1",
        "modelVersion": model_version,
        "provenance": {
            "declared": {
                "primaryEdge": model["declared"]["operator"]["primaryEdge"],
                "fallback": model["declared"]["operator"]["fallback"],
                "fallbackSemantics": {
                    key: value["fallbackSatisfies"]
                    for key, value in model["declared"]["journeys"].items()
                },
            },
            "observed": {
                "instances": by_instance,
                "traceCorroboration": corroboration,
            },
            "derived": {
                "typedDelta": deltas,
                "impactsOfTypedDelta": derived_impacts,
                "A_P": a_prefix,
                "q": q,
                "A_V": a_visits,
                "notPermitted": not_permitted,
            },
        },
        "estimates": journeys,
        "localAvailabilitySlis": service_sli,
        "arithmeticIdentity": {
            "A_P_times_q_times_A_V": a_prefix * q * a_visits,
            "permittedSuccessfulOverEligible": arithmetic_check,
            "absoluteDifference": abs(a_prefix * q * a_visits - arithmetic_check),
        },
        "evidencePolicy": {
            "responseBodyRead": False,
            "faultManifestRead": False,
            "outcomeRead": False,
            "stateIdentification": "metrics",
            "edgeSuppressionCorroboration": "traces",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.evidence, args.model)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

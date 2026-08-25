#!/usr/bin/env python3
"""Evaluate source-isolated contributions to runtime model discovery."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from discover_model import (
    aggregate_operator,
    load_adapters,
    metric_observations,
    select_journey_operator,
    trace_graph,
)


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def stable_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def metrics_only(
    base_operator_model: dict[str, object],
    metric_evidence_dir: Path,
    adapters_path: Path,
    tolerance_fraction: float,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Recover operator identity, state, and counts without reading trace evidence."""
    observations = metric_observations(metric_evidence_dir, load_adapters(adapters_path))
    eligible = int(read_json(metric_evidence_dir / "load-summary.json")["completed"])
    selected, selection_audit = select_journey_operator(
        observations, eligible, tolerance_fraction
    )
    counts = aggregate_operator(observations, selected)
    base_by_key = {
        (row["adapterId"], row["operatorName"], row["serviceInstanceId"]): row
        for row in base_operator_model["operators"]
    }
    state_changes = []
    for current in observations:
        key = (current["adapterId"], current["operatorName"], current["serviceInstanceId"])
        previous = base_by_key.get(key)
        if not previous or previous["runtimeState"] == current["runtimeState"]:
            continue
        state_changes.append(
            {
                "operatorType": current["operatorType"],
                "operatorName": current["operatorName"],
                "serviceName": current["serviceName"],
                "serviceInstanceId": current["serviceInstanceId"],
                "before": previous["runtimeState"],
                "after": current["runtimeState"],
            }
        )
    return (
        {
            "selectedOperator": selected,
            "stateChanges": state_changes,
            "runtimeParameters": {
                "eligible": eligible,
                "decisions": counts["decisions"],
                "permitted": counts["permitted"],
                "notPermitted": counts["notPermitted"],
                "q": counts["permitted"] / counts["decisions"],
            },
            "edgeBinding": {
                "status": "unresolved",
                "reason": "trace evidence withheld by ablation",
            },
            "operatorSelectionAudit": selection_audit,
            "inputPolicy": {
                "metricSnapshotsRead": True,
                "traceGraphRead": False,
                "journeyOutcomeRead": False,
            },
        },
        observations,
    )


def traces_only(
    base_interaction_model: dict[str, object],
    trace_evidence_dir: Path,
    tolerance_fraction: float,
) -> tuple[dict[str, object], dict[str, object]]:
    """Find disappeared interactions without reading operator metrics."""
    current_graph = trace_graph(trace_evidence_dir)
    current_edges = {row["edgeId"]: row for row in current_graph["interactions"]}
    candidates: list[dict[str, object]] = []
    for base_edge in base_interaction_model["interactions"]:
        edge_id = str(base_edge["edgeId"])
        for instance_id, baseline in base_edge.get("byInstance", {}).items():
            if float(baseline.get("executionRate") or 0.0) < 0.95:
                continue
            trace_total = int(
                current_graph["byInstance"].get(instance_id, {}).get("journeyTraces", 0)
            )
            tolerance = max(1, round(trace_total * tolerance_fraction))
            current = current_edges.get(edge_id, {}).get("byInstance", {}).get(instance_id, {})
            executions = int(current.get("executions", 0))
            missing = trace_total - executions
            if missing <= tolerance:
                continue
            candidates.append(
                {
                    "serviceInstanceId": instance_id,
                    "edgeId": edge_id,
                    "sourceService": base_edge["sourceService"],
                    "targetService": base_edge["targetService"],
                    "baselineExecutionRate": baseline["executionRate"],
                    "currentJourneyTraces": trace_total,
                    "currentExecutions": executions,
                    "missingExecutions": missing,
                    "tolerance": tolerance,
                }
            )
    if len(candidates) == 1:
        status = "identified"
        affected_edge: dict[str, object] | None = candidates[0]
    elif not candidates:
        status = "no-drift"
        affected_edge = None
    else:
        status = "ambiguous"
        affected_edge = None
    return (
        {
            "suppression": {
                "status": status,
                "affectedEdge": affected_edge,
                "candidates": candidates,
            },
            "operator": {
                "status": "unresolved",
                "reason": "operator metrics withheld by ablation",
            },
            "inputPolicy": {
                "metricSnapshotsRead": False,
                "traceGraphRead": True,
                "journeyOutcomeRead": False,
            },
        },
        current_graph,
    )


def evaluate(
    metric_base_path: Path,
    metric_evidence_dir: Path,
    trace_base_path: Path,
    trace_evidence_dir: Path,
    adapters_path: Path,
    full_delta_path: Path,
    tolerance_fraction: float,
) -> dict[str, object]:
    base_operator_model = read_json(metric_base_path)
    base_interaction_model = read_json(trace_base_path)
    if base_operator_model["modelVersion"] != base_interaction_model["modelVersion"]:
        raise ValueError("source-isolated bootstrap views do not share one model version")
    source_isolation = {
        "metricsOnlyTraceGraphAbsent": not (
            metric_evidence_dir / "traces.normalized.json"
        ).exists(),
        "metricsOnlyBaseContainsOnlyOperators": set(base_operator_model)
        == {"schemaVersion", "modelVersion", "operators"},
        "tracesOnlyMetricSnapshotsAbsent": not (
            trace_evidence_dir / "snapshots"
        ).exists(),
        "tracesOnlyBaseContainsOnlyInteractions": set(base_interaction_model)
        == {"schemaVersion", "modelVersion", "interactions"},
    }
    source_isolation["verified"] = all(source_isolation.values())
    if not source_isolation["verified"]:
        raise ValueError(f"ablation inputs are not source-isolated: {source_isolation}")
    full_delta = read_json(full_delta_path)
    metric_result, observations = metrics_only(
        base_operator_model, metric_evidence_dir, adapters_path, tolerance_fraction
    )
    trace_result, current_graph = traces_only(
        base_interaction_model, trace_evidence_dir, tolerance_fraction
    )
    full_result = {
        "status": (
            "typed-delta"
            if full_delta["stateChanges"] and full_delta["bindings"]
            else "no-drift"
            if not full_delta["stateChanges"] and not full_delta["bindings"]
            else "incomplete-delta"
        ),
        "selectedOperator": full_delta["selectedOperator"],
        "stateChanges": full_delta["stateChanges"],
        "bindings": full_delta["bindings"],
        "q": full_delta["runtimeParameters"]["q"],
        "inputPolicy": {
            "metricSnapshotsRead": True,
            "traceGraphRead": True,
            "journeyOutcomeRead": False,
        },
    }
    material = {
        "baseModelVersion": base_operator_model["modelVersion"],
        "fullDeltaVersion": full_delta["deltaVersion"],
        "metricsOnly": metric_result,
        "tracesOnly": trace_result,
        "fullFusion": full_result,
        "sourceIsolation": source_isolation,
    }
    return {
        "schemaVersion": "emac.evidence-source-ablations/v1",
        "reportVersion": stable_hash(material),
        **material,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metric-base", type=Path, required=True)
    parser.add_argument("--metric-evidence", type=Path, required=True)
    parser.add_argument("--trace-base", type=Path, required=True)
    parser.add_argument("--trace-evidence", type=Path, required=True)
    parser.add_argument("--adapters", type=Path, required=True)
    parser.add_argument("--full-delta", type=Path, required=True)
    parser.add_argument("--tolerance-fraction", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(
        args.metric_base,
        args.metric_evidence,
        args.trace_base,
        args.trace_evidence,
        args.adapters,
        args.full_delta,
        args.tolerance_fraction,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

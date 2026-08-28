#!/usr/bin/env python3
"""Discover bootstrap interaction models and runtime operator/edge deltas."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from artifact_integrity import (
    evidence_references,
    seal_artifact,
    validate_adapter_catalog,
    validate_bootstrap_model,
    validate_contract,
)
from evidence import (
    adapter_operator_counts,
    adapter_operator_state,
    discover_metric_identity,
    discover_operator_names,
    load_snapshot,
)


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_adapter_catalog(path: Path) -> dict[str, object]:
    payload = read_json(path)
    validate_adapter_catalog(payload)
    return payload


def load_adapters(path: Path) -> list[dict[str, object]]:
    return load_adapter_catalog(path)["adapters"]


def metric_observations(
    evidence_dir: Path, adapters: list[dict[str, object]]
) -> list[dict[str, object]]:
    snapshots = evidence_dir / "snapshots"
    observations: list[dict[str, object]] = []
    for end_path in sorted(snapshots.glob("*.end.prom")):
        source_id = end_path.name[: -len(".end.prom")]
        start_path = snapshots / f"{source_id}.start.prom"
        if not start_path.exists():
            raise ValueError(f"missing start snapshot for metric source {source_id}")
        start = load_snapshot(start_path)
        end = load_snapshot(end_path)
        for adapter in adapters:
            operator_names = discover_operator_names(end, adapter)
            if not operator_names:
                continue
            identity = discover_metric_identity(end, adapter)
            for operator_name in operator_names:
                observations.append(
                    {
                        "adapterId": adapter["id"],
                        "operatorType": adapter["operatorType"],
                        "operatorName": operator_name,
                        **identity,
                        "runtimeState": adapter_operator_state(end, adapter, operator_name),
                        "counts": adapter_operator_counts(start, end, adapter, operator_name),
                        "metricSource": source_id,
                    }
                )
    if not observations:
        raise ValueError("no supported runtime operators discovered in metric snapshots")
    return observations


def interactions_from_by_instance(
    by_instance: dict[str, dict[str, object]],
) -> list[dict[str, object]]:
    interactions: dict[str, dict[str, object]] = {}
    for instance_id, instance in by_instance.items():
        trace_count = int(instance["journeyTraces"])
        for edge_id, edge in instance.get("edges", {}).items():
            row = interactions.setdefault(
                edge_id,
                {
                    "edgeId": edge_id,
                    "sourceService": edge["sourceService"],
                    "targetService": edge["targetService"],
                    "executions": 0,
                    "journeyTraces": 0,
                    "operations": set(),
                    "byInstance": {},
                },
            )
            executions = int(edge["executions"])
            row["executions"] += executions
            row["journeyTraces"] += trace_count
            row["operations"].update(edge.get("operations", []))
            row["byInstance"][instance_id] = {
                "executions": executions,
                "journeyTraces": trace_count,
                "executionRate": executions / trace_count if trace_count else None,
            }

    # Include zero executions for instances where an edge disappeared.
    for row in interactions.values():
        for instance_id, instance in by_instance.items():
            if instance_id in row["byInstance"]:
                continue
            trace_count = int(instance["journeyTraces"])
            row["journeyTraces"] += trace_count
            row["byInstance"][instance_id] = {
                "executions": 0,
                "journeyTraces": trace_count,
                "executionRate": 0.0 if trace_count else None,
            }

    normalized = []
    for edge_id, row in sorted(interactions.items()):
        normalized.append(
            {
                **row,
                "operations": sorted(row["operations"]),
                "executionRate": (
                    row["executions"] / row["journeyTraces"] if row["journeyTraces"] else None
                ),
                "byInstance": dict(sorted(row["byInstance"].items())),
            }
        )
    return normalized


def trace_graph(evidence_dir: Path) -> dict[str, object]:
    traces = read_json(evidence_dir / "traces.normalized.json")
    by_instance = traces.get("byInstance", {})
    return {
        "returnedRawTraces": traces["returnedRawTraces"],
        "normalizedJourneyTraces": traces["normalizedJourneyTraces"],
        "byInstance": by_instance,
        "interactions": interactions_from_by_instance(by_instance),
        "query": traces.get("query", {}),
        "timing": traces.get("timing", {}),
    }


def discover_bootstrap(
    evidence_dir: Path, contract_path: Path, adapters_path: Path
) -> dict[str, object]:
    contract = read_json(contract_path)
    validate_contract(contract)
    catalog = load_adapter_catalog(adapters_path)
    adapters = catalog["adapters"]
    operators = metric_observations(evidence_dir, adapters)
    graph = trace_graph(evidence_dir)
    instances = sorted(
        {
            (row["serviceName"], row["serviceInstanceId"])
            for row in operators
        }
        | {
            (str(contract["entrypoint"]["serviceName"]), instance_id)
            for instance_id in graph["byInstance"]
        }
    )
    artifact = {
        "schemaVersion": "emac.discovered-interaction-model/v3",
        "discoveryMode": "bootstrap-runtime-evidence",
        "contractId": contract["contractId"],
        "contractVersion": contract["contractVersion"],
        "catalogVersion": catalog["catalogVersion"],
        "instances": [
            {"serviceName": service, "serviceInstanceId": instance}
            for service, instance in instances
        ],
        "interactions": graph["interactions"],
        "operators": operators,
        "evidenceRefs": evidence_references(evidence_dir),
        "evidenceSummary": {
            "normalizedJourneyTraces": graph["normalizedJourneyTraces"],
            "traceTiming": graph["timing"],
        },
    }
    result = seal_artifact(artifact, "modelVersion")
    validate_bootstrap_model(result)
    return result


def aggregate_operator(
    observations: list[dict[str, object]], operator_name: str
) -> dict[str, int]:
    selected = [row for row in observations if row["operatorName"] == operator_name]
    return {
        key: sum(int(row["counts"][key]) for row in selected)
        for key in ("permittedSuccessful", "permitted", "notPermitted", "decisions")
    }


def select_journey_operator(
    observations: list[dict[str, object]], eligible: int, tolerance_fraction: float
) -> tuple[str, list[dict[str, object]]]:
    names = sorted({str(row["operatorName"]) for row in observations})
    candidates = []
    tolerance = max(1, round(eligible * tolerance_fraction))
    for name in names:
        counts = aggregate_operator(observations, name)
        difference = abs(counts["decisions"] - eligible)
        candidates.append(
            {
                "operatorName": name,
                "decisions": counts["decisions"],
                "eligible": eligible,
                "absoluteDifference": difference,
                "withinTolerance": difference <= tolerance,
            }
        )
    matches = [row for row in candidates if row["withinTolerance"]]
    if len(matches) != 1:
        raise ValueError(f"journey operator is not uniquely identifiable: {candidates}")
    return str(matches[0]["operatorName"]), candidates


def infer_bindings(
    base_model: dict[str, object],
    current_graph: dict[str, object],
    observations: list[dict[str, object]],
    selected_operator: str,
    tolerance_fraction: float,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    base_edges = {row["edgeId"]: row for row in base_model["interactions"]}
    current_edges = {row["edgeId"]: row for row in current_graph["interactions"]}
    bindings: list[dict[str, object]] = []
    audits: list[dict[str, object]] = []

    for observation in observations:
        if observation["operatorName"] != selected_operator:
            continue
        rejected = int(observation["counts"]["notPermitted"])
        if rejected == 0:
            continue
        instance_id = str(observation["serviceInstanceId"])
        trace_total = int(
            current_graph["byInstance"].get(instance_id, {}).get("journeyTraces", 0)
        )
        tolerance = max(1, round(trace_total * tolerance_fraction))
        candidates = []
        for edge_id, base_edge in base_edges.items():
            baseline = base_edge.get("byInstance", {}).get(instance_id)
            if not baseline or float(baseline.get("executionRate") or 0.0) < 0.95:
                continue
            current = current_edges.get(edge_id, {}).get("byInstance", {}).get(instance_id, {})
            executions = int(current.get("executions", 0))
            missing = trace_total - executions
            difference = abs(missing - rejected)
            candidates.append(
                {
                    "edgeId": edge_id,
                    "sourceService": base_edge["sourceService"],
                    "targetService": base_edge["targetService"],
                    "baselineExecutionRate": baseline["executionRate"],
                    "currentTraceCount": trace_total,
                    "currentExecutions": executions,
                    "missingExecutions": missing,
                    "metricNotPermitted": rejected,
                    "absoluteDifference": difference,
                    "withinTolerance": difference <= tolerance,
                }
            )
        matches = [candidate for candidate in candidates if candidate["withinTolerance"]]
        audits.append(
            {
                "operatorName": selected_operator,
                "serviceInstanceId": instance_id,
                "tolerance": tolerance,
                "candidates": candidates,
                "unique": len(matches) == 1,
            }
        )
        if len(matches) == 1:
            bindings.append(
                {
                    "operatorName": selected_operator,
                    "operatorType": observation["operatorType"],
                    "serviceName": observation["serviceName"],
                    "serviceInstanceId": instance_id,
                    "affectedEdge": {
                        **{
                            key: matches[0][key]
                            for key in ("edgeId", "sourceService", "targetService")
                        },
                        "operations": sorted(
                            str(value)
                            for value in base_edges[matches[0]["edgeId"]].get(
                                "operations", []
                            )
                        ),
                    },
                    "evidence": {
                        "metricNotPermitted": rejected,
                        "missingTraceExecutions": matches[0]["missingExecutions"],
                        "absoluteDifference": matches[0]["absoluteDifference"],
                    },
                }
            )
    return bindings, audits


def discover_delta(
    base_model_path: Path,
    evidence_dir: Path,
    adapters_path: Path,
    tolerance_fraction: float,
) -> dict[str, object]:
    base_model = read_json(base_model_path)
    validate_bootstrap_model(base_model)
    catalog = load_adapter_catalog(adapters_path)
    if catalog["catalogVersion"] != base_model["catalogVersion"]:
        raise ValueError("adapter catalog differs from the bootstrap model lineage")
    adapters = catalog["adapters"]
    observations = metric_observations(evidence_dir, adapters)
    graph = trace_graph(evidence_dir)
    load = read_json(evidence_dir / "load-summary.json")
    eligible = int(load["completed"])
    selected_operator, selection_audit = select_journey_operator(
        observations, eligible, tolerance_fraction
    )
    counts = aggregate_operator(observations, selected_operator)
    if not counts["decisions"] or not counts["permitted"]:
        raise ValueError(f"undefined runtime parameters: {counts}")

    bindings, binding_audit = infer_bindings(
        base_model, graph, observations, selected_operator, tolerance_fraction
    )
    base_by_key = {
        (row["adapterId"], row["operatorName"], row["serviceInstanceId"]): row
        for row in base_model["operators"]
    }
    state_changes = []
    for current in observations:
        key = (current["adapterId"], current["operatorName"], current["serviceInstanceId"])
        previous = base_by_key.get(key)
        if not previous or previous["runtimeState"] == current["runtimeState"]:
            continue
        state_changes.append(
            {
                "kind": "operator-state",
                "path": (
                    f"operators[{current['operatorName']}].instances"
                    f"[{current['serviceInstanceId']}].runtimeState"
                ),
                "adapterId": current["adapterId"],
                "operatorName": current["operatorName"],
                "serviceName": current["serviceName"],
                "serviceInstanceId": current["serviceInstanceId"],
                "before": previous["runtimeState"],
                "after": current["runtimeState"],
                "evidence": "runtime operator state metric",
            }
        )

    runtime_parameters = {
        "eligible": eligible,
        "decisions": counts["decisions"],
        "permitted": counts["permitted"],
        "permittedSuccessful": counts["permittedSuccessful"],
        "notPermitted": counts["notPermitted"],
        "A_P": counts["decisions"] / eligible,
        "q": counts["permitted"] / counts["decisions"],
        "A_V": counts["permittedSuccessful"] / counts["permitted"],
    }
    artifact = {
        "schemaVersion": "emac.candidate-model-delta/v3",
        "baseModelVersion": base_model["modelVersion"],
        "catalogVersion": catalog["catalogVersion"],
        "selectedOperator": selected_operator,
        "stateChanges": state_changes,
        "bindings": bindings,
        "runtimeParameters": runtime_parameters,
        "observedOperators": observations,
        "observedTraceGraph": graph,
        "evidenceRefs": evidence_references(evidence_dir),
        "discoveryAudit": {
            "operatorSelection": selection_audit,
            "operatorEdgeBindings": binding_audit,
        },
        "evidencePolicy": {
            "responseBodyRead": False,
            "faultManifestRead": False,
            "outcomeRead": False,
        }
    }
    return seal_artifact(artifact, "deltaVersion")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    bootstrap_parser = subparsers.add_parser("bootstrap")
    bootstrap_parser.add_argument("--evidence", type=Path, required=True)
    bootstrap_parser.add_argument("--contract", type=Path, required=True)
    bootstrap_parser.add_argument("--adapters", type=Path, required=True)
    bootstrap_parser.add_argument("--output", type=Path, required=True)

    delta_parser = subparsers.add_parser("delta")
    delta_parser.add_argument("--base-model", type=Path, required=True)
    delta_parser.add_argument("--evidence", type=Path, required=True)
    delta_parser.add_argument("--adapters", type=Path, required=True)
    delta_parser.add_argument("--tolerance-fraction", type=float, required=True)
    delta_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "bootstrap":
        result = discover_bootstrap(args.evidence, args.contract, args.adapters)
    else:
        result = discover_delta(
            args.base_model,
            args.evidence,
            args.adapters,
            args.tolerance_fraction,
        )
    write_json(args.output, result)


if __name__ == "__main__":
    main()

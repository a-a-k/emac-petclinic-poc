#!/usr/bin/env python3
"""Counterfactual negative cases for operator-to-edge binding safety."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path

from discover_model import infer_bindings, trace_graph


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def stable_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ambiguity_replay(
    base_model: dict[str, object],
    current_graph: dict[str, object],
    observations: list[dict[str, object]],
    selected_operator: str,
    tolerance_fraction: float,
) -> dict[str, object]:
    rejected = [
        row
        for row in observations
        if row["operatorName"] == selected_operator and int(row["counts"]["notPermitted"]) > 0
    ]
    if not rejected:
        return {
            "status": "not-applicable",
            "reason": "no rejected operator decisions in this condition",
        }
    if len(rejected) != 1:
        raise ValueError(f"ambiguity replay requires one rejected instance, observed {len(rejected)}")
    observation = rejected[0]
    instance_id = str(observation["serviceInstanceId"])
    rejected_count = int(observation["counts"]["notPermitted"])
    stable_edges = sorted(
        (
            edge
            for edge in base_model["interactions"]
            if float(
                edge.get("byInstance", {}).get(instance_id, {}).get("executionRate") or 0.0
            )
            >= 0.95
        ),
        key=lambda row: str(row["edgeId"]),
    )
    if len(stable_edges) < 2:
        raise ValueError("ambiguity replay requires at least two stable bootstrap edges")
    replay_graph = copy.deepcopy(current_graph)
    replay_edges = {row["edgeId"]: row for row in replay_graph["interactions"]}
    trace_total = int(replay_graph["byInstance"][instance_id]["journeyTraces"])
    synthetic_executions = trace_total - rejected_count
    modified_edge_ids = []
    for base_edge in stable_edges[:2]:
        edge_id = str(base_edge["edgeId"])
        row = replay_edges.get(edge_id)
        if row is None:
            row = {
                "edgeId": edge_id,
                "sourceService": base_edge["sourceService"],
                "targetService": base_edge["targetService"],
                "byInstance": {},
            }
            replay_graph["interactions"].append(row)
            replay_edges[edge_id] = row
        row["byInstance"][instance_id] = {
            "executions": synthetic_executions,
            "journeyTraces": trace_total,
            "executionRate": synthetic_executions / trace_total if trace_total else None,
        }
        modified_edge_ids.append(edge_id)
    bindings, audits = infer_bindings(
        base_model, replay_graph, observations, selected_operator, tolerance_fraction
    )
    audit = next(row for row in audits if row["serviceInstanceId"] == instance_id)
    matching = [row["edgeId"] for row in audit["candidates"] if row["withinTolerance"]]
    refused = not bindings and len(matching) >= 2 and not audit["unique"]
    return {
        "status": "binding-refused" if refused else "unexpected-binding",
        "serviceInstanceId": instance_id,
        "operatorName": selected_operator,
        "metricNotPermitted": rejected_count,
        "syntheticallyAmbiguousEdges": modified_edge_ids,
        "matchingEdgeCandidates": matching,
        "emittedBindings": bindings,
        "audit": audit,
    }


def contradiction_replay(
    base_model: dict[str, object],
    current_graph: dict[str, object],
    full_delta: dict[str, object],
    tolerance_fraction: float,
) -> dict[str, object]:
    rejected = [
        row
        for row in full_delta["observedOperators"]
        if row["operatorName"] == full_delta["selectedOperator"]
        and int(row["counts"]["notPermitted"]) > 0
    ]
    if not rejected:
        return {
            "status": "not-applicable",
            "reason": "no rejected operator decisions in this condition",
        }
    if len(rejected) != 1 or len(full_delta["bindings"]) != 1:
        raise ValueError("contradiction replay requires one rejected instance and one real binding")

    observation = rejected[0]
    instance_id = str(observation["serviceInstanceId"])
    rejected_count = int(observation["counts"]["notPermitted"])
    affected_edge_id = str(full_delta["bindings"][0]["affectedEdge"]["edgeId"])
    replay_graph = copy.deepcopy(current_graph)
    replay_edge = next(
        row for row in replay_graph["interactions"] if row["edgeId"] == affected_edge_id
    )
    trace_total = int(replay_graph["byInstance"][instance_id]["journeyTraces"])
    conflicting_missing = max(0, rejected_count // 2)
    replay_edge["byInstance"][instance_id] = {
        "executions": trace_total - conflicting_missing,
        "journeyTraces": trace_total,
        "executionRate": (
            (trace_total - conflicting_missing) / trace_total if trace_total else None
        ),
    }
    bindings, audits = infer_bindings(
        base_model,
        replay_graph,
        full_delta["observedOperators"],
        str(full_delta["selectedOperator"]),
        tolerance_fraction,
    )
    audit = next(row for row in audits if row["serviceInstanceId"] == instance_id)
    matching = [row["edgeId"] for row in audit["candidates"] if row["withinTolerance"]]
    refused = not bindings and not matching
    return {
        "status": "binding-refused" if refused else "unexpected-binding",
        "serviceInstanceId": instance_id,
        "operatorName": full_delta["selectedOperator"],
        "affectedEdgeUnderTest": affected_edge_id,
        "metricNotPermitted": rejected_count,
        "syntheticMissingExecutions": conflicting_missing,
        "matchingEdgeCandidates": matching,
        "emittedBindings": bindings,
        "audit": audit,
    }


def evaluate(
    trace_base_path: Path,
    trace_evidence_dir: Path,
    full_delta_path: Path,
    tolerance_fraction: float,
) -> dict[str, object]:
    base_model = read_json(trace_base_path)
    current_graph = trace_graph(trace_evidence_dir)
    full_delta = read_json(full_delta_path)
    ambiguity = ambiguity_replay(
        base_model,
        current_graph,
        full_delta["observedOperators"],
        str(full_delta["selectedOperator"]),
        tolerance_fraction,
    )
    contradiction = contradiction_replay(
        base_model, current_graph, full_delta, tolerance_fraction
    )
    material = {
        "baseModelVersion": base_model["modelVersion"],
        "fullDeltaVersion": full_delta["deltaVersion"],
        "ambiguityReplay": ambiguity,
        "contradictionReplay": contradiction,
        "inputPolicy": {
            "journeyOutcomeRead": False,
            "usesFrozenPreOutcomeEvidence": True,
        },
    }
    return {
        "schemaVersion": "emac.negative-binding-cases/v1",
        "reportVersion": stable_hash(material),
        **material,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace-base", type=Path, required=True)
    parser.add_argument("--trace-evidence", type=Path, required=True)
    parser.add_argument("--full-delta", type=Path, required=True)
    parser.add_argument("--tolerance-fraction", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(
        args.trace_base,
        args.trace_evidence,
        args.full_delta,
        args.tolerance_fraction,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

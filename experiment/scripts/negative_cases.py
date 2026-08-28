#!/usr/bin/env python3
"""Counterfactual ambiguity and contradiction through the production pipeline."""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from apply_model_delta import apply_delta, validate_effective_lineage
from artifact_integrity import seal_artifact
from compile_journeys import compile_estimates, validate_compiled_estimates
from discover_model import discover_delta
from reconcile_model_delta import reconcile


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _rejected_instance(candidate: dict[str, object]) -> tuple[str, int] | None:
    rejected = [
        row
        for row in candidate["observedOperators"]
        if row["operatorName"] == candidate["selectedOperator"]
        and int(row["counts"]["notPermitted"]) > 0
    ]
    if not rejected:
        return None
    if len(rejected) != 1:
        raise ValueError(f"negative replay requires one rejected instance, observed {len(rejected)}")
    return str(rejected[0]["serviceInstanceId"]), int(rejected[0]["counts"]["notPermitted"])


def _edge_row(base_edge: dict[str, object], executions: int) -> dict[str, object]:
    return {
        "edgeId": base_edge["edgeId"],
        "sourceService": base_edge["sourceService"],
        "targetService": base_edge["targetService"],
        "executions": executions,
        "operations": base_edge.get("operations", []),
    }


def _ambiguity_mutation(
    traces: dict[str, object],
    base_model: dict[str, object],
    candidate: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    context = _rejected_instance(candidate)
    if context is None:
        return traces, {"status": "not-applicable"}
    instance_id, rejected = context
    instance = traces["byInstance"][instance_id]
    trace_total = int(instance["journeyTraces"])
    stable = sorted(
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
    if len(stable) < 2:
        raise ValueError("ambiguity replay requires two stable bootstrap edges")
    executions = trace_total - rejected
    mutated = copy.deepcopy(traces)
    for edge in stable[:2]:
        mutated["byInstance"][instance_id]["edges"][edge["edgeId"]] = _edge_row(
            edge, executions
        )
    return mutated, {
        "serviceInstanceId": instance_id,
        "syntheticallyAmbiguousEdges": [edge["edgeId"] for edge in stable[:2]],
        "metricNotPermitted": rejected,
    }


def _contradiction_mutation(
    traces: dict[str, object],
    base_model: dict[str, object],
    candidate: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    context = _rejected_instance(candidate)
    if context is None:
        return traces, {"status": "not-applicable"}
    instance_id, rejected = context
    if len(candidate["bindings"]) != 1:
        raise ValueError("contradiction replay requires one identified primary binding")
    edge_id = str(candidate["bindings"][0]["affectedEdge"]["edgeId"])
    base_edge = next(edge for edge in base_model["interactions"] if edge["edgeId"] == edge_id)
    mutated = copy.deepcopy(traces)
    instance = mutated["byInstance"][instance_id]
    trace_total = int(instance["journeyTraces"])
    conflicting_missing = max(0, rejected // 2)
    instance["edges"][edge_id] = _edge_row(base_edge, trace_total - conflicting_missing)
    return mutated, {
        "serviceInstanceId": instance_id,
        "affectedEdgeUnderTest": edge_id,
        "metricNotPermitted": rejected,
        "syntheticMissingExecutions": conflicting_missing,
    }


def _production_replay(
    name: str,
    mutation: Callable[
        [dict[str, object], dict[str, object], dict[str, object]],
        tuple[dict[str, object], dict[str, object]],
    ],
    base_model_path: Path,
    evidence_dir: Path,
    contract_path: Path,
    adapters_path: Path,
    primary_candidate: dict[str, object],
    tolerance_fraction: float,
) -> dict[str, object]:
    base_model = read_json(base_model_path)
    traces, mutation_record = mutation(
        read_json(evidence_dir / "traces.normalized.json"),
        base_model,
        primary_candidate,
    )
    if mutation_record.get("status") == "not-applicable":
        return {
            "status": "not-applicable",
            "reason": "no rejected operator decisions in this condition",
        }
    with tempfile.TemporaryDirectory(prefix=f"emac-{name}-") as temporary:
        replay_evidence = Path(temporary) / "evidence"
        shutil.copytree(evidence_dir / "snapshots", replay_evidence / "snapshots")
        shutil.copy2(evidence_dir / "load-summary.json", replay_evidence / "load-summary.json")
        (replay_evidence / "traces.normalized.json").write_text(
            json.dumps(traces, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        candidate = discover_delta(
            base_model_path, replay_evidence, adapters_path, tolerance_fraction
        )
        decision = reconcile(base_model, candidate)
        effective = apply_delta(base_model, candidate, decision)
        contract = read_json(contract_path)
        validate_effective_lineage(effective, base_model, candidate, decision)
        compiled = compile_estimates(effective, contract)
        validate_compiled_estimates(compiled, effective, contract)

    audit = candidate["discoveryAudit"]["operatorEdgeBindings"][0]
    matching = [
        row["edgeId"] for row in audit["candidates"] if row["withinTolerance"]
    ]
    expected_status = "unresolved" if name == "ambiguity" else "contradictory"
    refused = (
        decision["status"] == expected_status
        and compiled["status"] == "UNASSESSABLE"
        and not candidate["bindings"]
        and effective["appliedDeltaVersion"] is None
    )
    return {
        "status": "binding-refused" if refused else "unexpected-pipeline-result",
        **mutation_record,
        "matchingEdgeCandidates": matching,
        "emittedBindings": candidate["bindings"],
        "reconciliationStatus": decision["status"],
        "compilationStatus": compiled["status"],
        "pipeline": {
            "candidateDeltaVersion": candidate["deltaVersion"],
            "reconciliationVersion": decision["reconciliationVersion"],
            "effectiveModelVersion": effective["modelVersion"],
            "compilationVersion": compiled["compilationVersion"],
        },
        "audit": audit,
    }


def evaluate(
    base_model_path: Path,
    evidence_dir: Path,
    contract_path: Path,
    adapters_path: Path,
    tolerance_fraction: float,
) -> dict[str, object]:
    primary_candidate = discover_delta(
        base_model_path, evidence_dir, adapters_path, tolerance_fraction
    )
    ambiguity = _production_replay(
        "ambiguity",
        _ambiguity_mutation,
        base_model_path,
        evidence_dir,
        contract_path,
        adapters_path,
        primary_candidate,
        tolerance_fraction,
    )
    contradiction = _production_replay(
        "contradiction",
        _contradiction_mutation,
        base_model_path,
        evidence_dir,
        contract_path,
        adapters_path,
        primary_candidate,
        tolerance_fraction,
    )
    material = {
        "schemaVersion": "emac.negative-binding-cases/v2",
        "baseModelVersion": read_json(base_model_path)["modelVersion"],
        "primaryCandidateDeltaVersion": primary_candidate["deltaVersion"],
        "ambiguityReplay": ambiguity,
        "contradictionReplay": contradiction,
        "inputPolicy": {
            "journeyOutcomeRead": False,
            "usesFrozenPreOutcomeEvidence": True,
            "usesProductionPipeline": True,
        },
    }
    return seal_artifact(material, "reportVersion")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-model", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--adapters", type=Path, required=True)
    parser.add_argument("--tolerance-fraction", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(
        args.base_model,
        args.evidence,
        args.contract,
        args.adapters,
        args.tolerance_fraction,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

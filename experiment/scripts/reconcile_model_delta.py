#!/usr/bin/env python3
"""Admit or refuse a candidate runtime-model delta as a versioned decision."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from artifact_integrity import (
    DELTA_APPLICATION_FIELDS,
    seal_artifact,
    validate_bootstrap_model,
    validate_candidate_delta,
    validate_reconciliation,
)


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def reconcile(
    base_model: dict[str, object], candidate_delta: dict[str, object]
) -> dict[str, object]:
    validate_bootstrap_model(base_model)
    validate_candidate_delta(candidate_delta, base_model)
    runtime = candidate_delta["runtimeParameters"]
    not_permitted = int(runtime["notPermitted"])
    audits = candidate_delta["discoveryAudit"]["operatorEdgeBindings"]
    bindings = candidate_delta["bindings"]

    reasons: list[str] = []
    binding_decisions: list[dict[str, object]] = []
    status = "identified"
    if not_permitted:
        for audit in audits:
            matches = [
                candidate
                for candidate in audit["candidates"]
                if candidate["withinTolerance"]
            ]
            binding_decisions.append(
                {
                    "operatorName": audit["operatorName"],
                    "serviceInstanceId": audit["serviceInstanceId"],
                    "matchingEdgeIds": [row["edgeId"] for row in matches],
                    "candidateCount": len(matches),
                }
            )
        counts = [row["candidateCount"] for row in binding_decisions]
        if any(count > 1 for count in counts):
            status = "unresolved"
            reasons.append("multiple-count-consistent-edges")
        elif not counts or any(count == 0 for count in counts):
            status = "contradictory"
            reasons.append("no-count-consistent-edge")
        elif len(bindings) != len(binding_decisions):
            status = "unresolved"
            reasons.append("incomplete-operator-edge-binding")
    else:
        reasons.append("no-runtime-suppression")

    admitted = list(DELTA_APPLICATION_FIELDS) if status == "identified" else []
    rejected = [] if status == "identified" else list(DELTA_APPLICATION_FIELDS)
    artifact = {
        "schemaVersion": "emac.reconciliation-decision/v2",
        "baseModelVersion": base_model["modelVersion"],
        "candidateDeltaVersion": candidate_delta["deltaVersion"],
        "catalogVersion": candidate_delta["catalogVersion"],
        "status": status,
        "reasons": reasons,
        "admittedFields": admitted,
        "rejectedFields": rejected,
        "bindingDecisions": binding_decisions,
        "evidenceRefs": candidate_delta["evidenceRefs"],
        "policy": {
            "requiredBindingWhenNotPermitted": True,
            "uniqueCountConsistentEdgeRequired": True,
            "partialDeltaApplicationAllowed": False,
        },
    }
    result = seal_artifact(artifact, "reconciliationVersion")
    validate_reconciliation(result, base_model, candidate_delta)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-model", type=Path, required=True)
    parser.add_argument("--candidate-delta", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = read_json(args.base_model)
    candidate = read_json(args.candidate_delta)
    result = reconcile(base, candidate)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Compile journey reliability from an effective model; never read raw evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from apply_model_delta import validate_effective_lineage
from artifact_integrity import (
    IntegrityError,
    binding_matches_role,
    seal_artifact,
    validate_contract,
    validate_effective_model,
    verify_sealed_artifact,
)

def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def target_side(value: float, target: float) -> str:
    return "above-or-equal" if value >= target else "below"


def compile_estimates(
    effective_model: dict[str, object], contract: dict[str, object]
) -> dict[str, object]:
    validate_effective_model(effective_model)
    validate_contract(contract)
    if effective_model["contractId"] != contract["contractId"]:
        raise IntegrityError("effective model belongs to a different journey contract")
    if effective_model["contractVersion"] != contract["contractVersion"]:
        raise IntegrityError("effective model uses a different journey contract version")
    estimates: dict[str, object] = {}
    if effective_model["reconciliationStatus"] != "identified":
        reason = f"model-{effective_model['reconciliationStatus']}"
        for journey_id, declaration in contract["journeys"].items():
            estimates[journey_id] = {
                "assessmentStatus": "UNASSESSABLE",
                "target": float(declaration["target"]),
                "reason": reason,
                "requiredInteractionRole": declaration["suppressedInteractionRole"],
            }
        artifact = {
            "schemaVersion": "emac.compiled-journey-estimates/v3",
            "status": "UNASSESSABLE",
            "effectiveModelVersion": effective_model["modelVersion"],
            "reconciliationVersion": effective_model["reconciliationVersion"],
            "catalogVersion": effective_model["catalogVersion"],
            "contractId": contract["contractId"],
            "contractVersion": contract["contractVersion"],
            "estimates": estimates,
            "inputPolicy": {
                "effectiveModelContentHashVerified": True,
                "contractContentHashVerified": True,
                "rawMetricsRead": False,
                "rawTracesRead": False,
                "responseBodyRead": False,
                "outcomeRead": False,
            },
        }
        return seal_artifact(artifact, "compilationVersion")

    runtime = effective_model["runtimeReliability"]
    a_prefix = float(runtime["A_P"])
    q = float(runtime["q"])
    a_visits = float(runtime["A_V"])

    all_assessed = True
    for journey_id, declaration in contract["journeys"].items():
        role_id = str(declaration["suppressedInteractionRole"])
        role = contract["interactionRoles"][role_id]
        matching_bindings = [
            binding
            for binding in effective_model.get("operatorBindings", [])
            if binding_matches_role(binding, role)
        ]
        if q < 1.0 and len(matching_bindings) != 1:
            all_assessed = False
            estimates[journey_id] = {
                "assessmentStatus": "UNASSESSABLE",
                "target": float(declaration["target"]),
                "reason": "required-interaction-role-not-uniquely-bound",
                "requiredInteractionRole": role_id,
                "matchingBindingCount": len(matching_bindings),
            }
            continue
        a_fallback = 1.0 if declaration["fallbackSatisfiesJourney"] else 0.0
        discovered = a_prefix * (
            q * a_visits + (1.0 - q * a_visits) * a_fallback
        )
        frozen = a_prefix * (a_visits + (1.0 - a_visits) * a_fallback)
        target = float(declaration["target"])
        estimates[journey_id] = {
            "assessmentStatus": "ASSESSED",
            "target": target,
            "requiredInteractionRole": role_id,
            "semanticBinding": (
                matching_bindings[0]["affectedEdge"] if matching_bindings else None
            ),
            "fallbackSatisfiesJourney": declaration["fallbackSatisfiesJourney"],
            "modelDiscoveredEstimate": discovered,
            "frozenModelEstimate": frozen,
            "modelDiscoveredTargetSide": target_side(discovered, target),
            "frozenModelTargetSide": target_side(frozen, target),
        }

    artifact = {
        "schemaVersion": "emac.compiled-journey-estimates/v3",
        "status": "ASSESSED" if all_assessed else "UNASSESSABLE",
        "effectiveModelVersion": effective_model["modelVersion"],
        "reconciliationVersion": effective_model["reconciliationVersion"],
        "catalogVersion": effective_model["catalogVersion"],
        "contractId": contract["contractId"],
        "contractVersion": contract["contractVersion"],
        "estimates": estimates,
        "runtimeParametersFromEffectiveModel": runtime,
        "inputPolicy": {
            "effectiveModelContentHashVerified": True,
            "contractContentHashVerified": True,
            "rawMetricsRead": False,
            "rawTracesRead": False,
            "responseBodyRead": False,
            "outcomeRead": False,
        },
    }
    return seal_artifact(artifact, "compilationVersion")


def validate_compiled_estimates(
    compiled: dict[str, object],
    effective_model: dict[str, object],
    contract: dict[str, object],
) -> None:
    """Verify terminal sealing, input lineage, and exact compilation semantics."""
    verify_sealed_artifact(
        compiled,
        "compilationVersion",
        "emac.compiled-journey-estimates/v3",
    )
    expected = compile_estimates(effective_model, contract)
    if compiled != expected:
        raise IntegrityError(
            "compiled estimates do not match the declared effective model and contract"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-model", type=Path, required=True)
    parser.add_argument("--candidate-delta", type=Path, required=True)
    parser.add_argument("--reconciliation", type=Path, required=True)
    parser.add_argument("--effective-model", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = read_json(args.base_model)
    candidate = read_json(args.candidate_delta)
    reconciliation = read_json(args.reconciliation)
    effective = read_json(args.effective_model)
    contract = read_json(args.contract)
    validate_effective_lineage(effective, base, candidate, reconciliation)
    result = compile_estimates(effective, contract)
    validate_compiled_estimates(result, effective, contract)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

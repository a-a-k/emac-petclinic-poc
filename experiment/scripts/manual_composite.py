#!/usr/bin/env python3
"""Hand-maintained resilience-aware composite used as a strong accuracy baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from artifact_integrity import (
    binding_matches_role,
    runtime_parameter_issues,
    runtime_parameters_from_counts,
    validate_contract,
)
from discover_model import (
    aggregate_operator,
    load_adapters,
    metric_observations,
    trace_graph,
)


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate(
    evidence_dir: Path,
    contract: dict[str, object],
    manual_model: dict[str, object],
    adapters_path: Path,
) -> dict[str, object]:
    validate_contract(contract)
    observations = metric_observations(evidence_dir, load_adapters(adapters_path))
    operator = str(manual_model["operatorName"])
    role_id = str(manual_model["primaryInteractionRole"])
    role = contract["interactionRoles"].get(role_id)
    if role is None:
        raise ValueError(f"manual model references unknown interaction role {role_id!r}")
    if manual_model.get("fallback") != role.get("fallbackId"):
        raise ValueError("manual fallback does not match the declared interaction role")
    if any(
        declaration["suppressedInteractionRole"] != role_id
        for declaration in contract["journeys"].values()
    ):
        raise ValueError("manual composite cannot evaluate journeys with a different role")
    primary = manual_model["primaryEdge"]
    counts = aggregate_operator(observations, operator)
    eligible = int(read_json(evidence_dir / "load-summary.json")["completed"])
    runtime = runtime_parameters_from_counts(eligible, counts)

    def unassessable(reasons: list[str]) -> dict[str, object]:
        return {
            "schemaVersion": "emac.manual-dynamic-composite/v1",
            "assessmentStatus": "UNASSESSABLE",
            "manualMapping": manual_model,
            "reasons": reasons,
            "runtimeParameters": runtime,
            "estimates": {journey_id: None for journey_id in contract["journeys"]},
        }

    issues = runtime_parameter_issues(runtime)
    if issues:
        return unassessable(issues)
    matching_edges = [
        edge
        for edge in trace_graph(evidence_dir)["interactions"]
        if edge["sourceService"] == primary["sourceService"]
        and edge["targetService"] == primary["targetService"]
    ]
    if len(matching_edges) > 1:
        return unassessable(["manual-primary-edge-ambiguous-in-traces"])
    if not matching_edges and counts["permitted"] > 0:
        return unassessable(["manual-primary-edge-absent-despite-permitted-calls"])
    manual_binding = {"affectedEdge": matching_edges[0]} if matching_edges else None
    if manual_binding is not None and not binding_matches_role(manual_binding, role):
        return unassessable(["manual-primary-edge-does-not-satisfy-declared-role"])
    estimates = {}
    for journey_id, declaration in contract["journeys"].items():
        a_fallback = 1.0 if declaration["fallbackSatisfiesJourney"] else 0.0
        estimates[journey_id] = (
            counts["permittedSuccessful"]
            + (counts["decisions"] - counts["permittedSuccessful"]) * a_fallback
        ) / eligible
    return {
        "schemaVersion": "emac.manual-dynamic-composite/v1",
        "assessmentStatus": "ASSESSED",
        "manualMapping": manual_model,
        "semanticBinding": {
            "role": role_id,
            "affectedEdge": matching_edges[0] if matching_edges else primary,
            "source": "runtime-traces" if matching_edges else "declared-manual-mapping",
        },
        "runtimeParameters": runtime,
        "estimates": estimates,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--manual-model", type=Path, required=True)
    parser.add_argument("--adapters", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(
        args.evidence,
        read_json(args.contract),
        read_json(args.manual_model),
        args.adapters,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

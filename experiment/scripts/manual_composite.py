#!/usr/bin/env python3
"""Hand-maintained resilience-aware composite used as a strong accuracy baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from artifact_integrity import binding_matches_role, validate_contract
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
    primary = manual_model["primaryEdge"]
    matching_edges = [
        edge
        for edge in trace_graph(evidence_dir)["interactions"]
        if edge["sourceService"] == primary["sourceService"]
        and edge["targetService"] == primary["targetService"]
    ]
    if len(matching_edges) != 1:
        raise ValueError("manual primary edge is not uniquely present in runtime traces")
    manual_binding = {"affectedEdge": matching_edges[0]}
    if not binding_matches_role(manual_binding, role):
        raise ValueError("manual primary edge does not satisfy its declared semantic role")
    if any(
        declaration["suppressedInteractionRole"] != role_id
        for declaration in contract["journeys"].values()
    ):
        raise ValueError("manual composite cannot evaluate journeys with a different role")
    counts = aggregate_operator(observations, operator)
    eligible = int(read_json(evidence_dir / "load-summary.json")["completed"])
    if not eligible or not counts["decisions"] or not counts["permitted"]:
        raise ValueError("manual composite has an undefined denominator")
    a_prefix = counts["decisions"] / eligible
    q = counts["permitted"] / counts["decisions"]
    a_visits = counts["permittedSuccessful"] / counts["permitted"]
    estimates = {}
    for journey_id, declaration in contract["journeys"].items():
        a_suppressed = 1.0 if declaration["suppressedInteractionSatisfies"] else 0.0
        estimates[journey_id] = a_prefix * (
            q * a_visits + (1.0 - q * a_visits) * a_suppressed
        )
    return {
        "schemaVersion": "emac.manual-dynamic-composite/v1",
        "manualMapping": manual_model,
        "semanticBinding": {"role": role_id, "affectedEdge": matching_edges[0]},
        "runtimeParameters": {"A_P": a_prefix, "q": q, "A_V": a_visits},
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

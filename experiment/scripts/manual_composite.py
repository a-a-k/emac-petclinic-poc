#!/usr/bin/env python3
"""Hand-maintained resilience-aware composite used as a strong accuracy baseline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from discover_model import aggregate_operator, load_adapters, metric_observations


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def evaluate(
    evidence_dir: Path,
    contract: dict[str, object],
    manual_model: dict[str, object],
    adapters_path: Path,
) -> dict[str, object]:
    observations = metric_observations(evidence_dir, load_adapters(adapters_path))
    operator = str(manual_model["operatorName"])
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

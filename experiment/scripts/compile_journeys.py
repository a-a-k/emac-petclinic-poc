#!/usr/bin/env python3
"""Compile journey reliability from an effective model; never read raw evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def target_side(value: float, target: float) -> str:
    return "above-or-equal" if value >= target else "below"


def compile_estimates(
    effective_model: dict[str, object], contract: dict[str, object]
) -> dict[str, object]:
    runtime = effective_model["runtimeReliability"]
    a_prefix = float(runtime["A_P"])
    q = float(runtime["q"])
    a_visits = float(runtime["A_V"])
    if q < 1.0 and len(effective_model.get("operatorBindings", [])) != 1:
        raise ValueError("suppression was measured but no unique operator-to-edge binding exists")

    estimates: dict[str, object] = {}
    for journey_id, declaration in contract["journeys"].items():
        a_suppressed = 1.0 if declaration["suppressedInteractionSatisfies"] else 0.0
        discovered = a_prefix * (
            q * a_visits + (1.0 - q * a_visits) * a_suppressed
        )
        frozen = a_prefix * (a_visits + (1.0 - a_visits) * a_suppressed)
        target = float(declaration["target"])
        estimates[journey_id] = {
            "target": target,
            "suppressedInteractionSatisfies": declaration[
                "suppressedInteractionSatisfies"
            ],
            "modelDiscoveredEstimate": discovered,
            "frozenModelEstimate": frozen,
            "modelDiscoveredTargetSide": target_side(discovered, target),
            "frozenModelTargetSide": target_side(frozen, target),
        }

    material = {
        "effectiveModelVersion": effective_model["modelVersion"],
        "contractId": contract["contractId"],
        "estimates": estimates,
    }
    compilation_version = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schemaVersion": "emac.compiled-journey-estimates/v1",
        "compilationVersion": compilation_version,
        **material,
        "runtimeParametersFromEffectiveModel": runtime,
        "inputPolicy": {
            "rawMetricsRead": False,
            "rawTracesRead": False,
            "responseBodyRead": False,
            "outcomeRead": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--effective-model", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compile_estimates(read_json(args.effective_model), read_json(args.contract))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

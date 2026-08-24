#!/usr/bin/env python3
"""Apply a typed discovery delta to a bootstrap model and version the result."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def stable_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def apply_delta(base: dict[str, object], delta: dict[str, object]) -> dict[str, object]:
    if delta["baseModelVersion"] != base["modelVersion"]:
        raise ValueError(
            f"delta targets {delta['baseModelVersion']}, not bootstrap {base['modelVersion']}"
        )
    effective = copy.deepcopy(base)
    effective["schemaVersion"] = "emac.effective-interaction-model/v1"
    effective["parentModelVersion"] = base["modelVersion"]
    effective["appliedDeltaVersion"] = delta["deltaVersion"]

    observed_by_key = {
        (row["adapterId"], row["operatorName"], row["serviceInstanceId"]): row
        for row in delta["observedOperators"]
    }
    for operator in effective["operators"]:
        key = (operator["adapterId"], operator["operatorName"], operator["serviceInstanceId"])
        observed = observed_by_key.get(key)
        if observed:
            operator["runtimeState"] = observed["runtimeState"]
            operator["counts"] = observed["counts"]
            operator["metricSource"] = observed["metricSource"]

    effective["interactions"] = delta["observedTraceGraph"]["interactions"]
    effective["operatorBindings"] = delta["bindings"]
    effective["runtimeReliability"] = {
        "selectedOperator": delta["selectedOperator"],
        **delta["runtimeParameters"],
    }
    effective["appliedStateChanges"] = delta["stateChanges"]
    effective["discoveryAudit"] = delta["discoveryAudit"]
    version_material = copy.deepcopy(effective)
    version_material.pop("modelVersion", None)
    effective["modelVersion"] = stable_hash(version_material)
    return effective


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-model", type=Path, required=True)
    parser.add_argument("--delta", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = apply_delta(read_json(args.base_model), read_json(args.delta))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

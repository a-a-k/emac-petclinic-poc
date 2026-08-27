#!/usr/bin/env python3
"""Apply a typed discovery delta to a bootstrap model and version the result."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from artifact_integrity import (
    DELTA_APPLICATION_FIELDS,
    seal_artifact,
    validate_effective_model,
    validate_reconciliation,
)

def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def apply_delta(
    base: dict[str, object],
    delta: dict[str, object],
    reconciliation: dict[str, object],
) -> dict[str, object]:
    validate_reconciliation(reconciliation, base, delta)
    effective = copy.deepcopy(base)
    effective.pop("modelVersion", None)
    effective["schemaVersion"] = "emac.effective-interaction-model/v2"
    effective["parentModelVersion"] = base["modelVersion"]
    effective["candidateDeltaVersion"] = delta["deltaVersion"]
    effective["reconciliationVersion"] = reconciliation["reconciliationVersion"]
    effective["reconciliationStatus"] = reconciliation["status"]
    effective["reconciliation"] = {
        "reasons": reconciliation["reasons"],
        "admittedFields": reconciliation["admittedFields"],
        "rejectedFields": reconciliation["rejectedFields"],
        "bindingDecisions": reconciliation["bindingDecisions"],
    }
    effective["baselineInteractions"] = copy.deepcopy(base["interactions"])
    effective["evidenceRefs"] = delta["evidenceRefs"]

    if reconciliation["status"] != "identified":
        effective["appliedDeltaVersion"] = None
        effective["operatorBindings"] = []
        effective["unresolvedCandidate"] = {
            "deltaVersion": delta["deltaVersion"],
            "requiredFields": reconciliation["rejectedFields"],
        }
        result = seal_artifact(effective, "modelVersion")
        validate_effective_model(result)
        return result

    admitted = set(reconciliation["admittedFields"])
    if admitted != set(DELTA_APPLICATION_FIELDS):
        raise ValueError("identified reconciliation did not admit the complete delta")

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
    result = seal_artifact(effective, "modelVersion")
    validate_effective_model(result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-model", type=Path, required=True)
    parser.add_argument("--delta", type=Path, required=True)
    parser.add_argument("--reconciliation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = apply_delta(
        read_json(args.base_model),
        read_json(args.delta),
        read_json(args.reconciliation),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

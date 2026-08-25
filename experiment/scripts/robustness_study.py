#!/usr/bin/env python3
"""Replay frozen trace evidence under sampling and identity loss."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

from collect_trace_evidence import normalize_trace
from discover_model import interactions_from_by_instance


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def stable_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def selected(trace_id: str, rate: float, seed: int) -> bool:
    digest = hashlib.sha256(f"{seed}:{rate}:{trace_id}".encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big") / 2**64
    return value < rate


def sampled_trace_graph(
    evidence_dir: Path,
    contract: dict[str, object],
    rate: float,
    seed: int,
) -> dict[str, object]:
    if not 0.0 < rate <= 1.0:
        raise ValueError("sampling rate must be in (0, 1]")
    by_instance: dict[str, dict[str, object]] = {}
    seen: set[str] = set()
    considered = 0
    sampled = 0
    for raw_path in sorted((evidence_dir / "traces.raw.chunks").glob("*.json.gz")):
        with gzip.open(raw_path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
        for trace in payload.get("data", []):
            trace_id = str(trace.get("traceID", ""))
            if trace_id and trace_id in seen:
                continue
            if trace_id:
                seen.add(trace_id)
            row = normalize_trace(trace, contract)
            if row is None:
                continue
            considered += 1
            if not selected(str(row["traceId"]), rate, seed):
                continue
            sampled += 1
            instance = by_instance.setdefault(
                str(row["entryInstance"]), {"journeyTraces": 0, "edges": {}}
            )
            instance["journeyTraces"] += 1
            for edge in row["edges"]:
                edge_row = instance["edges"].setdefault(
                    edge["edgeId"],
                    {
                        "edgeId": edge["edgeId"],
                        "sourceService": edge["sourceService"],
                        "targetService": edge["targetService"],
                        "executions": 0,
                        "operations": set(),
                    },
                )
                edge_row["executions"] += 1
                edge_row["operations"].update(edge.get("operations", []))
    for instance in by_instance.values():
        instance["edges"] = {
            edge_id: {**edge, "operations": sorted(edge["operations"])}
            for edge_id, edge in sorted(instance["edges"].items())
        }
    return {
        "samplingRate": rate,
        "samplingSeed": seed,
        "consideredJourneyTraces": considered,
        "sampledJourneyTraces": sampled,
        "byInstance": by_instance,
        "interactions": interactions_from_by_instance(by_instance),
    }


def rate_binding(
    base_model: dict[str, object],
    graph: dict[str, object],
    full_delta: dict[str, object],
    tolerance_fraction: float,
    minimum_instance_traces: int = 3,
) -> dict[str, object]:
    rejected = [
        row
        for row in full_delta["observedOperators"]
        if row["operatorName"] == full_delta["selectedOperator"]
        and int(row["counts"]["notPermitted"]) > 0
    ]
    if not rejected:
        return {"status": "no-drift", "binding": None, "falseBinding": False}
    if len(rejected) != 1:
        return {
            "status": "unresolved",
            "reason": "multiple rejected instances",
            "binding": None,
            "falseBinding": False,
        }
    observation = rejected[0]
    instance_id = str(observation["serviceInstanceId"])
    trace_total = int(graph["byInstance"].get(instance_id, {}).get("journeyTraces", 0))
    if trace_total < minimum_instance_traces:
        return {
            "status": "unresolved",
            "reason": "insufficient sampled traces for rejected instance",
            "serviceInstanceId": instance_id,
            "sampledInstanceTraces": trace_total,
            "minimumSampledInstanceTraces": minimum_instance_traces,
            "binding": None,
            "falseBinding": False,
        }
    decisions = int(observation["counts"]["decisions"])
    rejected_rate = int(observation["counts"]["notPermitted"]) / decisions
    current_edges = {row["edgeId"]: row for row in graph["interactions"]}
    candidates = []
    for base_edge in base_model["interactions"]:
        baseline = base_edge.get("byInstance", {}).get(instance_id)
        if not baseline or float(baseline.get("executionRate") or 0.0) < 0.95:
            continue
        current = current_edges.get(base_edge["edgeId"], {}).get("byInstance", {}).get(
            instance_id, {}
        )
        executions = int(current.get("executions", 0))
        suppression_rate = (trace_total - executions) / trace_total
        difference = abs(suppression_rate - rejected_rate)
        candidates.append(
            {
                "edgeId": base_edge["edgeId"],
                "sourceService": base_edge["sourceService"],
                "targetService": base_edge["targetService"],
                "sampledInstanceTraces": trace_total,
                "sampledExecutions": executions,
                "sampledSuppressionRate": suppression_rate,
                "metricRejectionRate": rejected_rate,
                "absoluteRateDifference": difference,
                "withinTolerance": difference <= tolerance_fraction,
            }
        )
    matches = [row for row in candidates if row["withinTolerance"]]
    binding = matches[0] if len(matches) == 1 else None
    expected = (
        full_delta["bindings"][0]["affectedEdge"]["edgeId"]
        if len(full_delta["bindings"]) == 1
        else None
    )
    false_binding = binding is not None and binding["edgeId"] != expected
    return {
        "status": "recovered" if binding else "unresolved",
        "serviceInstanceId": instance_id,
        "binding": binding,
        "candidates": candidates,
        "falseBinding": false_binding,
    }


def identity_redaction(
    base_model: dict[str, object],
    current_graph: dict[str, object],
    full_delta: dict[str, object],
    tolerance_fraction: float,
) -> dict[str, object]:
    observations = [
        row
        for row in full_delta["observedOperators"]
        if row["operatorName"] == full_delta["selectedOperator"]
    ]
    permitted = sum(int(row["counts"]["permitted"]) for row in observations)
    decisions = sum(int(row["counts"]["decisions"]) for row in observations)
    rejected = sum(int(row["counts"]["notPermitted"]) for row in observations)
    states = sorted({str(row["runtimeState"]) for row in observations})
    total_traces = sum(
        int(row["journeyTraces"]) for row in current_graph["byInstance"].values()
    )
    current_edges = {row["edgeId"]: row for row in current_graph["interactions"]}
    tolerance = max(1, round(total_traces * tolerance_fraction))
    candidates = []
    for base_edge in base_model["interactions"]:
        base_executions = sum(
            int(row["executions"]) for row in base_edge.get("byInstance", {}).values()
        )
        base_traces = sum(
            int(row["journeyTraces"]) for row in base_edge.get("byInstance", {}).values()
        )
        if not base_traces or base_executions / base_traces < 0.95:
            continue
        executions = int(current_edges.get(base_edge["edgeId"], {}).get("executions", 0))
        missing = total_traces - executions
        difference = abs(missing - rejected)
        candidates.append(
            {
                "edgeId": base_edge["edgeId"],
                "missingExecutions": missing,
                "metricNotPermitted": rejected,
                "withinTolerance": difference <= tolerance,
            }
        )
    matches = [row for row in candidates if row["withinTolerance"]]
    return {
        "globalQ": permitted / decisions if decisions else None,
        "operatorStateMixture": states,
        "globalAffectedEdge": matches[0]["edgeId"] if len(matches) == 1 else None,
        "specificInstance": "unresolved",
        "candidateAudit": candidates,
        "identityRemovedFromMetrics": True,
        "identityRemovedFromTraces": True,
    }


def evaluate(
    base_model_path: Path,
    evidence_dir: Path,
    contract_path: Path,
    full_delta_path: Path,
    tolerance_fraction: float,
    minimum_instance_traces: int,
    seed: int,
    rates: tuple[float, ...] = (0.10, 0.01),
) -> dict[str, object]:
    base_model = read_json(base_model_path)
    contract = read_json(contract_path)
    full_delta = read_json(full_delta_path)
    full_graph = full_delta["observedTraceGraph"]
    sampling = {}
    for index, rate in enumerate(rates):
        graph = sampled_trace_graph(evidence_dir, contract, rate, seed + index)
        sampling[str(rate)] = {
            "graphSummary": {
                "consideredJourneyTraces": graph["consideredJourneyTraces"],
                "sampledJourneyTraces": graph["sampledJourneyTraces"],
                "sampledByInstance": {
                    instance: row["journeyTraces"] for instance, row in graph["byInstance"].items()
                },
            },
            "discovery": rate_binding(
                base_model,
                graph,
                full_delta,
                tolerance_fraction,
                minimum_instance_traces,
            ),
        }
    material = {
        "baseModelVersion": base_model["modelVersion"],
        "fullDeltaVersion": full_delta["deltaVersion"],
        "traceSampling": sampling,
        "identityRedaction": identity_redaction(
            base_model, full_graph, full_delta, tolerance_fraction
        ),
        "inputPolicy": {
            "journeyOutcomeRead": False,
            "usesFrozenPreOutcomeEvidence": True,
        },
    }
    return {
        "schemaVersion": "emac.robustness-study/v1",
        "reportVersion": stable_hash(material),
        **material,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-model", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--full-delta", type=Path, required=True)
    parser.add_argument("--tolerance-fraction", type=float, required=True)
    parser.add_argument("--minimum-instance-traces", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(
        args.base_model,
        args.evidence,
        args.contract,
        args.full_delta,
        args.tolerance_fraction,
        args.minimum_instance_traces,
        args.seed,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

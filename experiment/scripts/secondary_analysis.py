#!/usr/bin/env python3
"""Run and finalize separately visible secondary analyses for one paired block."""

from __future__ import annotations

import argparse
import json
import math
import shutil
from pathlib import Path

from evidence_ablations import evaluate as evaluate_ablations
from negative_cases import evaluate as evaluate_negative_cases
from robustness_study import evaluate as evaluate_robustness
from run_experiment import read_json, summarize, write_json, write_markdown


ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = ROOT / "experiment" / "protocol.json"
CONTRACT_PATH = ROOT / "experiment" / "journey-contract.json"
ADAPTERS_PATH = ROOT / "experiment" / "operator-adapters.json"


def condition_dirs(pair_dir: Path) -> list[Path]:
    paths = [pair_dir / name for name in ("control", "treatment")]
    if not all(path.is_dir() for path in paths):
        raise ValueError(f"paired block is incomplete: {paths}")
    return paths


def materialize_ablation_inputs(condition_dir: Path) -> tuple[Path, Path, Path, Path]:
    evidence_dir = condition_dir / "evidence"
    bootstrap = read_json(condition_dir / "model" / "bootstrap-model.json")
    ablation_dir = condition_dir / "ablations"
    metric_base = ablation_dir / "inputs" / "metrics-only" / "bootstrap-operators.json"
    metric_evidence = ablation_dir / "inputs" / "metrics-only" / "evidence"
    trace_base = ablation_dir / "inputs" / "traces-only" / "bootstrap-interactions.json"
    trace_evidence = ablation_dir / "inputs" / "traces-only" / "evidence"
    write_json(
        metric_base,
        {
            "schemaVersion": "emac.metrics-only-bootstrap-view/v1",
            "modelVersion": bootstrap["modelVersion"],
            "operators": bootstrap["operators"],
        },
    )
    metric_evidence.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        evidence_dir / "snapshots", metric_evidence / "snapshots", dirs_exist_ok=True
    )
    shutil.copy2(evidence_dir / "load-summary.json", metric_evidence / "load-summary.json")
    write_json(
        trace_base,
        {
            "schemaVersion": "emac.traces-only-bootstrap-view/v1",
            "modelVersion": bootstrap["modelVersion"],
            "interactions": bootstrap["interactions"],
        },
    )
    trace_evidence.mkdir(parents=True, exist_ok=True)
    shutil.copy2(
        evidence_dir / "traces.normalized.json",
        trace_evidence / "traces.normalized.json",
    )
    return metric_base, metric_evidence, trace_base, trace_evidence


def run_ablations(pair_dir: Path, protocol: dict[str, object]) -> None:
    tolerance = float(protocol["measurement"]["operatorEdgeBindingToleranceFraction"])
    for condition_dir in condition_dirs(pair_dir):
        inputs = materialize_ablation_inputs(condition_dir)
        report = evaluate_ablations(
            *inputs,
            ADAPTERS_PATH,
            condition_dir / "model" / "typed-delta.json",
            tolerance,
        )
        write_json(condition_dir / "ablations" / "evidence-source-ablation.json", report)
        print(
            "EMAC_SECONDARY kind=ablation"
            f" condition={condition_dir.name}"
            f" metrics={report['metricsOnly']['edgeBinding']['status']}"
            f" traces={report['tracesOnly']['suppression']['status']}"
            f" fusion={report['fullFusion']['status']}",
            flush=True,
        )


def run_negative_cases(pair_dir: Path, protocol: dict[str, object]) -> None:
    tolerance = float(protocol["measurement"]["operatorEdgeBindingToleranceFraction"])
    for condition_dir in condition_dirs(pair_dir):
        trace_base = (
            condition_dir
            / "ablations"
            / "inputs"
            / "traces-only"
            / "bootstrap-interactions.json"
        )
        trace_evidence = (
            condition_dir / "ablations" / "inputs" / "traces-only" / "evidence"
        )
        report = evaluate_negative_cases(
            trace_base,
            trace_evidence,
            condition_dir / "model" / "typed-delta.json",
            tolerance,
        )
        write_json(condition_dir / "negative-cases" / "report.json", report)
        print(
            "EMAC_SECONDARY kind=negative"
            f" condition={condition_dir.name}"
            f" ambiguity={report['ambiguityReplay']['status']}"
            f" contradiction={report['contradictionReplay']['status']}",
            flush=True,
        )


def run_robustness(pair_dir: Path, protocol: dict[str, object]) -> None:
    tolerance = float(protocol["measurement"]["operatorEdgeBindingToleranceFraction"])
    minimum_instance_traces = int(
        protocol["evaluation"]["robustness"]["minimumSampledInstanceTraces"]
    )
    seed = int(read_json(pair_dir / "schedule.json")["seed"])
    for index, condition_dir in enumerate(condition_dirs(pair_dir)):
        report = evaluate_robustness(
            condition_dir / "model" / "bootstrap-model.json",
            condition_dir / "evidence",
            CONTRACT_PATH,
            condition_dir / "model" / "typed-delta.json",
            tolerance,
            minimum_instance_traces,
            seed + index * 1000,
        )
        write_json(condition_dir / "robustness" / "report.json", report)
        print(
            "EMAC_SECONDARY kind=robustness"
            f" condition={condition_dir.name}"
            f" sampling10={report['traceSampling']['0.1']['discovery']['status']}"
            f" sampling1={report['traceSampling']['0.01']['discovery']['status']}"
            f" identity={report['identityRedaction']['specificInstance']}",
            flush=True,
        )


def secondary_checks(
    condition: str,
    result: dict[str, object],
    assignment: dict[str, object],
    ablations: dict[str, object],
    negatives: dict[str, object],
    robustness: dict[str, object],
) -> dict[str, bool]:
    metrics_only = ablations["metricsOnly"]
    traces_only = ablations["tracesOnly"]
    full_fusion = ablations["fullFusion"]
    identity = robustness["identityRedaction"]
    q = float(result["discovery"]["runtimeParameters"]["q"])
    checks = {
        "ablationSourcesAreSeparated": bool(ablations["sourceIsolation"]["verified"]),
        "metricsOnlyLeavesEdgeUnresolved": metrics_only["edgeBinding"]["status"]
        == "unresolved",
        "tracesOnlyLeavesOperatorUnresolved": traces_only["operator"]["status"]
        == "unresolved",
        "fullFusionQMatchesPrimary": math.isclose(
            float(full_fusion["q"]), q, abs_tol=1e-12
        ),
        "sampling10NeverFalseBinds": not robustness["traceSampling"]["0.1"][
            "discovery"
        ]["falseBinding"],
        "sampling1NeverFalseBinds": not robustness["traceSampling"]["0.01"][
            "discovery"
        ]["falseBinding"],
        "identityRedactionPreservesGlobalQ": math.isclose(
            float(identity["globalQ"]), q, abs_tol=1e-12
        ),
        "identityRedactionLeavesInstanceUnresolved": identity["specificInstance"]
        == "unresolved",
    }
    if condition == "treatment":
        metrics_state = metrics_only["stateChanges"]
        trace_edge = traces_only["suppression"]["affectedEdge"]
        checks.update(
            {
                "metricsOnlyStateRecovery": (
                    len(metrics_state) == 1
                    and metrics_state[0]["serviceInstanceId"]
                    == assignment["minorityInstanceId"]
                    and metrics_state[0]["after"] == "OPEN"
                ),
                "tracesOnlyEdgeRecovery": (
                    traces_only["suppression"]["status"] == "identified"
                    and trace_edge is not None
                    and trace_edge["serviceInstanceId"] == assignment["minorityInstanceId"]
                    and trace_edge["sourceService"] == "api-gateway"
                    and trace_edge["targetService"] == "visits-service"
                ),
                "fullFusionTypedRecovery": full_fusion["status"] == "typed-delta",
                "ambiguityReplayRefusesBinding": (
                    negatives["ambiguityReplay"]["status"] == "binding-refused"
                    and len(negatives["ambiguityReplay"]["matchingEdgeCandidates"]) >= 2
                    and not negatives["ambiguityReplay"]["emittedBindings"]
                ),
                "contradictionReplayRefusesBinding": (
                    negatives["contradictionReplay"]["status"] == "binding-refused"
                    and not negatives["contradictionReplay"]["emittedBindings"]
                ),
                "identityRedactionLeavesBindingUnresolved": (
                    identity["globalAffectedEdge"] is None
                    and identity["specificInstance"] == "unresolved"
                ),
            }
        )
    else:
        checks.update(
            {
                "metricsOnlyNoFalseStateDelta": not metrics_only["stateChanges"],
                "tracesOnlyNoFalseSuppression": traces_only["suppression"]["status"]
                == "no-drift",
                "fullFusionNoFalseDelta": full_fusion["status"] == "no-drift",
                "ambiguityReplayNotApplicable": negatives["ambiguityReplay"]["status"]
                == "not-applicable",
                "contradictionReplayNotApplicable": negatives["contradictionReplay"][
                    "status"
                ]
                == "not-applicable",
            }
        )
    return checks


def finalize(pair_dir: Path, phase: str, ordinal: int, allow_invalid: bool) -> None:
    run_root = pair_dir.parents[1]
    assignment = read_json(run_root / "ground-truth" / "runtime-assignment.json")
    pair_result_path = pair_dir / "pair-result.json"
    pair = read_json(pair_result_path)
    for condition, result in pair["conditions"].items():
        condition_dir = pair_dir / condition
        ablations = read_json(condition_dir / "ablations" / "evidence-source-ablation.json")
        negatives = read_json(condition_dir / "negative-cases" / "report.json")
        robustness = read_json(condition_dir / "robustness" / "report.json")
        checks = secondary_checks(
            condition, result, assignment, ablations, negatives, robustness
        )
        result["ablations"] = ablations
        result["negativeCases"] = negatives
        result["robustness"] = robustness
        result["validity"]["checks"].update(checks)
        result["validity"]["valid"] = all(result["validity"]["checks"].values())
        write_json(condition_dir / "result.json", result)
        write_json(condition_dir / "validity.json", result["validity"])
    pair["valid"] = pair["localSliBalance"]["valid"] and all(
        row["validity"]["valid"] for row in pair["conditions"].values()
    )
    pair["secondaryAnalysisStatus"] = "complete"
    write_json(pair_result_path, pair)
    confirmatory = [pair] if phase == "confirmatory" else []
    report = summarize(confirmatory, [pair])
    report.update(
        {
            "phase": phase,
            "pairOrdinal": ordinal,
            "requestedConfirmatoryPairs": 1 if phase == "confirmatory" else 0,
            "replacementPairsUsed": 0,
        }
    )
    write_json(run_root / "report.json", report)
    write_markdown(run_root / "report.md", report)
    print(f"EMAC_SECONDARY kind=finalize pair={pair['pairId']} valid={pair['valid']}", flush=True)
    if not pair["valid"] and not allow_invalid:
        raise SystemExit(f"{phase} pair {ordinal} failed final validity checks")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command", choices=("ablations", "negative-cases", "robustness", "finalize")
    )
    parser.add_argument("--pair-dir", type=Path, required=True)
    parser.add_argument("--phase", choices=("pilot", "confirmatory"), required=True)
    parser.add_argument("--pair-ordinal", type=int, required=True)
    parser.add_argument("--allow-invalid", action="store_true")
    args = parser.parse_args()
    protocol = read_json(PROTOCOL_PATH)
    if args.command == "ablations":
        run_ablations(args.pair_dir, protocol)
    elif args.command == "negative-cases":
        run_negative_cases(args.pair_dir, protocol)
    elif args.command == "robustness":
        run_robustness(args.pair_dir, protocol)
    else:
        finalize(args.pair_dir, args.phase, args.pair_ordinal, args.allow_invalid)


if __name__ == "__main__":
    main()

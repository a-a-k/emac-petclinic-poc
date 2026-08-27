#!/usr/bin/env python3
"""Summarize timing and SLI balance from an already completed artifact run."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def describe(values: list[float]) -> dict[str, object]:
    if not values:
        raise ValueError("cannot summarize an empty sample")
    ordered = sorted(float(value) for value in values)
    return {
        "n": len(ordered),
        "min": ordered[0],
        "median": statistics.median(ordered),
        "mean": statistics.fmean(ordered),
        "max": ordered[-1],
        "values": ordered,
    }


def summarize(
    root: Path,
    source_run_id: str,
    source_run_attempt: int,
    source_commit: str,
) -> dict[str, object]:
    pair_paths = sorted(root.glob("**/confirmatory/pair-*/pair-result.json"))
    if not pair_paths:
        raise ValueError(f"no confirmatory pair artifacts found under {root}")
    pairs = [(path, read_json(path)) for path in pair_paths]
    pair_ids = [str(pair["pairId"]) for _path, pair in pairs]
    if len(pair_ids) != len(set(pair_ids)):
        raise ValueError("duplicate pair IDs in source artifacts")
    invalid = [pair_id for pair_id, (_path, pair) in zip(pair_ids, pairs) if not pair["valid"]]
    if invalid:
        raise ValueError(f"detailed summary requires retained valid pairs: {invalid}")

    sli_values: dict[str, list[float]] = {
        service: [] for service in ("gateway", "customers", "visits")
    }
    timing_values: dict[str, list[float]] = {}
    condition_durations: list[float] = []
    trace_values: dict[str, list[float]] = {
        "querySeconds": [],
        "normalizeSeconds": [],
        "rawGzipWriteSeconds": [],
        "rawBytes": [],
        "normalizedJourneyTraces": [],
    }

    for pair_path, pair in pairs:
        pair_dir = pair_path.parent
        for service, values in sli_values.items():
            difference = pair["localSliBalance"]["services"][service][
                "absoluteDifference"
            ]
            if difference is None:
                raise ValueError(f"undefined {service} SLI difference in {pair['pairId']}")
            values.append(float(difference) * 100.0)

        for condition in ("control", "treatment"):
            condition_durations.append(
                float(pair["conditions"][condition]["durationSeconds"])
            )
            timing = read_json(pair_dir / condition / "model" / "pipeline-timing.json")
            for key, value in timing.items():
                if key == "schemaVersion":
                    continue
                timing_values.setdefault(key, []).append(float(value))
            delta = read_json(pair_dir / condition / "model" / "typed-delta.json")
            graph = delta["observedTraceGraph"]
            trace_timing = graph["timing"]
            for key in ("querySeconds", "normalizeSeconds", "rawGzipWriteSeconds", "rawBytes"):
                trace_values[key].append(float(trace_timing[key]))
            trace_values["normalizedJourneyTraces"].append(
                float(graph["normalizedJourneyTraces"])
            )

    return {
        "schemaVersion": "emac.detailed-artifact-summary/v1",
        "source": {
            "repository": "a-a-k/emac-petclinic-poc",
            "runId": source_run_id,
            "runAttempt": source_run_attempt,
            "experimentCommit": source_commit,
        },
        "pairCount": len(pairs),
        "conditionRunCount": len(pairs) * 2,
        "pairIds": sorted(pair_ids),
        "localAvailabilityAbsoluteDifferencePercentagePoints": {
            service: describe(values) for service, values in sli_values.items()
        },
        "pipelineTimingSeconds": {
            key: describe(values) for key, values in sorted(timing_values.items())
        },
        "conditionDurationSeconds": describe(condition_durations),
        "traceCollection": {
            key: describe(values) for key, values in trace_values.items()
        },
        "scope": {
            "pipelineTimingExcludesTelemetryCollection": True,
            "conditionDurationIncludesStackResetTrafficAndCollection": True,
            "valuesRetainedForIndependentRecalculation": True,
        },
    }


def write_markdown(path: Path, report: dict[str, object]) -> None:
    timings = report["pipelineTimingSeconds"]
    slis = report["localAvailabilityAbsoluteDifferencePercentagePoints"]
    lines = [
        "# Detailed artifact summary",
        "",
        f"- Source run: {report['source']['runId']}",
        f"- Experiment commit: `{report['source']['experimentCommit']}`",
        f"- Valid pairs: {report['pairCount']}",
        f"- Condition runs: {report['conditionRunCount']}",
        (
            "- EmaC pipeline seconds (median/max): "
            f"{timings['emacPipelineSeconds']['median']:.4f}/"
            f"{timings['emacPipelineSeconds']['max']:.4f}"
        ),
        (
            "- Manual composite seconds (median/max): "
            f"{timings['manualBaselineSeconds']['median']:.4f}/"
            f"{timings['manualBaselineSeconds']['max']:.4f}"
        ),
        "- Maximum paired local-availability difference (percentage points):",
        f"  - Gateway: {slis['gateway']['max']:.6f}",
        f"  - Customers: {slis['customers']['max']:.6f}",
        f"  - Visits: {slis['visits']['max']:.6f}",
        "",
        "Pipeline timing excludes trace collection; the full distributions remain in report.json.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--source-run-attempt", type=int, required=True)
    parser.add_argument("--source-commit", required=True)
    args = parser.parse_args()
    report = summarize(
        args.input,
        args.source_run_id,
        args.source_run_attempt,
        args.source_commit,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    write_markdown(args.output / "report.md", report)


if __name__ == "__main__":
    main()

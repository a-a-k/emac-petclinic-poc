#!/usr/bin/env python3
"""Aggregate independently executed paired blocks without selecting conditions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from run_experiment import summarize, write_json, write_markdown


def load_pairs(root: Path) -> list[dict[str, object]]:
    paths = sorted(root.glob("**/confirmatory/pair-*/pair-result.json"))
    pairs = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    ids = [str(pair["pairId"]) for pair in pairs]
    if len(ids) != len(set(ids)):
        duplicates = sorted({pair_id for pair_id in ids if ids.count(pair_id) > 1})
        raise ValueError(f"duplicate pair IDs in downloaded artifacts: {duplicates}")
    return sorted(pairs, key=lambda pair: int(str(pair["pairId"]).rsplit("-", 1)[1]))


def aggregate(pairs: list[dict[str, object]], required: int) -> dict[str, object]:
    valid = [pair for pair in pairs if bool(pair["valid"])]
    retained = valid[:required]
    report = summarize(retained, pairs)
    report["requestedConfirmatoryPairs"] = required
    report["validAttemptsAvailable"] = len(valid)
    report["unusedValidReplacementAttempts"] = max(0, len(valid) - required)
    report["retainedPairIds"] = [pair["pairId"] for pair in retained]
    report["allAttemptedPairIds"] = [pair["pairId"] for pair in pairs]
    report["complete"] = len(retained) == required
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--required", type=int, required=True)
    args = parser.parse_args()

    pairs = load_pairs(args.input)
    report = aggregate(pairs, args.required)
    args.output.mkdir(parents=True, exist_ok=True)
    write_json(args.output / "report.json", report)
    write_markdown(args.output / "report.md", report)
    if not report["complete"]:
        raise SystemExit(
            f"only {report['validAttemptsAvailable']} valid paired blocks are available; "
            f"{args.required} required"
        )


if __name__ == "__main__":
    main()

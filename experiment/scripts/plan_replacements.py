#!/usr/bin/env python3
"""Produce a bounded GitHub Actions matrix for invalid primary pair attempts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from aggregate_pairs import load_pairs


def replacement_matrix(valid: int, required: int, maximum: int) -> dict[str, object]:
    missing = max(0, required - valid)
    scheduled = min(missing, maximum)
    if scheduled == 0:
        return {"include": [{"ordinal": 0, "run": False}]}
    return {
        "include": [
            {"ordinal": required + offset, "run": True}
            for offset in range(1, scheduled + 1)
        ]
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--required", type=int, required=True)
    parser.add_argument("--maximum", type=int, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()

    pairs = load_pairs(args.input)
    valid = sum(bool(pair["valid"]) for pair in pairs)
    matrix = replacement_matrix(valid, args.required, args.maximum)
    with args.github_output.open("a", encoding="utf-8") as handle:
        handle.write(f"matrix={json.dumps(matrix, separators=(',', ':'))}\n")
        handle.write(f"valid_primary={valid}\n")


if __name__ == "__main__":
    main()

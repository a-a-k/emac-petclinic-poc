import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from summarize_artifact_details import summarize  # noqa: E402


class ArtifactSummaryTests(unittest.TestCase):
    def test_summary_reports_pipeline_timing_and_sli_balance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pair_dir = root / "run" / "confirmatory" / "pair-01"
            pair_dir.mkdir(parents=True)
            pair = {
                "pairId": "confirmatory-pair-01",
                "valid": True,
                "localSliBalance": {
                    "services": {
                        "gateway": {"absoluteDifference": 0.0001},
                        "customers": {"absoluteDifference": 0.0002},
                        "visits": {"absoluteDifference": 0.0003},
                    }
                },
                "conditions": {
                    "control": {"durationSeconds": 10.0},
                    "treatment": {"durationSeconds": 12.0},
                },
            }
            (pair_dir / "pair-result.json").write_text(json.dumps(pair), encoding="utf-8")
            for index, condition in enumerate(("control", "treatment"), start=1):
                model_dir = pair_dir / condition / "model"
                model_dir.mkdir(parents=True)
                timing = {
                    "schemaVersion": "emac.pipeline-timing/v1",
                    "emacPipelineSeconds": float(index),
                    "manualBaselineSeconds": float(index) / 2,
                }
                (model_dir / "pipeline-timing.json").write_text(
                    json.dumps(timing), encoding="utf-8"
                )
                delta = {
                    "observedTraceGraph": {
                        "normalizedJourneyTraces": 6000,
                        "timing": {
                            "querySeconds": 1.0,
                            "normalizeSeconds": 2.0,
                            "rawGzipWriteSeconds": 3.0,
                            "rawBytes": 4.0,
                        },
                    }
                }
                (model_dir / "typed-delta.json").write_text(
                    json.dumps(delta), encoding="utf-8"
                )

            report = summarize(root, "123", 1, "a" * 40)
            self.assertEqual(report["pairCount"], 1)
            self.assertEqual(report["conditionRunCount"], 2)
            self.assertEqual(
                report["pipelineTimingSeconds"]["emacPipelineSeconds"]["median"],
                1.5,
            )
            self.assertAlmostEqual(
                report["localAvailabilityAbsoluteDifferencePercentagePoints"][
                    "visits"
                ]["max"],
                0.03,
            )


if __name__ == "__main__":
    unittest.main()

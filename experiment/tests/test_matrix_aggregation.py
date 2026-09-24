import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from aggregate_pairs import aggregate  # noqa: E402
from plan_replacements import replacement_matrix  # noqa: E402


def pair(ordinal: int, valid: bool) -> dict[str, object]:
    control = {
        "condition": "control",
        "discovery": {"stateChanges": [], "operatorBindings": []},
        "validity": {
            "checks": {
                "metricsOnlyNoFalseStateDelta": True,
                "tracesOnlyNoFalseSuppression": True,
                "fullFusionNoFalseDelta": True,
            }
        },
        "comparison": {
            "owner-history": {
                "modelDiscoveredAbsoluteError": 0.0,
                "manualDynamicAbsoluteError": 0.0,
                "frozenAbsoluteError": 0.0,
                "frozenTargetSideError": False,
            },
            "owner-only": {},
        },
        "robustness": {
            "traceSampling": {
                "0.1": {"discovery": {"status": "no-drift", "falseBinding": False}},
                "0.01": {"discovery": {"status": "no-drift", "falseBinding": False}},
            }
        },
    }
    treatment = {
        **control,
        "condition": "treatment",
        "validity": {
            "checks": {
                "exactStateDeltaRecovery": True,
                "uniqueOperatorEdgeBindingRecovery": True,
                "metricsOnlyStateRecovery": True,
                "tracesOnlyEdgeRecovery": True,
                "fullFusionTypedRecovery": True,
                "ambiguityReplayRefusesBinding": True,
                "contradictionReplayRefusesBinding": True,
            }
        },
        "robustness": {
            "traceSampling": {
                "0.1": {"discovery": {"status": "recovered", "falseBinding": False}},
                "0.01": {"discovery": {"status": "unresolved", "falseBinding": False}},
            }
        },
    }
    return {
        "pairId": f"confirmatory-pair-{ordinal:02d}",
        "valid": valid,
        "secondaryAnalysisStatus": "complete",
        "conditions": {"control": control, "treatment": treatment},
    }


class MatrixAggregationTests(unittest.TestCase):
    def test_pending_secondary_blocks_completion_without_excluding_or_replacing_pair(self) -> None:
        pending = copy.deepcopy(pair(1, True))
        pending["secondaryAnalysisStatus"] = "pending"
        report = aggregate([pending], 1)
        self.assertFalse(report["complete"])
        self.assertEqual(report["confirmatoryPairsRetained"], 1)
        self.assertEqual(report["secondaryAnalysisIncompletePairs"], 1)
        self.assertEqual(report["incompleteSecondaryPairIds"], [pending["pairId"]])
        self.assertEqual(report["exactTreatmentModelRecovery"]["denominator"], 1)
        self.assertEqual(
            replacement_matrix(report["validAttemptsAvailable"], 1, 2, attempted=1),
            {"include": [{"ordinal": 0, "run": False}]},
        )

    def test_missing_or_failed_secondary_status_is_incomplete(self) -> None:
        for status in (None, "failed"):
            with self.subTest(status=status):
                record = copy.deepcopy(pair(1, True))
                if status is None:
                    del record["secondaryAnalysisStatus"]
                else:
                    record["secondaryAnalysisStatus"] = status
                for row in record["conditions"].values():
                    row["validity"]["policy"] = "measurement-and-intervention-only/v1"
                report = aggregate([record], 1)
                self.assertFalse(report["complete"])
                self.assertEqual(report["secondaryAnalysisIncompletePairs"], 1)

    def test_completed_method_failure_is_a_retained_completed_result(self) -> None:
        record = copy.deepcopy(pair(1, True))
        record["conditions"]["treatment"]["validity"]["methodChecks"] = {
            "exactStateDeltaRecovery": False,
            "uniqueOperatorEdgeBindingRecovery": False,
            "ambiguityReplayRefusesBinding": False,
        }
        report = aggregate([record], 1)
        self.assertTrue(report["complete"])
        self.assertEqual(report["exactTreatmentModelRecovery"], {"numerator": 0, "denominator": 1})
        self.assertEqual(report["incompleteSecondaryPairIds"], [])

    def test_legacy_inline_results_require_all_secondary_reports(self) -> None:
        record = copy.deepcopy(pair(1, True))
        del record["secondaryAnalysisStatus"]
        for row in record["conditions"].values():
            row["ablations"] = {"fullFusion": {"status": "no-drift"}}
            row["negativeCases"] = {"ambiguityReplay": {"status": "not-applicable"}}
        report = aggregate([record], 1)
        self.assertTrue(report["complete"])
        self.assertEqual(report["eligibilityPolicy"], "legacy-recorded-validity")
        del record["conditions"]["treatment"]["negativeCases"]
        self.assertFalse(aggregate([record], 1)["complete"])

    def test_cli_writes_incomplete_report_and_fails_until_secondary_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result_path = root / "input" / "confirmatory" / "pair-01" / "pair-result.json"
            result_path.parent.mkdir(parents=True)
            record = copy.deepcopy(pair(1, True))
            record["secondaryAnalysisStatus"] = "pending"
            result_path.write_text(json.dumps(record), encoding="utf-8")
            command = [sys.executable, str(SCRIPTS / "aggregate_pairs.py"),
                       "--input", str(root / "input"), "--output", str(root / "report"), "--required", "1"]
            incomplete = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(incomplete.returncode, 0)
            self.assertIn("incomplete secondary analysis", incomplete.stderr)
            report = json.loads((root / "report" / "report.json").read_text())
            self.assertFalse(report["complete"])
            markdown = (root / "report" / "report.md").read_text()
            self.assertIn("Aggregate complete: False", markdown)
            self.assertIn(record["pairId"], markdown)
            record["secondaryAnalysisStatus"] = "complete"
            record["conditions"]["treatment"]["validity"]["methodChecks"] = {"fullFusionTypedRecovery": False}
            result_path.write_text(json.dumps(record), encoding="utf-8")
            completed = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_aggregate_retains_first_required_valid_pairs(self) -> None:
        report = aggregate([pair(1, True), pair(2, False), pair(21, True)], 2)
        self.assertTrue(report["complete"])
        self.assertEqual(
            report["retainedPairIds"],
            ["confirmatory-pair-01", "confirmatory-pair-21"],
        )
        self.assertEqual(report["invalidAttemptsRetained"], 1)
        self.assertEqual(
            report["evidenceSourceAblations"]["treatments"][
                "ambiguityReplayRefusesBinding"
            ],
            {"numerator": 2, "denominator": 2},
        )
        self.assertEqual(
            report["robustness"]["traceSampling"]["0.1"]["treatments"],
            {"recovered": 2, "unresolved": 0, "falseBindings": 0, "denominator": 2},
        )

    def test_replacements_are_bounded_and_zero_uses_noop_matrix(self) -> None:
        self.assertEqual(
            replacement_matrix(18, 20, 2),
            {"include": [{"ordinal": 21, "run": True}, {"ordinal": 22, "run": True}]},
        )
        self.assertEqual(
            replacement_matrix(20, 20, 2),
            {"include": [{"ordinal": 0, "run": False}]},
        )


if __name__ == "__main__":
    unittest.main()

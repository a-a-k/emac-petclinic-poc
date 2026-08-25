import sys
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
        "conditions": {"control": control, "treatment": treatment},
    }


class MatrixAggregationTests(unittest.TestCase):
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

import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from secondary_analysis import secondary_checks  # noqa: E402


class SecondaryAnalysisTests(unittest.TestCase):
    def test_treatment_secondary_checks_accept_safe_degradation(self) -> None:
        result = {"discovery": {"runtimeParameters": {"q": 0.99}}}
        assignment = {"minorityInstanceId": "opaque-minority"}
        ablations = {
            "sourceIsolation": {"verified": True},
            "metricsOnly": {
                "edgeBinding": {"status": "unresolved"},
                "stateChanges": [
                    {
                        "serviceInstanceId": "opaque-minority",
                        "after": "OPEN",
                    }
                ],
            },
            "tracesOnly": {
                "operator": {"status": "unresolved"},
                "suppression": {
                    "status": "identified",
                    "affectedEdge": {
                        "serviceInstanceId": "opaque-minority",
                        "sourceService": "api-gateway",
                        "targetService": "visits-service",
                    },
                },
            },
            "fullFusion": {"status": "typed-delta", "q": 0.99},
        }
        negatives = {
            "ambiguityReplay": {
                "status": "binding-refused",
                "reconciliationStatus": "unresolved",
                "compilationStatus": "UNASSESSABLE",
                "matchingEdgeCandidates": ["edge-a", "edge-b"],
                "emittedBindings": [],
            },
            "contradictionReplay": {
                "status": "binding-refused",
                "reconciliationStatus": "contradictory",
                "compilationStatus": "UNASSESSABLE",
                "emittedBindings": [],
            },
        }
        robustness = {
            "traceSampling": {
                "0.1": {"discovery": {"status": "recovered", "falseBinding": False}},
                "0.01": {"discovery": {"status": "unresolved", "falseBinding": False}},
            },
            "identityRedaction": {
                "globalQ": 0.99,
                "globalAffectedEdge": None,
                "specificInstance": "unresolved",
            },
        }
        checks = secondary_checks(
            "treatment", result, assignment, ablations, negatives, robustness
        )
        self.assertTrue(all(checks.values()), checks)


if __name__ == "__main__":
    unittest.main()

"""Regression examples for the r13 review findings, using real pipeline inputs."""

from __future__ import annotations

import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

EXPERIMENT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EXPERIMENT / "scripts"))

from aggregate_pairs import aggregate
from apply_model_delta import apply_delta, validate_effective_lineage
from artifact_integrity import IntegrityError, seal_artifact
from compile_journeys import compile_estimates, validate_compiled_estimates
from discover_model import discover_bootstrap, discover_delta
from manual_composite import evaluate as manual_composite
from negative_cases import evaluate as negative_cases
from plan_replacements import replacement_matrix
from reconcile_model_delta import reconcile
from run_experiment import compare_assessments, condition_validity, run_discovery_pipeline
from secondary_analysis import finalize
from test_discovery import INSTANCE_ONE, INSTANCE_TWO, metrics, trace_summary, write_evidence
from test_matrix_aggregation import pair


class SubmissionRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.contract_path = EXPERIMENT / "journey-contract.json"
        self.adapters_path = EXPERIMENT / "operator-adapters.json"
        self.contract = json.loads(self.contract_path.read_text())
        self.protocol = json.loads((EXPERIMENT / "protocol.json").read_text())
        bootstrap = self.root / "bootstrap"
        write_evidence(
            bootstrap,
            metrics(INSTANCE_ONE, 0, 0, 0, "CLOSED"),
            metrics(INSTANCE_ONE, 100, 0, 0, "CLOSED"),
            metrics(INSTANCE_TWO, 0, 0, 0, "CLOSED"),
            metrics(INSTANCE_TWO, 100, 0, 0, "CLOSED"),
            trace_summary(100, 100, 100, 100), 200,
        )
        self.base = discover_bootstrap(bootstrap, self.contract_path, self.adapters_path)
        self.base_path = self.root / "base.json"
        self.base_path.write_text(json.dumps(self.base))

    def evidence(self, name="evidence", *, first=9900, second=100, rejected=False, completed=10000):
        destination = self.root / name
        write_evidence(
            destination,
            metrics(INSTANCE_ONE, 0, 0, 0, "CLOSED"),
            metrics(INSTANCE_ONE, 0 if rejected else first, 0, first if rejected else 0, "OPEN" if rejected else "CLOSED"),
            metrics(INSTANCE_TWO, 0, 0, 0, "CLOSED"),
            metrics(INSTANCE_TWO, 0, 0, second, "OPEN"),
            trace_summary(0 if rejected else first, 0, first, second), completed,
        )
        return destination

    def pipeline(self, evidence):
        candidate = discover_delta(self.base_path, evidence, self.adapters_path, 0.01)
        decision = reconcile(self.base, candidate)
        effective = apply_delta(self.base, candidate, decision)
        validate_effective_lineage(effective, self.base, candidate, decision)
        compiled = compile_estimates(effective, self.contract)
        validate_compiled_estimates(compiled, effective, self.contract)
        return candidate, decision, effective, compiled

    def test_operator_ambiguity_and_absence_reach_versioned_unassessable(self):
        for mode in ("two-matches", "no-match", "no-supported-operator"):
            with self.subTest(mode=mode):
                evidence = self.evidence(mode)
                for metric_path in (evidence / "snapshots").glob("*.prom"):
                    original = metric_path.read_text()
                    if mode == "two-matches":
                        modified = original + original.replace('name="getOwnerDetails"', 'name="otherOperation"')
                    elif mode == "no-supported-operator":
                        modified = "# no supported operator metrics observed\n"
                    else:
                        modified = original.replace(" 9900\n", " 9700\n")
                    metric_path.write_text(modified)
                candidate, decision, effective, compiled = self.pipeline(evidence)
                self.assertIsNone(candidate["selectedOperator"])
                self.assertIsNone(candidate["runtimeParameters"])
                self.assertEqual(decision["status"], "unresolved")
                self.assertIsNone(effective["appliedDeltaVersion"])
                self.assertEqual(compiled["status"], "UNASSESSABLE")
                self.assertNotIn("modelDiscoveredEstimate", compiled["estimates"]["owner-history"])
                replay = negative_cases(self.base_path, evidence, self.contract_path, self.adapters_path, 0.01)
                self.assertEqual(replay["ambiguityReplay"]["status"], "not-evaluable")
                if mode == "two-matches":
                    orchestrated = run_discovery_pipeline(self.root / "orchestrated", evidence, self.base_path, self.protocol)
                    self.assertEqual(orchestrated[2]["status"], "UNASSESSABLE")

    def test_tolerance_does_not_admit_probability_above_one(self):
        evidence = self.evidence(first=100, second=1, completed=100)
        candidate, decision, effective, compiled = self.pipeline(evidence)
        self.assertEqual(candidate["runtimeParameters"]["A_P"], 1.01)
        self.assertEqual(decision["status"], "contradictory")
        self.assertIn("decisions-exceed-eligible-requests", decision["reasons"])
        self.assertEqual(compiled["status"], "UNASSESSABLE")

    def test_all_rejected_uses_declared_fallback_without_inventing_conditional_success(self):
        evidence = self.evidence(rejected=True)
        candidate, decision, effective, compiled = self.pipeline(evidence)
        self.assertEqual(decision["status"], "identified")
        self.assertIsNone(candidate["runtimeParameters"]["A_V"])
        self.assertEqual(len(candidate["bindings"]), 2)
        self.assertEqual(compiled["estimates"]["owner-history"]["modelDiscoveredEstimate"], 0.0)
        self.assertEqual(compiled["estimates"]["owner-only"]["modelDiscoveredEstimate"], 1.0)
        self.assertIsNone(compiled["estimates"]["owner-history"]["frozenModelEstimate"])
        orchestrated = run_discovery_pipeline(self.root / "all-rejected", evidence, self.base_path, self.protocol)
        self.assertEqual(orchestrated[3]["estimates"]["owner-history"], 0.0)

    def test_unassessable_is_retained_without_a_numeric_error(self):
        evidence = self.evidence()
        candidate, decision, effective, compiled = self.pipeline(evidence)
        for raw in (evidence / "snapshots").glob("*.prom"):
            original = raw.read_text()
            raw.write_text(original + original.replace('name="getOwnerDetails"', 'name="otherOperation"'))
        _, _, _, unavailable = self.pipeline(evidence)
        comparison = compare_assessments(
            unavailable, {"estimates": {"owner-history": 0.99, "owner-only": 1.0}},
            {"oracle": {"owner-history": {"reliability": 0.99}, "owner-only": {"reliability": 1.0}}}, self.contract,
        )
        self.assertIsNone(comparison["owner-history"]["modelDiscoveredAbsoluteError"])
        self.assertIsNone(comparison["owner-history"]["modelDiscoveredTargetSideError"])
        second = pair(2, True)
        second["conditions"]["treatment"]["comparison"] = comparison
        second["conditions"]["treatment"]["validity"]["checks"]["uniqueOperatorEdgeBindingRecovery"] = False
        second["conditions"]["control"]["discovery"] = {"stateChanges": [{"after": "OPEN"}], "operatorBindings": []}
        report = aggregate([pair(1, True), second], 2)
        self.assertEqual(report["exactTreatmentModelRecovery"], {"numerator": 1, "denominator": 2})
        self.assertEqual(report["falseDiscoveryInControls"], {"numerator": 1, "denominator": 2})
        self.assertEqual(report["ownerHistoryAssessmentCoverage"]["modelDiscovery"], {"assessed": 3, "unassessable": 1, "denominator": 4})
        self.assertEqual(replacement_matrix(report["validAttemptsAvailable"], 2, 2), {"include": [{"ordinal": 0, "run": False}]})

    def test_missing_child_spans_preserve_unassessable_baseline_and_primary(self):
        evidence = self.evidence()
        trace_path = evidence / "traces.normalized.json"
        graph = json.loads(trace_path.read_text())
        for row in graph["byInstance"].values():
            row["edges"].pop("api-gateway=>visits-service", None)
        graph["byInstance"][INSTANCE_TWO]["edges"].pop("api-gateway=>customers-service")
        trace_path.write_text(json.dumps(graph))
        condition_dir = self.root / "missing-child-spans"
        delta, effective, compiled, manual, freeze_ns = run_discovery_pipeline(
            condition_dir, evidence, self.base_path, self.protocol
        )
        self.assertEqual(effective["reconciliationStatus"], "unresolved")
        self.assertEqual(compiled["status"], "UNASSESSABLE")
        self.assertEqual(manual["assessmentStatus"], "UNASSESSABLE")
        self.assertEqual(manual["reasons"], ["manual-primary-edge-absent-despite-permitted-calls"])
        self.assertTrue((condition_dir / "model" / "pre-outcome-freeze.json").is_file())
        self.assertGreater(freeze_ns, 0)
        load = {
            "requested": 10000, "completed": 10000, "byGatewaySlot": {"A": 9900, "B": 100},
            "snapshotDir": str(evidence / "snapshots"), "runId": "fixture-window",
        }
        assignment = {"logicalSlots": {"A": INSTANCE_ONE, "B": INSTANCE_TWO}, "minoritySlot": "B", "minorityInstanceId": INSTANCE_TWO}
        manipulation = {
            "minorityGateway": {"finalState": "OPEN", "decisions": 120, "permittedFailed": 100, "notPermitted": 20},
            "visitsAfterFaultDisabled": {"healthy": True},
        }
        with patch("run_experiment.SLOT_METRIC_SOURCES", {"A": "source-one", "B": "source-two"}):
            validity = condition_validity(
                "treatment", self.base, 1, 2, 3, 4, manipulation, load, load,
                delta, effective, compiled, freeze_ns, freeze_ns + 1, 10.0, assignment, self.protocol,
            )
        self.assertTrue(validity["valid"], validity)
        comparison = compare_assessments(
            compiled, manual,
            {"oracle": {"owner-history": {"reliability": 0.99}, "owner-only": {"reliability": 1.0}}},
            self.contract,
        )
        for row in comparison.values():
            self.assertIsNone(row["modelDiscoveredAbsoluteError"])
            self.assertIsNone(row["manualDynamicAbsoluteError"])
            self.assertIsNone(row["manualDynamicTargetSideError"])
            self.assertEqual(row["manualDynamicAssessmentStatus"], "UNASSESSABLE")
            self.assertEqual(row["manualDynamicReasons"], manual["reasons"])
        record = copy.deepcopy(pair(1, True))
        record["conditions"]["treatment"]["comparison"] = comparison
        record["conditions"]["treatment"]["validity"] = validity
        report = aggregate([record], 1)
        self.assertTrue(report["complete"])
        self.assertEqual(report["exactTreatmentModelRecovery"], {"numerator": 0, "denominator": 1})
        for method in ("modelDiscovery", "manualDynamic"):
            self.assertEqual(report["ownerHistoryAssessmentCoverage"][method],
                             {"assessed": 1, "unassessable": 1, "denominator": 2})

    def test_manual_baseline_refuses_wrong_role_evidence_but_rejects_invalid_declaration(self):
        evidence = self.evidence()
        trace_path = evidence / "traces.normalized.json"
        graph = json.loads(trace_path.read_text())
        graph["byInstance"][INSTANCE_ONE]["edges"]["api-gateway=>visits-service"]["operations"] = ["/unrelated"]
        trace_path.write_text(json.dumps(graph))
        manual_model = json.loads((EXPERIMENT / "manual-composite.json").read_text())
        result = manual_composite(evidence, self.contract, manual_model, self.adapters_path)
        self.assertEqual(result["assessmentStatus"], "UNASSESSABLE")
        self.assertEqual(result["reasons"], ["manual-primary-edge-does-not-satisfy-declared-role"])
        manual_model["fallback"] = "undeclared-fallback"
        with self.assertRaisesRegex(ValueError, "manual fallback does not match"):
            manual_composite(evidence, self.contract, manual_model, self.adapters_path)

    def test_measurement_validity_does_not_depend_on_discovery_answer(self):
        evidence = self.evidence()
        candidate, decision, effective, compiled = self.pipeline(evidence)
        wrong = copy.deepcopy(candidate)
        wrong.update(selectedOperator=None, stateChanges=[], bindings=[])
        load = {
            "requested": 10000, "completed": 10000, "byGatewaySlot": {"A": 9900, "B": 100},
            "snapshotDir": str(evidence / "snapshots"), "runId": "fixture-window",
        }
        assignment = {"logicalSlots": {"A": INSTANCE_ONE, "B": INSTANCE_TWO}, "minoritySlot": "B", "minorityInstanceId": INSTANCE_TWO}
        manipulation = {
            "minorityGateway": {"finalState": "OPEN", "decisions": 120, "permittedFailed": 100, "notPermitted": 20},
            "visitsAfterFaultDisabled": {"healthy": True},
        }
        with patch("run_experiment.SLOT_METRIC_SOURCES", {"A": "source-one", "B": "source-two"}):
            validity = condition_validity(
                "treatment", self.base, 1, 2, 3, 4, manipulation, load, load,
                wrong, effective, compiled, 5, 6, 10.0, assignment, self.protocol,
            )
        self.assertTrue(validity["valid"], validity)
        self.assertFalse(validity["methodChecks"]["exactStateDeltaRecovery"])
        self.assertFalse(validity["methodChecks"]["uniqueOperatorEdgeBindingRecovery"])

    def test_failed_secondary_check_does_not_exclude_pair(self):
        pair_dir = self.root / "confirmatory" / "pair-01"
        pair_dir.mkdir(parents=True)
        record = pair(1, True)
        record["localSliBalance"] = {"valid": True}
        for condition, result in record["conditions"].items():
            result["validity"]["valid"] = True
            for rel in ("ablations/evidence-source-ablation.json", "negative-cases/report.json", "robustness/report.json"):
                dest = pair_dir / condition / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text("{}")
        (pair_dir / "pair-result.json").write_text(json.dumps(record))
        truth = self.root / "ground-truth"
        truth.mkdir()
        (truth / "runtime-assignment.json").write_text("{}")
        with patch("secondary_analysis.secondary_checks", return_value={"fullFusionTypedRecovery": False}):
            finalize(pair_dir, "confirmatory", 1, False)
        updated = json.loads((pair_dir / "pair-result.json").read_text())
        self.assertTrue(updated["valid"])
        self.assertFalse(updated["conditions"]["treatment"]["validity"]["methodChecks"]["fullFusionTypedRecovery"])

    def test_nonfinite_resealed_probability_is_rejected(self):
        _, _, effective, _ = self.pipeline(self.evidence())
        for value in (math.nan, math.inf):
            with self.subTest(value=value):
                changed = copy.deepcopy(effective)
                changed["runtimeReliability"]["q"] = value
                changed = seal_artifact(changed, "modelVersion")
                with self.assertRaises(IntegrityError):
                    compile_estimates(changed, self.contract)

    def test_missing_unclassified_attempt_cannot_be_replaced_or_hidden(self):
        self.assertEqual(replacement_matrix(19, 20, 2, attempted=19), {"include": [{"ordinal": 0, "run": False}]})
        report = aggregate([pair(1, True), pair(3, True)], 2)
        self.assertFalse(report["complete"])
        self.assertEqual(report["missingPrimaryPairIds"], ["confirmatory-pair-02"])


if __name__ == "__main__":
    unittest.main()

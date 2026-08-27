from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


EXPERIMENT = Path(__file__).resolve().parents[1]
SCRIPTS = EXPERIMENT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from apply_model_delta import apply_delta  # noqa: E402
from artifact_integrity import IntegrityError, seal_artifact  # noqa: E402
from collect_trace_evidence import collect, normalize_trace  # noqa: E402
from compile_journeys import compile_estimates  # noqa: E402
from discover_model import discover_bootstrap, discover_delta, trace_graph  # noqa: E402
from evidence_ablations import evaluate as evaluate_ablations  # noqa: E402
from evidence import (  # noqa: E402
    adapter_operator_counts,
    adapter_operator_state,
    discover_metric_identity,
    discover_operator_names,
    http_server_availability,
    parse_prometheus,
)
from manual_composite import evaluate as manual_evaluate  # noqa: E402
from negative_cases import evaluate as evaluate_negative_cases  # noqa: E402
from reconcile_model_delta import reconcile  # noqa: E402
from run_experiment import run_discovery_pipeline  # noqa: E402
from robustness_study import identity_redaction, rate_binding  # noqa: E402


INSTANCE_ONE = "instance-18f8d5aa87d20816"
INSTANCE_TWO = "instance-81efda7b3446b8f2"


def metrics(
    instance: str,
    successful: int,
    failed: int,
    not_permitted: int,
    state: str,
) -> str:
    tag = f'replica="{instance}",service="api-gateway"'
    return f"""
resilience4j_circuitbreaker_calls_seconds_count{{kind="successful",name="getOwnerDetails",{tag}}} {successful}
resilience4j_circuitbreaker_calls_seconds_count{{kind="failed",name="getOwnerDetails",{tag}}} {failed}
resilience4j_circuitbreaker_calls_seconds_count{{kind="ignored",name="getOwnerDetails",{tag}}} 0
resilience4j_circuitbreaker_not_permitted_calls_total{{name="getOwnerDetails",{tag}}} {not_permitted}
resilience4j_circuitbreaker_state{{name="getOwnerDetails",state="closed",{tag}}} {1 if state == 'CLOSED' else 0}
resilience4j_circuitbreaker_state{{name="getOwnerDetails",state="open",{tag}}} {1 if state == 'OPEN' else 0}
"""


def trace_summary(first_visits: int, second_visits: int, first_total: int, second_total: int) -> dict[str, object]:
    def instance(total: int, visits: int) -> dict[str, object]:
        edges = {
            "api-gateway=>customers-service": {
                "edgeId": "api-gateway=>customers-service",
                "sourceService": "api-gateway",
                "targetService": "customers-service",
                "executions": total,
                "operations": ["/owners/6"],
            }
        }
        if visits:
            edges["api-gateway=>visits-service"] = {
                "edgeId": "api-gateway=>visits-service",
                "sourceService": "api-gateway",
                "targetService": "visits-service",
                "executions": visits,
                "operations": ["/pets/visits"],
            }
        return {"journeyTraces": total, "edges": edges}

    return {
        "schemaVersion": "emac.discovered-trace-graph/v2",
        "returnedRawTraces": first_total + second_total,
        "normalizedJourneyTraces": first_total + second_total,
        "timing": {},
        "byInstance": {
            INSTANCE_ONE: instance(first_total, first_visits),
            INSTANCE_TWO: instance(second_total, second_visits),
        },
        "traces": [],
    }


def write_evidence(
    root: Path,
    first_start: str,
    first_end: str,
    second_start: str,
    second_end: str,
    traces: dict[str, object],
    completed: int,
) -> None:
    snapshots = root / "snapshots"
    snapshots.mkdir(parents=True)
    for source, phase, content in (
        ("source-one", "start", first_start),
        ("source-one", "end", first_end),
        ("source-two", "start", second_start),
        ("source-two", "end", second_end),
    ):
        (snapshots / f"{source}.{phase}.prom").write_text(content, encoding="utf-8")
    (root / "traces.normalized.json").write_text(json.dumps(traces), encoding="utf-8")
    (root / "load-summary.json").write_text(
        json.dumps({"completed": completed}), encoding="utf-8"
    )


def write_ablation_inputs(
    root: Path, base: dict[str, object], evidence_dir: Path
) -> tuple[Path, Path, Path, Path]:
    metric_base = root / "metrics-only" / "bootstrap-operators.json"
    metric_evidence = root / "metrics-only" / "evidence"
    trace_base = root / "traces-only" / "bootstrap-interactions.json"
    trace_evidence = root / "traces-only" / "evidence"
    metric_base.parent.mkdir(parents=True)
    trace_base.parent.mkdir(parents=True)
    metric_evidence.mkdir(parents=True)
    trace_evidence.mkdir(parents=True)
    metric_base.write_text(
        json.dumps(
            {
                "schemaVersion": "emac.metrics-only-bootstrap-view/v1",
                "modelVersion": base["modelVersion"],
                "operators": base["operators"],
            }
        ),
        encoding="utf-8",
    )
    trace_base.write_text(
        json.dumps(
            {
                "schemaVersion": "emac.traces-only-bootstrap-view/v1",
                "modelVersion": base["modelVersion"],
                "interactions": base["interactions"],
            }
        ),
        encoding="utf-8",
    )
    shutil.copytree(evidence_dir / "snapshots", metric_evidence / "snapshots")
    shutil.copy2(evidence_dir / "load-summary.json", metric_evidence / "load-summary.json")
    shutil.copy2(
        evidence_dir / "traces.normalized.json",
        trace_evidence / "traces.normalized.json",
    )
    return metric_base, metric_evidence, trace_base, trace_evidence


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.contract_path = EXPERIMENT / "journey-contract.json"
        self.adapters_path = EXPERIMENT / "operator-adapters.json"
        self.contract = json.loads(self.contract_path.read_text(encoding="utf-8"))
        self.adapter = json.loads(self.adapters_path.read_text(encoding="utf-8"))["adapters"][0]

    def test_adapter_discovers_identity_operator_state_and_counts(self) -> None:
        start = parse_prometheus(metrics(INSTANCE_ONE, 0, 0, 0, "CLOSED"))
        end = parse_prometheus(metrics(INSTANCE_ONE, 99, 0, 1, "OPEN"))
        self.assertEqual(
            discover_metric_identity(end, self.adapter),
            {"serviceName": "api-gateway", "serviceInstanceId": INSTANCE_ONE},
        )
        self.assertEqual(discover_operator_names(end, self.adapter), ["getOwnerDetails"])
        self.assertEqual(adapter_operator_state(end, self.adapter, "getOwnerDetails"), "OPEN")
        counts = adapter_operator_counts(start, end, self.adapter, "getOwnerDetails")
        self.assertEqual(counts["permitted"], 99)
        self.assertEqual(counts["notPermitted"], 1)

    def test_local_availability_discovers_custom_timed_metric_family(self) -> None:
        start = parse_prometheus(
            'petclinic_owner_seconds_count{outcome="SUCCESS",status="200",uri="/owners/{ownerId}"} 10\n'
        )
        end = parse_prometheus(
            'petclinic_owner_seconds_count{outcome="SUCCESS",status="200",uri="/owners/{ownerId}"} 30\n'
            'http_server_requests_seconds_count{outcome="SUCCESS",status="200",uri="/actuator/health"} 50\n'
        )
        result = http_server_availability(start, end, "/owners/")
        self.assertEqual(result["metricName"], "petclinic_owner_seconds_count")
        self.assertEqual(result["total"], 20)
        self.assertEqual(result["availability"], 1.0)

    def test_trace_normalizer_discovers_targets_from_span_parentage(self) -> None:
        trace = {
            "traceID": "trace-1",
            "processes": {
                "p0": {
                    "serviceName": "api-gateway",
                    "tags": [{"key": "service.instance.id", "value": INSTANCE_ONE}],
                },
                "p1": {"serviceName": "downstream-alpha", "tags": []},
                "p2": {"serviceName": "downstream-beta", "tags": []},
            },
            "spans": [
                {
                    "spanID": "root",
                    "processID": "p0",
                    "operationName": "GET /api/gateway/owners/{ownerId}",
                    "tags": [],
                    "references": [],
                },
                {
                    "spanID": "client-a",
                    "processID": "p0",
                    "operationName": "GET",
                    "tags": [{"key": "span.kind", "value": "client"}],
                    "references": [{"refType": "CHILD_OF", "spanID": "root"}],
                },
                {
                    "spanID": "server-a",
                    "processID": "p1",
                    "operationName": "GET /a",
                    "tags": [],
                    "references": [{"refType": "CHILD_OF", "spanID": "client-a"}],
                },
                {
                    "spanID": "client-b",
                    "processID": "p0",
                    "operationName": "GET",
                    "tags": [{"key": "span.kind", "value": "client"}],
                    "references": [{"refType": "CHILD_OF", "spanID": "root"}],
                },
                {
                    "spanID": "server-b",
                    "processID": "p2",
                    "operationName": "GET /b",
                    "tags": [],
                    "references": [{"refType": "CHILD_OF", "spanID": "client-b"}],
                },
            ],
        }
        normalized = normalize_trace(trace, self.contract)
        self.assertEqual(normalized["entryInstance"], INSTANCE_ONE)
        self.assertEqual(
            [edge["targetService"] for edge in normalized["edges"]],
            ["downstream-alpha", "downstream-beta"],
        )
        trace["spans"][0]["tags"].append(
            {
                "key": "http.request.header.x_experiment_run_id",
                "value": ["pair-01-treatment-evidence"],
            }
        )
        self.assertIsNotNone(
            normalize_trace(trace, self.contract, "pair-01-treatment-evidence")
        )
        self.assertIsNone(normalize_trace(trace, self.contract, "adjacent-window"))

    @patch("collect_trace_evidence.query_jaeger")
    def test_trace_collection_is_chunked_and_deduplicates_boundaries(self, query) -> None:
        trace = {
            "traceID": "boundary-trace",
            "processes": {
                "p0": {
                    "serviceName": "api-gateway",
                    "tags": [{"key": "service.instance.id", "value": INSTANCE_ONE}],
                }
            },
            "spans": [
                {
                    "spanID": "root",
                    "processID": "p0",
                    "operationName": "GET /api/gateway/owners/{ownerId}",
                    "tags": [],
                    "references": [],
                }
            ],
        }
        payload = {"data": [trace]}
        raw = json.dumps(payload).encode("utf-8")
        query.side_effect = [
            (payload, raw, 0.1),
            (payload, raw, 0.1),
            ({"data": []}, b'{"data":[]}', 0.1),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            result = collect(
                "http://unused",
                0,
                20_000_000,
                Path(temporary),
                100,
                self.contract,
                timeout=1,
                chunk_seconds=10,
            )
            self.assertEqual(query.call_count, 3)
            self.assertEqual(
                [(call.args[2], call.args[3]) for call in query.call_args_list],
                [
                    (0, 9_999_999),
                    (10_000_000, 19_999_999),
                    (20_000_000, 20_000_000),
                ],
            )
            self.assertEqual(result["returnedRawTraces"], 2)
            self.assertEqual(result["normalizedJourneyTraces"], 1)
            self.assertEqual(result["timing"]["chunkCount"], 3)
            self.assertEqual(len(list((Path(temporary) / "traces.raw.chunks").glob("*.gz"))), 3)

    def test_complete_pipeline_rejects_integrity_and_semantic_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bootstrap_dir = root / "bootstrap"
            write_evidence(
                bootstrap_dir,
                metrics(INSTANCE_ONE, 0, 0, 0, "CLOSED"),
                metrics(INSTANCE_ONE, 100, 0, 0, "CLOSED"),
                metrics(INSTANCE_TWO, 0, 0, 0, "CLOSED"),
                metrics(INSTANCE_TWO, 100, 0, 0, "CLOSED"),
                trace_summary(100, 100, 100, 100),
                200,
            )
            base = discover_bootstrap(bootstrap_dir, self.contract_path, self.adapters_path)
            base_path = root / "bootstrap-model.json"
            base_path.write_text(json.dumps(base), encoding="utf-8")

            evidence_dir = root / "evidence"
            write_evidence(
                evidence_dir,
                metrics(INSTANCE_ONE, 0, 0, 0, "CLOSED"),
                metrics(INSTANCE_ONE, 9900, 0, 0, "CLOSED"),
                metrics(INSTANCE_TWO, 0, 0, 0, "OPEN"),
                metrics(INSTANCE_TWO, 0, 0, 100, "OPEN"),
                trace_summary(9900, 0, 9900, 100),
                10000,
            )
            delta = discover_delta(base_path, evidence_dir, self.adapters_path, 0.01)
            self.assertEqual(delta["selectedOperator"], "getOwnerDetails")
            self.assertEqual(delta["stateChanges"][0]["serviceInstanceId"], INSTANCE_TWO)
            self.assertEqual(
                delta["bindings"][0]["affectedEdge"]["targetService"], "visits-service"
            )
            self.assertAlmostEqual(delta["runtimeParameters"]["q"], 0.99)

            delta_path = root / "typed-delta.json"
            delta_path.write_text(json.dumps(delta), encoding="utf-8")
            ablation_inputs = write_ablation_inputs(root / "ablations", base, evidence_dir)
            ablations = evaluate_ablations(
                *ablation_inputs, self.adapters_path, delta_path, 0.01
            )
            self.assertTrue(ablations["sourceIsolation"]["verified"])
            self.assertEqual(ablations["metricsOnly"]["stateChanges"][0]["after"], "OPEN")
            self.assertAlmostEqual(
                ablations["metricsOnly"]["runtimeParameters"]["q"], 0.99
            )
            self.assertEqual(ablations["metricsOnly"]["edgeBinding"]["status"], "unresolved")
            self.assertEqual(ablations["tracesOnly"]["suppression"]["status"], "identified")
            self.assertEqual(
                ablations["tracesOnly"]["suppression"]["affectedEdge"]["targetService"],
                "visits-service",
            )
            self.assertEqual(ablations["tracesOnly"]["operator"]["status"], "unresolved")
            self.assertEqual(ablations["fullFusion"]["status"], "typed-delta")
            negatives = evaluate_negative_cases(
                base_path,
                evidence_dir,
                self.contract_path,
                self.adapters_path,
                0.01,
            )
            self.assertEqual(negatives["ambiguityReplay"]["status"], "binding-refused")
            self.assertEqual(
                len(negatives["ambiguityReplay"]["matchingEdgeCandidates"]), 2
            )
            self.assertEqual(negatives["ambiguityReplay"]["emittedBindings"], [])
            self.assertEqual(negatives["contradictionReplay"]["status"], "binding-refused")
            self.assertEqual(
                negatives["ambiguityReplay"]["reconciliationStatus"], "unresolved"
            )
            self.assertEqual(
                negatives["contradictionReplay"]["reconciliationStatus"],
                "contradictory",
            )
            self.assertEqual(
                negatives["ambiguityReplay"]["compilationStatus"], "UNASSESSABLE"
            )
            full_graph = trace_graph(evidence_dir)
            sampled = rate_binding(base, full_graph, delta, 0.01)
            self.assertEqual(sampled["status"], "recovered")
            self.assertFalse(sampled["falseBinding"])
            redacted = identity_redaction(base, full_graph, delta, 0.01)
            self.assertAlmostEqual(redacted["globalQ"], 0.99)
            self.assertIsNone(redacted["globalAffectedEdge"])
            self.assertEqual(redacted["specificInstance"], "unresolved")

            reconciliation = reconcile(base, delta)
            self.assertEqual(reconciliation["status"], "identified")
            effective = apply_delta(base, delta, reconciliation)
            compiled = compile_estimates(effective, self.contract)
            self.assertEqual(compiled["status"], "ASSESSED")
            self.assertAlmostEqual(
                compiled["estimates"]["owner-history"]["modelDiscoveredEstimate"], 0.99
            )
            self.assertAlmostEqual(
                compiled["estimates"]["owner-only"]["modelDiscoveredEstimate"], 1.0
            )

            tampered_delta = copy.deepcopy(delta)
            tampered_delta["runtimeParameters"]["q"] = 0.5
            with self.assertRaises(IntegrityError):
                apply_delta(base, tampered_delta, reconciliation)

            tampered_graph_delta = copy.deepcopy(delta)
            tampered_graph_delta["observedTraceGraph"]["interactions"][0][
                "targetService"
            ] = "mutated-service"
            with self.assertRaises(IntegrityError):
                apply_delta(base, tampered_graph_delta, reconciliation)

            tampered_reconciliation = copy.deepcopy(reconciliation)
            tampered_reconciliation["admittedFields"].remove("bindings")
            with self.assertRaises(IntegrityError):
                apply_delta(base, delta, tampered_reconciliation)

            incomplete_reconciliation = copy.deepcopy(reconciliation)
            incomplete_reconciliation["admittedFields"].remove("bindings")
            incomplete_reconciliation = seal_artifact(
                incomplete_reconciliation, "reconciliationVersion"
            )
            with self.assertRaises(IntegrityError):
                apply_delta(base, delta, incomplete_reconciliation)

            tampered_effective = copy.deepcopy(effective)
            tampered_effective["runtimeReliability"]["q"] = 0.5
            with self.assertRaises(IntegrityError):
                compile_estimates(tampered_effective, self.contract)

            wrong_edge_model = copy.deepcopy(effective)
            customers_edge = next(
                edge
                for edge in wrong_edge_model["interactions"]
                if edge["targetService"] == "customers-service"
            )
            wrong_edge_model["operatorBindings"][0]["affectedEdge"] = {
                key: copy.deepcopy(customers_edge[key])
                for key in (
                    "edgeId",
                    "sourceService",
                    "targetService",
                    "operations",
                )
            }
            wrong_edge_model = seal_artifact(wrong_edge_model, "modelVersion")
            wrong_edge_compilation = compile_estimates(wrong_edge_model, self.contract)
            self.assertEqual(wrong_edge_compilation["status"], "UNASSESSABLE")
            self.assertEqual(
                wrong_edge_compilation["estimates"]["owner-history"]["reason"],
                "required-interaction-role-not-uniquely-bound",
            )
            manual = manual_evaluate(
                evidence_dir,
                self.contract,
                json.loads((EXPERIMENT / "manual-composite.json").read_text(encoding="utf-8")),
                self.adapters_path,
            )
            self.assertAlmostEqual(manual["estimates"]["owner-history"], 0.99)

            protocol = json.loads(
                (EXPERIMENT / "protocol.json").read_text(encoding="utf-8")
            )
            orchestrated = run_discovery_pipeline(
                root / "orchestrated", evidence_dir, base_path, protocol
            )
            self.assertEqual(
                orchestrated[2]["estimates"]["owner-history"]["modelDiscoveredEstimate"],
                0.99,
            )

    def test_control_produces_no_state_delta_or_edge_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bootstrap_dir = root / "bootstrap"
            write_evidence(
                bootstrap_dir,
                metrics(INSTANCE_ONE, 0, 0, 0, "CLOSED"),
                metrics(INSTANCE_ONE, 100, 0, 0, "CLOSED"),
                metrics(INSTANCE_TWO, 0, 0, 0, "CLOSED"),
                metrics(INSTANCE_TWO, 100, 0, 0, "CLOSED"),
                trace_summary(100, 100, 100, 100),
                200,
            )
            base = discover_bootstrap(bootstrap_dir, self.contract_path, self.adapters_path)
            base_path = root / "bootstrap-model.json"
            base_path.write_text(json.dumps(base), encoding="utf-8")
            evidence_dir = root / "control"
            write_evidence(
                evidence_dir,
                metrics(INSTANCE_ONE, 0, 0, 0, "CLOSED"),
                metrics(INSTANCE_ONE, 9900, 0, 0, "CLOSED"),
                metrics(INSTANCE_TWO, 0, 0, 0, "CLOSED"),
                metrics(INSTANCE_TWO, 100, 0, 0, "CLOSED"),
                trace_summary(9900, 100, 9900, 100),
                10000,
            )
            delta = discover_delta(base_path, evidence_dir, self.adapters_path, 0.01)
            self.assertEqual(delta["stateChanges"], [])
            self.assertEqual(delta["bindings"], [])
            delta_path = root / "control-delta.json"
            delta_path.write_text(json.dumps(delta), encoding="utf-8")
            ablation_inputs = write_ablation_inputs(
                root / "control-ablations", base, evidence_dir
            )
            ablations = evaluate_ablations(
                *ablation_inputs, self.adapters_path, delta_path, 0.01
            )
            self.assertEqual(ablations["metricsOnly"]["stateChanges"], [])
            self.assertEqual(ablations["tracesOnly"]["suppression"]["status"], "no-drift")
            self.assertEqual(ablations["fullFusion"]["status"], "no-drift")
            negatives = evaluate_negative_cases(
                base_path,
                evidence_dir,
                self.contract_path,
                self.adapters_path,
                0.01,
            )
            self.assertEqual(negatives["ambiguityReplay"]["status"], "not-applicable")
            self.assertEqual(
                negatives["contradictionReplay"]["status"], "not-applicable"
            )
            self.assertEqual(
                rate_binding(base, trace_graph(evidence_dir), delta, 0.01)["status"],
                "no-drift",
            )
            reconciliation = reconcile(base, delta)
            self.assertEqual(reconciliation["status"], "identified")
            compiled = compile_estimates(
                apply_delta(base, delta, reconciliation), self.contract
            )
            self.assertEqual(
                compiled["estimates"]["owner-history"]["modelDiscoveredEstimate"], 1.0
            )

    def test_ambiguous_suppression_is_not_silently_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bootstrap_dir = root / "bootstrap"
            write_evidence(
                bootstrap_dir,
                metrics(INSTANCE_ONE, 0, 0, 0, "CLOSED"),
                metrics(INSTANCE_ONE, 100, 0, 0, "CLOSED"),
                metrics(INSTANCE_TWO, 0, 0, 0, "CLOSED"),
                metrics(INSTANCE_TWO, 100, 0, 0, "CLOSED"),
                trace_summary(100, 100, 100, 100),
                200,
            )
            base = discover_bootstrap(bootstrap_dir, self.contract_path, self.adapters_path)
            base_path = root / "bootstrap-model.json"
            base_path.write_text(json.dumps(base), encoding="utf-8")
            evidence_dir = root / "ambiguous"
            ambiguous_traces = trace_summary(9900, 0, 9900, 100)
            ambiguous_traces["byInstance"][INSTANCE_TWO]["edges"] = {}
            write_evidence(
                evidence_dir,
                metrics(INSTANCE_ONE, 0, 0, 0, "CLOSED"),
                metrics(INSTANCE_ONE, 9900, 0, 0, "CLOSED"),
                metrics(INSTANCE_TWO, 0, 0, 0, "OPEN"),
                metrics(INSTANCE_TWO, 0, 0, 100, "OPEN"),
                ambiguous_traces,
                10000,
            )
            delta = discover_delta(base_path, evidence_dir, self.adapters_path, 0.01)
            self.assertEqual(delta["bindings"], [])
            self.assertFalse(delta["discoveryAudit"]["operatorEdgeBindings"][0]["unique"])
            reconciliation = reconcile(base, delta)
            self.assertEqual(reconciliation["status"], "unresolved")
            effective = apply_delta(base, delta, reconciliation)
            self.assertIsNone(effective["appliedDeltaVersion"])
            compiled = compile_estimates(effective, self.contract)
            self.assertEqual(compiled["status"], "UNASSESSABLE")
            self.assertEqual(
                compiled["estimates"]["owner-history"]["assessmentStatus"],
                "UNASSESSABLE",
            )

    def test_discovery_implementation_contains_no_fixture_identity_or_topology(self) -> None:
        forbidden = ("gateway-A", "gateway-B", "getOwnerDetails", "visits-service")
        for filename in (
            "discover_model.py",
            "apply_model_delta.py",
            "compile_journeys.py",
            "collect_trace_evidence.py",
            "evidence_ablations.py",
            "negative_cases.py",
            "robustness_study.py",
        ):
            text = (SCRIPTS / filename).read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, text, f"{token!r} leaked into {filename}")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


EXPERIMENT = Path(__file__).resolve().parents[1]
SCRIPTS = EXPERIMENT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from apply_model_delta import apply_delta  # noqa: E402
from collect_trace_evidence import normalize_trace  # noqa: E402
from compile_journeys import compile_estimates  # noqa: E402
from discover_model import discover_bootstrap, discover_delta  # noqa: E402
from evidence import (  # noqa: E402
    adapter_operator_counts,
    adapter_operator_state,
    discover_metric_identity,
    discover_operator_names,
    parse_prometheus,
)
from manual_composite import evaluate as manual_evaluate  # noqa: E402


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

    def test_complete_delta_apply_compile_pipeline(self) -> None:
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

            effective = apply_delta(base, delta)
            compiled = compile_estimates(effective, self.contract)
            self.assertAlmostEqual(
                compiled["estimates"]["owner-history"]["modelDiscoveredEstimate"], 0.99
            )
            self.assertAlmostEqual(
                compiled["estimates"]["owner-only"]["modelDiscoveredEstimate"], 1.0
            )
            manual = manual_evaluate(
                evidence_dir,
                self.contract,
                json.loads((EXPERIMENT / "manual-composite.json").read_text(encoding="utf-8")),
                self.adapters_path,
            )
            self.assertAlmostEqual(manual["estimates"]["owner-history"], 0.99)

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
            compiled = compile_estimates(apply_delta(base, delta), self.contract)
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
            with self.assertRaises(ValueError):
                compile_estimates(apply_delta(base, delta), self.contract)

    def test_discovery_implementation_contains_no_fixture_identity_or_topology(self) -> None:
        forbidden = ("gateway-A", "gateway-B", "getOwnerDetails", "visits-service")
        for filename in (
            "discover_model.py",
            "apply_model_delta.py",
            "compile_journeys.py",
            "collect_trace_evidence.py",
        ):
            text = (SCRIPTS / filename).read_text(encoding="utf-8")
            for token in forbidden:
                self.assertNotIn(token, text, f"{token!r} leaked into {filename}")


if __name__ == "__main__":
    unittest.main()

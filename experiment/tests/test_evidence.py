from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from evidence import (  # noqa: E402
    circuitbreaker_counts,
    circuitbreaker_state,
    http_server_availability,
    parse_prometheus,
)
from emac_evaluate import evaluate  # noqa: E402


def metrics(successful: int, failed: int, not_permitted: int, state: str) -> str:
    inactive = "open" if state == "closed" else "closed"
    return f"""
# TYPE resilience4j_circuitbreaker_calls_seconds histogram
resilience4j_circuitbreaker_calls_seconds_count{{kind="successful",name="getOwnerDetails"}} {successful}
resilience4j_circuitbreaker_calls_seconds_count{{kind="failed",name="getOwnerDetails"}} {failed}
resilience4j_circuitbreaker_calls_seconds_count{{kind="ignored",name="getOwnerDetails"}} 0
resilience4j_circuitbreaker_not_permitted_calls_total{{name="getOwnerDetails"}} {not_permitted}
resilience4j_circuitbreaker_state{{name="getOwnerDetails",state="{state}"}} 1
resilience4j_circuitbreaker_state{{name="getOwnerDetails",state="{inactive}"}} 0
http_server_requests_seconds_count{{method="GET",status="200",uri="/pets/visits"}} {successful}
"""


class EvidenceTests(unittest.TestCase):
    def test_exact_treatment_counts_and_state(self) -> None:
        start = parse_prometheus(metrics(0, 0, 0, "closed"))
        end = parse_prometheus(metrics(0, 100, 20, "open"))
        counts = circuitbreaker_counts(start, end, "getOwnerDetails")
        self.assertEqual(counts["permittedFailed"], 100)
        self.assertEqual(counts["notPermitted"], 20)
        self.assertEqual(counts["decisions"], 120)
        self.assertEqual(circuitbreaker_state(end, "getOwnerDetails"), "OPEN")

    def test_service_availability_uses_exact_counter_delta(self) -> None:
        start = parse_prometheus(metrics(10, 0, 0, "closed"))
        end = parse_prometheus(metrics(110, 0, 0, "closed"))
        sli = http_server_availability(start, end, "/pets/visits")
        self.assertEqual(sli["successful"], 100)
        self.assertEqual(sli["total"], 100)
        self.assertEqual(sli["availability"], 1.0)

    def test_prometheus_fractional_values_are_rejected_for_exact_counts(self) -> None:
        start = parse_prometheus(metrics(0, 0, 0, "closed"))
        end = parse_prometheus(metrics(0, 0, 0, "closed").replace(
            'not_permitted_calls_total{name="getOwnerDetails"} 0',
            'not_permitted_calls_total{name="getOwnerDetails"} 0.5',
        ))
        with self.assertRaises(ValueError):
            circuitbreaker_counts(start, end, "getOwnerDetails")

    def test_semantic_control_uses_same_q_without_open_implies_bad_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshots = root / "snapshots"
            snapshots.mkdir()
            (snapshots / "gateway-A.start.prom").write_text(metrics(0, 0, 0, "closed"))
            (snapshots / "gateway-A.end.prom").write_text(metrics(99, 0, 0, "closed"))
            (snapshots / "gateway-B.start.prom").write_text(metrics(0, 0, 0, "open"))
            (snapshots / "gateway-B.end.prom").write_text(metrics(0, 0, 1, "open"))
            service_start = 'http_server_requests_seconds_count{status="200",uri="/owners/{ownerId}"} 0\n'
            customer_end = 'http_server_requests_seconds_count{status="200",uri="/owners/{ownerId}"} 100\n'
            visits_start = 'http_server_requests_seconds_count{status="200",uri="/pets/visits"} 0\n'
            visits_end = 'http_server_requests_seconds_count{status="200",uri="/pets/visits"} 99\n'
            (snapshots / "customers.start.prom").write_text(service_start)
            (snapshots / "customers.end.prom").write_text(customer_end)
            (snapshots / "visits.start.prom").write_text(visits_start)
            (snapshots / "visits.end.prom").write_text(visits_end)
            (root / "load-summary.json").write_text(json.dumps({
                "byGateway": {"A": 99, "B": 1},
                "http2xx": 100,
                "completed": 100,
            }))
            (root / "traces.normalized.json").write_text(json.dumps({
                "byInstance": {
                    "gateway-A": {"journeyTraces": 99, "withoutGatewayVisitsClientSpan": 0},
                    "gateway-B": {"journeyTraces": 1, "withoutGatewayVisitsClientSpan": 1},
                }
            }))
            model = Path(__file__).resolve().parents[1] / "journey-model.json"
            result = evaluate(root, model)
            self.assertAlmostEqual(result["provenance"]["derived"]["q"], 0.99)
            self.assertAlmostEqual(
                result["estimates"]["owner-history"]["evidenceReconciledEstimate"], 0.99
            )
            self.assertAlmostEqual(
                result["estimates"]["owner-only"]["evidenceReconciledEstimate"], 1.0
            )
            self.assertEqual(
                result["provenance"]["derived"]["typedDelta"][0]["path"],
                "operator[getOwnerDetails].runtimeState[gateway-B]",
            )


if __name__ == "__main__":
    unittest.main()

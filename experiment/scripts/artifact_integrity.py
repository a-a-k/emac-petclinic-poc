#!/usr/bin/env python3
"""Canonical versioning and invariant checks for EmaC pipeline artifacts."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from typing import Iterable


class IntegrityError(ValueError):
    """Raised when an artifact does not match its declared content version."""


DELTA_APPLICATION_FIELDS = (
    "selectedOperator",
    "stateChanges",
    "bindings",
    "runtimeParameters",
    "observedOperators",
    "observedTraceGraph",
    "discoveryAudit",
)


def stable_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def seal_artifact(payload: dict[str, object], version_field: str) -> dict[str, object]:
    artifact = copy.deepcopy(payload)
    artifact.pop(version_field, None)
    artifact[version_field] = stable_hash(artifact)
    return artifact


def verify_sealed_artifact(
    artifact: dict[str, object],
    version_field: str,
    expected_schema: str,
) -> None:
    if artifact.get("schemaVersion") != expected_schema:
        raise IntegrityError(
            f"expected schema {expected_schema}, observed {artifact.get('schemaVersion')!r}"
        )
    declared = artifact.get(version_field)
    if not isinstance(declared, str) or len(declared) != 64:
        raise IntegrityError(f"missing or malformed {version_field}")
    material = copy.deepcopy(artifact)
    material.pop(version_field, None)
    computed = stable_hash(material)
    if declared != computed:
        raise IntegrityError(
            f"{version_field} content mismatch: declared {declared}, computed {computed}"
        )


def _require_mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise IntegrityError(f"{label} must be an object")
    return value


def _require_list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise IntegrityError(f"{label} must be an array")
    return value


def _require_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise IntegrityError(f"{label} must be numeric")
    return float(value)


def _require_content_version(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise IntegrityError(f"{label} must be a 64-character content version")
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evidence_references(evidence_dir: Path) -> list[dict[str, object]]:
    required = [evidence_dir / "load-summary.json", evidence_dir / "traces.normalized.json"]
    required.extend(sorted((evidence_dir / "snapshots").glob("*.prom")))
    missing = [str(path) for path in required[:2] if not path.is_file()]
    if missing:
        raise IntegrityError(f"missing evidence inputs: {missing}")
    return [
        {
            "path": path.relative_to(evidence_dir).as_posix(),
            "sha256": file_sha256(path),
            "bytes": path.stat().st_size,
        }
        for path in sorted(required, key=lambda item: item.as_posix())
    ]


def validate_contract(contract: dict[str, object]) -> None:
    verify_sealed_artifact(contract, "contractVersion", "emac.journey-contract/v4")
    roles = _require_mapping(contract.get("interactionRoles"), "interactionRoles")
    journeys = _require_mapping(contract.get("journeys"), "journeys")
    if not roles or not journeys:
        raise IntegrityError("contract requires interaction roles and journeys")
    for role_id, raw_role in roles.items():
        role = _require_mapping(raw_role, f"interactionRoles.{role_id}")
        selectors = _require_list(role.get("operationContains"), f"{role_id}.operationContains")
        if not selectors or not all(isinstance(value, str) and value for value in selectors):
            raise IntegrityError(f"{role_id}.operationContains must contain strings")
    for journey_id, raw_journey in journeys.items():
        journey = _require_mapping(raw_journey, f"journeys.{journey_id}")
        role_id = journey.get("suppressedInteractionRole")
        if role_id not in roles:
            raise IntegrityError(f"journey {journey_id} references unknown role {role_id!r}")
        if not isinstance(journey.get("fallbackSatisfiesJourney"), bool):
            raise IntegrityError(
                f"journey {journey_id} requires fallbackSatisfiesJourney boolean"
            )
        target = _require_number(journey.get("target"), f"journeys.{journey_id}.target")
        if not 0.0 <= target <= 1.0:
            raise IntegrityError(f"journey {journey_id} target is outside [0, 1]")


def validate_adapter_catalog(catalog: dict[str, object]) -> None:
    verify_sealed_artifact(
        catalog, "catalogVersion", "emac.operator-adapters/v2"
    )
    adapters = _require_list(catalog.get("adapters"), "adapters")
    if not adapters:
        raise IntegrityError("operator adapter catalog is empty")
    seen_ids: set[str] = set()
    for index, raw_adapter in enumerate(adapters):
        adapter = _require_mapping(raw_adapter, f"adapters[{index}]")
        adapter_id = str(adapter.get("id", ""))
        if not adapter_id or adapter_id in seen_ids:
            raise IntegrityError(f"adapter id is missing or duplicated: {adapter_id!r}")
        seen_ids.add(adapter_id)
        if not isinstance(adapter.get("operatorType"), str):
            raise IntegrityError(f"adapter {adapter_id} lacks operatorType")
        _require_mapping(adapter.get("metrics"), f"adapter {adapter_id}.metrics")
        _require_mapping(adapter.get("labels"), f"adapter {adapter_id}.labels")
        _require_mapping(adapter.get("semantics"), f"adapter {adapter_id}.semantics")


def validate_bootstrap_model(model: dict[str, object]) -> None:
    verify_sealed_artifact(
        model, "modelVersion", "emac.discovered-interaction-model/v3"
    )
    _require_content_version(model.get("catalogVersion"), "catalogVersion")
    _require_content_version(model.get("contractVersion"), "contractVersion")
    if not isinstance(model.get("contractId"), str) or not model.get("contractId"):
        raise IntegrityError("bootstrap model requires contractId")
    _require_list(model.get("instances"), "instances")
    _require_list(model.get("interactions"), "interactions")
    _require_list(model.get("operators"), "operators")


def _interaction_index(interactions: Iterable[object]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for index, raw in enumerate(interactions):
        row = _require_mapping(raw, f"interactions[{index}]")
        edge_id = str(row.get("edgeId", ""))
        source = str(row.get("sourceService", ""))
        target = str(row.get("targetService", ""))
        if not edge_id or not source or not target:
            raise IntegrityError(f"interaction {index} lacks edge identity")
        if edge_id in result:
            raise IntegrityError(f"duplicate edgeId {edge_id}")
        result[edge_id] = row
    return result


def validate_binding_against_interactions(
    binding: dict[str, object], interactions: Iterable[object]
) -> None:
    affected = _require_mapping(binding.get("affectedEdge"), "binding.affectedEdge")
    index = _interaction_index(interactions)
    edge_id = str(affected.get("edgeId", ""))
    interaction = index.get(edge_id)
    if interaction is None:
        raise IntegrityError(f"binding references absent edge {edge_id!r}")
    for field in ("sourceService", "targetService"):
        if affected.get(field) != interaction.get(field):
            raise IntegrityError(
                f"binding {field} {affected.get(field)!r} disagrees with edge {edge_id}"
            )
    binding_operations = sorted(str(value) for value in affected.get("operations", []))
    interaction_operations = sorted(
        str(value) for value in interaction.get("operations", [])
    )
    if binding_operations != interaction_operations:
        raise IntegrityError(
            f"binding operations disagree with interaction {edge_id}: "
            f"{binding_operations!r} != {interaction_operations!r}"
        )


def validate_candidate_delta(
    delta: dict[str, object], base_model: dict[str, object] | None = None
) -> None:
    verify_sealed_artifact(delta, "deltaVersion", "emac.candidate-model-delta/v3")
    _require_content_version(delta.get("catalogVersion"), "catalogVersion")
    if base_model is not None:
        validate_bootstrap_model(base_model)
        if delta.get("baseModelVersion") != base_model.get("modelVersion"):
            raise IntegrityError("candidate delta targets a different bootstrap model")
        if delta.get("catalogVersion") != base_model.get("catalogVersion"):
            raise IntegrityError("candidate delta uses a different adapter catalog")
    runtime = _require_mapping(delta.get("runtimeParameters"), "runtimeParameters")
    _validate_runtime_parameters(runtime)
    graph = _require_mapping(delta.get("observedTraceGraph"), "observedTraceGraph")
    interactions = _require_list(graph.get("interactions"), "observedTraceGraph.interactions")
    _interaction_index(interactions)
    binding_interactions = base_model["interactions"] if base_model is not None else interactions
    for raw_binding in _require_list(delta.get("bindings"), "bindings"):
        validate_binding_against_interactions(
            _require_mapping(raw_binding, "binding"), binding_interactions
        )
    refs = _require_list(delta.get("evidenceRefs"), "evidenceRefs")
    if not refs:
        raise IntegrityError("candidate delta requires hash-addressed evidenceRefs")


def _validate_runtime_parameters(runtime: dict[str, object]) -> None:
    eligible = _require_number(runtime.get("eligible"), "eligible")
    decisions = _require_number(runtime.get("decisions"), "decisions")
    permitted = _require_number(runtime.get("permitted"), "permitted")
    successful = _require_number(runtime.get("permittedSuccessful"), "permittedSuccessful")
    not_permitted = _require_number(runtime.get("notPermitted"), "notPermitted")
    if eligible <= 0 or decisions <= 0 or permitted <= 0:
        raise IntegrityError("runtime parameter denominators must be positive")
    if not math.isclose(permitted + not_permitted, decisions, abs_tol=1e-9):
        raise IntegrityError("permitted + notPermitted must equal decisions")
    if not 0 <= successful <= permitted:
        raise IntegrityError("permittedSuccessful must be within permitted calls")
    expected = {
        "A_P": decisions / eligible,
        "q": permitted / decisions,
        "A_V": successful / permitted,
    }
    for field, value in expected.items():
        observed = _require_number(runtime.get(field), field)
        if not math.isclose(observed, value, rel_tol=0.0, abs_tol=1e-12):
            raise IntegrityError(f"runtime parameter {field} is inconsistent with counts")


def validate_reconciliation(
    reconciliation: dict[str, object],
    base_model: dict[str, object],
    candidate_delta: dict[str, object],
) -> None:
    verify_sealed_artifact(
        reconciliation, "reconciliationVersion", "emac.reconciliation-decision/v2"
    )
    validate_candidate_delta(candidate_delta, base_model)
    if reconciliation.get("baseModelVersion") != base_model.get("modelVersion"):
        raise IntegrityError("reconciliation targets a different bootstrap model")
    if reconciliation.get("candidateDeltaVersion") != candidate_delta.get("deltaVersion"):
        raise IntegrityError("reconciliation targets a different candidate delta")
    if reconciliation.get("catalogVersion") != candidate_delta.get("catalogVersion"):
        raise IntegrityError("reconciliation uses a different adapter catalog")
    if reconciliation.get("status") not in {"identified", "unresolved", "contradictory"}:
        raise IntegrityError("invalid reconciliation status")
    admitted = _require_list(reconciliation.get("admittedFields"), "admittedFields")
    rejected = _require_list(reconciliation.get("rejectedFields"), "rejectedFields")
    _require_list(reconciliation.get("reasons"), "reasons")
    if reconciliation.get("evidenceRefs") != candidate_delta.get("evidenceRefs"):
        raise IntegrityError("reconciliation evidenceRefs disagree with candidate delta")
    if reconciliation["status"] == "identified":
        if admitted != list(DELTA_APPLICATION_FIELDS) or rejected:
            raise IntegrityError("identified reconciliation must admit the complete delta")
    elif admitted or rejected != list(DELTA_APPLICATION_FIELDS):
        raise IntegrityError("unresolved reconciliation must reject the complete delta")


def validate_effective_model(model: dict[str, object]) -> None:
    verify_sealed_artifact(model, "modelVersion", "emac.effective-interaction-model/v3")
    _require_content_version(model.get("catalogVersion"), "catalogVersion")
    _require_content_version(model.get("contractVersion"), "contractVersion")
    if not isinstance(model.get("contractId"), str) or not model.get("contractId"):
        raise IntegrityError("effective model requires contractId")
    status = model.get("reconciliationStatus")
    if status not in {"identified", "unresolved", "contradictory"}:
        raise IntegrityError("effective model lacks a valid reconciliationStatus")
    if status == "identified":
        if model.get("appliedDeltaVersion") != model.get("candidateDeltaVersion"):
            raise IntegrityError("identified model must apply its candidate delta")
        runtime = _require_mapping(model.get("runtimeReliability"), "runtimeReliability")
        _validate_runtime_parameters(runtime)
        interactions = _require_list(model.get("interactions"), "interactions")
        for raw_binding in _require_list(model.get("operatorBindings"), "operatorBindings"):
            validate_binding_against_interactions(
                _require_mapping(raw_binding, "binding"), interactions
            )
    else:
        if model.get("appliedDeltaVersion") is not None:
            raise IntegrityError("an unresolved model cannot report an applied delta")
        if _require_list(model.get("operatorBindings"), "operatorBindings"):
            raise IntegrityError("an unresolved model cannot expose admitted bindings")


def binding_matches_role(
    binding: dict[str, object], role: dict[str, object]
) -> bool:
    affected = _require_mapping(binding.get("affectedEdge"), "binding.affectedEdge")
    operations = [str(value) for value in affected.get("operations", [])]
    selectors = [str(value) for value in role.get("operationContains", [])]
    return bool(selectors) and any(
        selector.lower() in operation.lower()
        for selector in selectors
        for operation in operations
    )

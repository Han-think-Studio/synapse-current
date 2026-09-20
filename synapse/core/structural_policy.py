"""Evidence-backed structural policy findings for Phase 38.

The evaluator consumes one immutable ``StructuralObservation`` and explicit
data-only rules.  A matching edge becomes an ``OBSERVED`` finding only when
its endpoints and evidence are available.  Missing layer metadata, missing
endpoints, and missing evidence become explicit ``UNRESOLVED`` findings.  This
module never assigns Canonical authority, emits a score, or enforces a rule.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from synapse.core.idea_session import canonical_hash
from synapse.core.structural_observation import (
    STRUCTURAL_ISSUE_SEVERITIES,
    STRUCTURAL_RELATIONS,
    StructuralEdge,
    StructuralNode,
    StructuralObservation,
)


class StructuralPolicyError(ValueError):
    """Raised when a structural policy contract cannot be trusted."""


STRUCTURAL_POLICY_SCHEMA = "structural.policy.v1"
STRUCTURAL_POLICY_FINDING_STATUSES = frozenset({"OBSERVED", "UNRESOLVED"})
STRUCTURAL_LAYER_ATTRIBUTE = "layer"
_MAX_RULES = 1_000
_MAX_FINDINGS = 50_000
_MAX_TEXT = 4_000
_MAX_ATTRIBUTE_DEPTH = 12


def _text(value: Any, label: str, *, limit: int = _MAX_TEXT, required: bool = True) -> str:
    if not isinstance(value, str):
        raise StructuralPolicyError(f"{label}는 문자열이어야 합니다.")
    result = value.strip()
    if required and not result:
        raise StructuralPolicyError(f"{label}은(는) 비어 있을 수 없습니다.")
    if len(result) > limit:
        raise StructuralPolicyError(f"{label}이(가) 너무 깁니다.")
    return result


def _strings(value: Any, label: str, *, limit: int = 1_000) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise StructuralPolicyError(f"{label}는 문자열 배열이어야 합니다.")
    if len(value) > limit:
        raise StructuralPolicyError(f"{label} 항목이 너무 많습니다.")
    result = tuple(_text(item, label, limit=240) for item in value)
    if len(result) != len(set(result)):
        raise StructuralPolicyError(f"{label}에 중복 항목이 있습니다.")
    return tuple(sorted(result))


def _freeze_json(value: Any, label: str, *, depth: int = 0) -> Any:
    if depth > _MAX_ATTRIBUTE_DEPTH:
        raise StructuralPolicyError(f"{label} 중첩 깊이가 너무 깊습니다.")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StructuralPolicyError(f"{label}에 유한하지 않은 숫자가 있습니다.")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise StructuralPolicyError(f"{label}의 객체 키는 문자열이어야 합니다.")
            frozen[key] = _freeze_json(item, f"{label}.{key}", depth=depth + 1)
        return MappingProxyType(dict(sorted(frozen.items())))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, label, depth=depth + 1) for item in value)
    raise StructuralPolicyError(f"{label}에는 JSON 값만 허용됩니다.")


def _freeze_mapping(value: Mapping[str, Any], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StructuralPolicyError(f"{label}는 JSON 객체여야 합니다.")
    frozen = _freeze_json(value, label)
    if not isinstance(frozen, Mapping):  # pragma: no cover - guarded above
        raise StructuralPolicyError(f"{label}는 JSON 객체여야 합니다.")
    return frozen


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _normalize_layer(value: Any) -> str:
    layer = _text(value, STRUCTURAL_LAYER_ATTRIBUTE, limit=120)
    return layer.replace("-", "_").replace(" ", "_").upper()


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralPolicyRule:
    """Data-only matcher for one prohibited directed relationship."""

    id: str
    description: str
    relations: tuple[str, ...]
    source_layer: str
    target_layer: str
    severity: str = "ERROR"
    schema_version: str = STRUCTURAL_POLICY_SCHEMA

    def __post_init__(self) -> None:
        schema_version = _text(self.schema_version, "rule.schema_version", limit=120)
        if schema_version != STRUCTURAL_POLICY_SCHEMA:
            raise StructuralPolicyError(
                f"지원하지 않는 structural policy schema입니다: {schema_version}"
            )
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "id", _text(self.id, "rule.id", limit=240))
        object.__setattr__(self, "description", _text(self.description, "rule.description"))
        if isinstance(self.relations, (str, bytes)) or not isinstance(self.relations, Sequence):
            raise StructuralPolicyError("rule.relations는 relation 문자열 배열이어야 합니다.")
        relations = tuple(_text(value, "rule.relations", limit=80).upper() for value in self.relations)
        if not relations:
            raise StructuralPolicyError("rule.relations은(는) 하나 이상 필요합니다.")
        if len(relations) != len(set(relations)):
            raise StructuralPolicyError("rule.relations에 중복 relation이 있습니다.")
        unsupported = set(relations) - STRUCTURAL_RELATIONS
        if unsupported:
            raise StructuralPolicyError(
                f"지원하지 않는 structural relation입니다: {sorted(unsupported)}"
            )
        object.__setattr__(self, "relations", tuple(sorted(relations)))
        object.__setattr__(self, "source_layer", _normalize_layer(self.source_layer))
        object.__setattr__(self, "target_layer", _normalize_layer(self.target_layer))
        severity = _text(self.severity, "rule.severity", limit=40).upper()
        if severity not in STRUCTURAL_ISSUE_SEVERITIES:
            raise StructuralPolicyError(f"지원하지 않는 policy severity입니다: {severity}")
        object.__setattr__(self, "severity", severity)

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "relations": list(self.relations),
            "source_layer": self.source_layer,
            "target_layer": self.target_layer,
            "severity": self.severity,
            "schema_version": self.schema_version,
        }


BUILT_IN_STRUCTURAL_POLICY_RULES = (
    StructuralPolicyRule(
        id="CORE_MUST_NOT_DEPEND_ON_RUNTIME",
        description="CORE layer must not import or depend on RUNTIME layer.",
        relations=("DEPENDS_ON", "IMPORTS"),
        source_layer="CORE",
        target_layer="RUNTIME",
    ),
    StructuralPolicyRule(
        id="PROJECTION_MUST_NOT_WRITE_REGISTRY",
        description="PROJECTION layer must not write directly to REGISTRY.",
        relations=("WRITES",),
        source_layer="PROJECTION",
        target_layer="REGISTRY",
    ),
    StructuralPolicyRule(
        id="DOMAIN_PACK_MUST_NOT_WRITE_CANONICAL",
        description="DOMAIN_PACK must not write directly to CANONICAL.",
        relations=("WRITES",),
        source_layer="DOMAIN_PACK",
        target_layer="CANONICAL",
    ),
    StructuralPolicyRule(
        id="RUNTIME_RESPONSE_MUST_NOT_PROMOTE_CANONICAL",
        description="RUNTIME_RESPONSE must not promote directly into CANONICAL.",
        relations=("WRITES",),
        source_layer="RUNTIME_RESPONSE",
        target_layer="CANONICAL",
    ),
)


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralFinding:
    """One evidence-backed policy result, never a Canonical state item."""

    rule_id: str
    status: str
    severity: str
    observation_id: str
    source_id: str
    target_id: str
    relation: str
    detail: str
    source_hash: str
    evidence_refs: tuple[str, ...] = ()
    schema_version: str = STRUCTURAL_POLICY_SCHEMA
    id: str = ""
    finding_hash: str = ""
    canonical_mutation: bool = False
    filesystem_mutation: bool = False

    def __post_init__(self) -> None:
        schema_version = _text(self.schema_version, "finding.schema_version", limit=120)
        if schema_version != STRUCTURAL_POLICY_SCHEMA:
            raise StructuralPolicyError(
                f"지원하지 않는 structural policy schema입니다: {schema_version}"
            )
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "rule_id", _text(self.rule_id, "finding.rule_id", limit=240))
        status = _text(self.status, "finding.status", limit=40).upper()
        if status not in STRUCTURAL_POLICY_FINDING_STATUSES:
            raise StructuralPolicyError(f"지원하지 않는 finding status입니다: {status}")
        object.__setattr__(self, "status", status)
        severity = _text(self.severity, "finding.severity", limit=40).upper()
        if severity not in STRUCTURAL_ISSUE_SEVERITIES:
            raise StructuralPolicyError(f"지원하지 않는 finding severity입니다: {severity}")
        object.__setattr__(self, "severity", severity)
        object.__setattr__(
            self,
            "observation_id",
            _text(self.observation_id, "finding.observation_id", limit=500),
        )
        object.__setattr__(self, "source_id", _text(self.source_id, "finding.source_id", limit=500))
        object.__setattr__(self, "target_id", _text(self.target_id, "finding.target_id", limit=500))
        relation = _text(self.relation, "finding.relation", limit=80).upper()
        if relation not in STRUCTURAL_RELATIONS:
            raise StructuralPolicyError(f"지원하지 않는 finding relation입니다: {relation}")
        object.__setattr__(self, "relation", relation)
        object.__setattr__(self, "detail", _text(self.detail, "finding.detail"))
        object.__setattr__(self, "source_hash", _text(self.source_hash, "finding.source_hash", limit=240))
        evidence_refs = _strings(self.evidence_refs, "finding.evidence_refs")
        if status == "OBSERVED" and not evidence_refs:
            raise StructuralPolicyError("OBSERVED finding에는 evidence reference가 필요합니다.")
        object.__setattr__(self, "evidence_refs", evidence_refs)
        if not isinstance(self.canonical_mutation, bool) or self.canonical_mutation:
            raise StructuralPolicyError("finding.canonical_mutation은 false여야 합니다.")
        if not isinstance(self.filesystem_mutation, bool) or self.filesystem_mutation:
            raise StructuralPolicyError("finding.filesystem_mutation은 false여야 합니다.")
        finding_hash = canonical_hash(self._hash_payload())
        expected_id = f"structural-finding:{finding_hash.removeprefix('sha256:')}"
        if self.finding_hash and self.finding_hash != finding_hash:
            raise StructuralPolicyError("finding_hash가 payload와 일치하지 않습니다.")
        if self.id and self.id != expected_id:
            raise StructuralPolicyError("StructuralFinding id가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "finding_hash", finding_hash)
        object.__setattr__(self, "id", expected_id)

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "rule_id": self.rule_id,
            "status": self.status,
            "severity": self.severity,
            "observation_id": self.observation_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation": self.relation,
            "detail": self.detail,
            "source_hash": self.source_hash,
            "evidence_refs": list(self.evidence_refs),
        }

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "finding_hash": self.finding_hash,
            "rule_id": self.rule_id,
            "status": self.status,
            "severity": self.severity,
            "observation_id": self.observation_id,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "relation": self.relation,
            "detail": self.detail,
            "source_hash": self.source_hash,
            "evidence_refs": list(self.evidence_refs),
            "schema_version": self.schema_version,
            "canonical_mutation": self.canonical_mutation,
            "filesystem_mutation": self.filesystem_mutation,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class StructuralPolicyReport:
    """Deterministic, non-blocking report for one observed structure."""

    source_id: str
    source_hash: str
    rules: tuple[StructuralPolicyRule, ...] = ()
    findings: tuple[StructuralFinding, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = STRUCTURAL_POLICY_SCHEMA
    id: str = ""
    report_hash: str = ""
    canonical_mutation: bool = False
    filesystem_mutation: bool = False

    def __post_init__(self) -> None:
        schema_version = _text(self.schema_version, "report.schema_version", limit=120)
        if schema_version != STRUCTURAL_POLICY_SCHEMA:
            raise StructuralPolicyError(
                f"지원하지 않는 structural policy schema입니다: {schema_version}"
            )
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "source_id", _text(self.source_id, "report.source_id", limit=500))
        object.__setattr__(self, "source_hash", _text(self.source_hash, "report.source_hash", limit=240))
        rules = _typed_items(self.rules, "report.rules", StructuralPolicyRule, _MAX_RULES)
        if len({rule.id for rule in rules}) != len(rules):
            raise StructuralPolicyError("report.rules에 중복 rule id가 있습니다.")
        rules = tuple(sorted(rules, key=lambda rule: rule.id))
        findings = _typed_items(self.findings, "report.findings", StructuralFinding, _MAX_FINDINGS)
        if len({finding.id for finding in findings}) != len(findings):
            raise StructuralPolicyError("report.findings에 중복 finding id가 있습니다.")
        rule_ids = {rule.id for rule in rules}
        for finding in findings:
            if finding.observation_id != self.source_id or finding.source_hash != self.source_hash:
                raise StructuralPolicyError("finding의 source identity가 report와 다릅니다.")
            if finding.rule_id not in rule_ids:
                raise StructuralPolicyError("finding이 report.rules에 없는 rule을 참조합니다.")
        findings = tuple(
            sorted(
                findings,
                key=lambda finding: (
                    finding.rule_id,
                    finding.source_id,
                    finding.target_id,
                    finding.relation,
                    finding.status,
                    finding.id,
                ),
            )
        )
        object.__setattr__(self, "rules", rules)
        object.__setattr__(self, "findings", findings)
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata, "report.metadata"))
        if not isinstance(self.canonical_mutation, bool) or self.canonical_mutation:
            raise StructuralPolicyError("report.canonical_mutation은 false여야 합니다.")
        if not isinstance(self.filesystem_mutation, bool) or self.filesystem_mutation:
            raise StructuralPolicyError("report.filesystem_mutation은 false여야 합니다.")
        report_hash = canonical_hash(self._hash_payload())
        expected_id = f"structural-policy-report:{report_hash.removeprefix('sha256:')}"
        if self.report_hash and self.report_hash != report_hash:
            raise StructuralPolicyError("report_hash가 payload와 일치하지 않습니다.")
        if self.id and self.id != expected_id:
            raise StructuralPolicyError("StructuralPolicyReport id가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "report_hash", report_hash)
        object.__setattr__(self, "id", expected_id)

    def _hash_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "source_hash": self.source_hash,
            "rules": [rule.to_record() for rule in self.rules],
            "findings": [finding.to_record() for finding in self.findings],
        }

    @property
    def violations(self) -> tuple[StructuralFinding, ...]:
        return tuple(finding for finding in self.findings if finding.status == "OBSERVED")

    @property
    def unresolved(self) -> tuple[StructuralFinding, ...]:
        return tuple(finding for finding in self.findings if finding.status == "UNRESOLVED")

    @property
    def passed(self) -> bool:
        """Return clean observation status, not an enforcement decision."""

        return not self.findings

    def to_record(self) -> dict[str, Any]:
        if self.violations and self.unresolved:
            status = "FINDINGS_AND_UNRESOLVED"
        elif self.violations:
            status = "FINDINGS"
        elif self.unresolved:
            status = "UNRESOLVED"
        else:
            status = "CLEAN"
        return {
            "id": self.id,
            "report_hash": self.report_hash,
            "source_id": self.source_id,
            "source_hash": self.source_hash,
            "status": status,
            "rules": [rule.to_record() for rule in self.rules],
            "findings": [finding.to_record() for finding in self.findings],
            "metadata": _thaw_json(self.metadata),
            "schema_version": self.schema_version,
            "canonical_mutation": self.canonical_mutation,
            "filesystem_mutation": self.filesystem_mutation,
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_record(), ensure_ascii=False, indent=indent, sort_keys=True)


def _typed_items(
    value: Any,
    label: str,
    expected_type: type[Any],
    max_items: int,
) -> tuple[Any, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise StructuralPolicyError(f"{label}는 배열이어야 합니다.")
    if len(value) > max_items:
        raise StructuralPolicyError(f"{label} 항목이 너무 많습니다.")
    result = tuple(value)
    if any(not isinstance(item, expected_type) for item in result):
        raise StructuralPolicyError(f"{label}에는 {expected_type.__name__} 항목만 허용됩니다.")
    return result


def _validate_rules(rules: Sequence[StructuralPolicyRule]) -> tuple[StructuralPolicyRule, ...]:
    validated = _typed_items(rules, "rules", StructuralPolicyRule, _MAX_RULES)
    if not validated:
        raise StructuralPolicyError("rules은(는) 하나 이상 필요합니다.")
    if len({rule.id for rule in validated}) != len(validated):
        raise StructuralPolicyError("rules에 중복 rule id가 있습니다.")
    return tuple(sorted(validated, key=lambda rule: rule.id))


def _node_layer(node: StructuralNode | None, endpoint: str) -> tuple[str | None, str | None]:
    if node is None:
        return None, f"missing_{endpoint}_node"
    if STRUCTURAL_LAYER_ATTRIBUTE not in node.attributes:
        return None, f"missing_{endpoint}_layer"
    try:
        return _normalize_layer(node.attributes[STRUCTURAL_LAYER_ATTRIBUTE]), None
    except StructuralPolicyError:
        return None, f"invalid_{endpoint}_layer"


def _edge_evidence_refs(
    edge: StructuralEdge,
    source: StructuralNode | None,
    target: StructuralNode | None,
) -> tuple[str, ...]:
    refs = set(edge.evidence_refs)
    if source is not None:
        refs.update(source.evidence_refs)
    if target is not None:
        refs.update(target.evidence_refs)
    return tuple(sorted(refs))


def _unresolved_detail(rule: StructuralPolicyRule, reason: str) -> str:
    details = {
        "missing_source_node": "source node is missing from the observation",
        "missing_target_node": "target node is missing from the observation",
        "missing_source_layer": "source node has no layer attribute",
        "missing_target_layer": "target node has no layer attribute",
        "invalid_source_layer": "source node layer attribute is invalid",
        "invalid_target_layer": "target node layer attribute is invalid",
        "missing_evidence": "matching endpoints have no evidence reference",
    }
    return f"{rule.id}: {reason}: {details[reason]}"


def evaluate_structural_policy(
    observation: StructuralObservation,
    *,
    rules: Sequence[StructuralPolicyRule] = BUILT_IN_STRUCTURAL_POLICY_RULES,
) -> StructuralPolicyReport:
    """Evaluate explicit edge rules without enforcing or mutating anything."""

    if not isinstance(observation, StructuralObservation):
        raise StructuralPolicyError("observation은 StructuralObservation이어야 합니다.")
    validated_rules = _validate_rules(rules)
    node_by_id = {node.id: node for node in observation.nodes}
    findings: list[StructuralFinding] = []
    for edge in sorted(observation.edges, key=lambda item: item.key):
        matching_rules = tuple(rule for rule in validated_rules if edge.relation in rule.relations)
        if not matching_rules:
            continue
        source = node_by_id.get(edge.source_id)
        target = node_by_id.get(edge.target_id)
        evidence_refs = _edge_evidence_refs(edge, source, target)
        source_layer, source_reason = _node_layer(source, "source")
        target_layer, target_reason = _node_layer(target, "target")
        for rule in matching_rules:
            reason = source_reason or target_reason
            if reason is not None:
                findings.append(
                    StructuralFinding(
                        rule_id=rule.id,
                        status="UNRESOLVED",
                        severity=rule.severity,
                        observation_id=observation.source_id,
                        source_id=edge.source_id,
                        target_id=edge.target_id,
                        relation=edge.relation,
                        detail=_unresolved_detail(rule, reason),
                        source_hash=observation.observation_hash,
                        evidence_refs=evidence_refs,
                    )
                )
                continue
            if source_layer != rule.source_layer or target_layer != rule.target_layer:
                continue
            if not evidence_refs:
                findings.append(
                    StructuralFinding(
                        rule_id=rule.id,
                        status="UNRESOLVED",
                        severity=rule.severity,
                        observation_id=observation.source_id,
                        source_id=edge.source_id,
                        target_id=edge.target_id,
                        relation=edge.relation,
                        detail=_unresolved_detail(rule, "missing_evidence"),
                        source_hash=observation.observation_hash,
                    )
                )
                continue
            findings.append(
                StructuralFinding(
                    rule_id=rule.id,
                    status="OBSERVED",
                    severity=rule.severity,
                    observation_id=observation.source_id,
                    source_id=edge.source_id,
                    target_id=edge.target_id,
                    relation=edge.relation,
                    detail=rule.description,
                    source_hash=observation.observation_hash,
                    evidence_refs=evidence_refs,
                )
            )
    return StructuralPolicyReport(
        source_id=observation.source_id,
        source_hash=observation.observation_hash,
        rules=validated_rules,
        findings=tuple(findings),
        metadata={
            "sensor_type": observation.sensor_type,
            "sensor_version": observation.sensor_version,
            "workspace_hash": observation.workspace_hash,
            "captured_at": observation.captured_at,
        },
    )


__all__ = [
    "BUILT_IN_STRUCTURAL_POLICY_RULES",
    "STRUCTURAL_LAYER_ATTRIBUTE",
    "STRUCTURAL_POLICY_FINDING_STATUSES",
    "STRUCTURAL_POLICY_SCHEMA",
    "StructuralFinding",
    "StructuralPolicyError",
    "StructuralPolicyReport",
    "StructuralPolicyRule",
    "evaluate_structural_policy",
]

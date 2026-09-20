"""Deterministic semantic checks for generated idea artifact content.

The blueprint and artifact manifest describe *what* must be produced.  This
module checks a model-produced content mapping when one is available.  It is
deliberately proposal-only: it does not call a provider, write files, or
change routing/state behaviour.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from synapse.core.idea_artifacts import ArtifactProposal
from synapse.core.idea_reactive_depth import COMMON_REACTIVE_REQUIREMENTS
from synapse.core.idea_session import canonical_hash

_PLACEHOLDERS = frozenset(
    {
        "",
        "todo",
        "tbd",
        "pending",
        "placeholder",
        "to be determined",
        "n/a",
        "na",
        "none",
        "null",
        "example",
        "예시",
        "미정",
        "추후 작성",
        "나중에 작성",
    }
)

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "stable_id": ("stable_id", "id", "artifact_id"),
    "input_refs": ("input_refs", "inputs", "source_refs", "references"),
    "event_or_trigger": ("event_or_trigger", "event", "events", "trigger", "triggers"),
    "guard_or_precondition": ("guard_or_precondition", "guard", "guards", "precondition", "conditions"),
    "effect_or_state_delta": ("effect_or_state_delta", "effect", "effects", "state_delta", "transition", "transitions"),
    "downstream_impact_refs": ("downstream_impact_refs", "downstream_impacts", "impact_refs", "impacts", "downstream"),
    "normal_boundary_failure_cases": ("normal_boundary_failure_cases", "validation_cases", "validation", "test_cases"),
}


def _text(value: Any) -> str:
    return value.strip().casefold() if isinstance(value, str) else ""


def _meaningful(value: Any) -> bool:
    """Return true only when a value carries content beyond a placeholder."""

    if isinstance(value, str):
        normalized = _text(value)
        return bool(normalized) and normalized not in _PLACEHOLDERS
    if isinstance(value, Mapping):
        return bool(value) and any(_meaningful(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return bool(value) and any(_meaningful(item) for item in value)
    return value is not None


def _find_field(content: Mapping[str, Any], requirement: str) -> tuple[str | None, Any]:
    for alias in _FIELD_ALIASES[requirement]:
        if alias in content:
            return alias, content[alias]
    return None, None


def _validation_case_kinds(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        return {str(key).strip().casefold() for key, item in value.items() if _meaningful(item)}
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return set()
    kinds: set[str] = set()
    for item in value:
        if isinstance(item, Mapping):
            kind = item.get("kind", item.get("case"))
            if isinstance(kind, str) and _meaningful(item.get("expected", item.get("result", item))):
                kinds.add(kind.strip().casefold())
        elif isinstance(item, str) and _meaningful(item):
            # Plain case strings are accepted only as an explicitly labelled
            # convention; three unlabeled strings cannot prove case coverage.
            continue
    return kinds


def validate_reactive_content(
    artifact: ArtifactProposal,
    content: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate common connected-reaction fields in one generated artifact.

    The result is an immutable-style receipt.  Missing, empty, or placeholder
    fields produce ``BLOCKED``.  The validator intentionally does not infer a
    trigger, guard, effect, or impact from prose in another field.
    """

    if not isinstance(artifact, ArtifactProposal):
        raise TypeError("artifact must be an ArtifactProposal")
    if not isinstance(content, Mapping):
        raise TypeError("content must be a mapping")

    errors: list[dict[str, str]] = []
    checked: dict[str, dict[str, Any]] = {}
    for requirement in COMMON_REACTIVE_REQUIREMENTS:
        field, value = _find_field(content, requirement)
        meaningful = _meaningful(value) if field is not None else False
        checked[requirement] = {"field": field, "meaningful": meaningful}
        if field is None:
            errors.append({"code": "missing_reactive_field", "requirement": requirement})
        elif not meaningful:
            errors.append({"code": "placeholder_reactive_field", "requirement": requirement, "field": field})

    # The manifest is the source of truth for domain depth.  Keep the legacy
    # common-field aliases above, but require explicit profile/zone lenses so
    # the validator never infers domain semantics from prose.
    profile_lens = content.get("profile_lens")
    zone_reactive = content.get("zone_reactive")
    required_domain = tuple(
        requirement
        for requirement in artifact.reactive_requirements
        if requirement not in COMMON_REACTIVE_REQUIREMENTS
    )
    lens_requirements = tuple(
        requirement
        for requirement in required_domain
        if requirement in {"decision", "tradeoff", "feedback_loop", "request_contract", "failure_boundary", "idempotent_recovery", "evidence_provenance", "falsification", "reproduction", "user_outcome", "lifecycle_state", "support_escalation", "player_choice", "world_reaction", "save_load_consistency", "trigger_scope", "permission_guard", "retry_idempotency", "learner_signal", "misconception_repair", "revision_effect"}
    )
    zone_requirements = tuple(requirement for requirement in required_domain if requirement not in lens_requirements)
    checked["profile_lens"] = {"field": "profile_lens", "required": list(lens_requirements)}
    checked["zone_reactive"] = {"field": "zone_reactive", "required": list(zone_requirements)}
    for container_name, container, requirements in (
        ("profile_lens", profile_lens, lens_requirements),
        ("zone_reactive", zone_reactive, zone_requirements),
    ):
        for requirement in requirements:
            value = container.get(requirement) if isinstance(container, Mapping) else None
            if not _meaningful(value):
                errors.append({"code": "missing_profile_zone_reactive_field", "container": container_name, "requirement": requirement})

    case_field, case_value = _find_field(content, "normal_boundary_failure_cases")
    if case_field is not None:
        kinds = _validation_case_kinds(case_value)
        checked["normal_boundary_failure_cases"]["case_kinds"] = sorted(kinds)
        missing = sorted({"normal", "boundary", "failure"} - kinds)
        if missing:
            errors.append({"code": "incomplete_validation_cases", "missing": ",".join(missing)})

    payload = {
        "schema_version": "idea.reactive-semantic.v1",
        "artifact_id": artifact.artifact_id,
        "blueprint_id": artifact.blueprint_id,
        "content_hash": canonical_hash(dict(content)),
        "required_requirements": list(artifact.reactive_requirements),
        "checked": checked,
        "errors": errors,
        "status": "READY" if not errors else "BLOCKED",
        "proposal_only": True,
        "filesystem_mutation": False,
        "execution_allowed": False,
    }
    payload["receipt_hash"] = canonical_hash(payload)
    return payload


def verify_reactive_content_receipt(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Verify a semantic receipt without re-running or trusting its content.

    Generated content is treated as untrusted at the review boundary.  A
    changed field must invalidate the receipt hash before any caller can use
    its ``READY`` status.  This is a read-only check and never invokes a
    provider or applies a proposal.
    """

    if not isinstance(receipt, Mapping):
        raise TypeError("receipt must be a mapping")
    checks = {
        "schema_supported": receipt.get("schema_version") == "idea.reactive-semantic.v1",
        "proposal_only": receipt.get("proposal_only") is True,
        "filesystem_mutation_false": receipt.get("filesystem_mutation") is False,
        "execution_disallowed": receipt.get("execution_allowed") is False,
        "receipt_hash_present": isinstance(receipt.get("receipt_hash"), str)
        and receipt["receipt_hash"].startswith("sha256:"),
    }
    if checks["receipt_hash_present"]:
        payload = dict(receipt)
        supplied_hash = payload.pop("receipt_hash")
        checks["receipt_hash_matches"] = canonical_hash(payload) == supplied_hash
    else:
        checks["receipt_hash_matches"] = False
    checks["ready_status"] = receipt.get("status") == "READY"
    errors = tuple(name for name, passed in checks.items() if not passed)
    return {
        "schema_version": "idea.reactive-semantic-verification.v1",
        "passed": not errors,
        "checks": checks,
        "errors": list(errors),
        "proposal_only": True,
        "filesystem_mutation": False,
        "execution_allowed": False,
        "receipt_hash": receipt.get("receipt_hash"),
    }


__all__ = ["validate_reactive_content", "verify_reactive_content_receipt"]

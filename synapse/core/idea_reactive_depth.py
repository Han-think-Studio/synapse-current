"""Shared novel-grade connection requirements for every idea profile.

This module is declarative. It does not route providers, persist story state,
resume runs, execute handlers, or apply files. It gives each profile the same
minimum causal vocabulary while preserving its domain-specific reaction lens.
"""

from __future__ import annotations

from collections.abc import Mapping

COMMON_REACTIVE_REQUIREMENTS = (
    "stable_id",
    "input_refs",
    "event_or_trigger",
    "guard_or_precondition",
    "effect_or_state_delta",
    "downstream_impact_refs",
    "normal_boundary_failure_cases",
)

ZONE_REACTIVE_REQUIREMENTS: Mapping[str, tuple[str, ...]] = {
    "context": ("scope_change", "constraint_cost", "assumption_test"),
    "actors": ("goal_need_tension", "knowledge_boundary", "choice_alternatives"),
    "relationships": ("directed_change", "cause_event_ref", "fallback_or_repair"),
    "causality": ("cause_effect_chain", "counterfactual", "consequence_cost"),
    "milestones": ("entry_state", "exit_state", "blocked_transition"),
    "signals": ("observable_signal", "threshold_or_interpretation", "response_path"),
    "interactions": ("input_state", "actor_choice", "output_state"),
    "state_reaction": ("before_state", "guard", "transition", "handler_effect", "recovery"),
    "integrity": ("cross_zone_assertion", "contradiction_case", "replay_case"),
}

PROFILE_REACTIVE_LENSES: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "general": {"lens": ("decision", "tradeoff", "feedback_loop")},
    "software": {"lens": ("request_contract", "failure_boundary", "idempotent_recovery")},
    "research": {"lens": ("evidence_provenance", "falsification", "reproduction")},
    "product": {"lens": ("user_outcome", "lifecycle_state", "support_escalation")},
    "game": {"lens": ("player_choice", "world_reaction", "save_load_consistency")},
    "automation": {"lens": ("trigger_scope", "permission_guard", "retry_idempotency")},
    "content": {"lens": ("learner_signal", "misconception_repair", "revision_effect")},
}


def reactive_requirements_for(profile_id: str, zone: str) -> tuple[str, ...]:
    """Return deterministic shared and profile-specific depth requirements."""

    profile = profile_id.strip().lower()
    normalized_zone = zone.strip().lower()
    if profile not in PROFILE_REACTIVE_LENSES:
        raise KeyError(f"unknown profile: {profile_id}")
    if normalized_zone not in ZONE_REACTIVE_REQUIREMENTS:
        raise KeyError(f"unknown zone: {zone}")
    lens = PROFILE_REACTIVE_LENSES[profile]["lens"]
    return tuple(dict.fromkeys((*COMMON_REACTIVE_REQUIREMENTS, *ZONE_REACTIVE_REQUIREMENTS[normalized_zone], *lens)))


__all__ = [
    "COMMON_REACTIVE_REQUIREMENTS",
    "PROFILE_REACTIVE_LENSES",
    "ZONE_REACTIVE_REQUIREMENTS",
    "reactive_requirements_for",
]

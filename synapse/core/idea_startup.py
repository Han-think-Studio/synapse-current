"""Single proposal-only entry point for the novel-grade idea startup flow."""

from typing import Any

from synapse.core.blueprint_catalog import build_blueprint_scaffold_plan, load_blueprint_catalog
from synapse.core.idea_apply_guard import evaluate_idea_apply_guard
from synapse.core.idea_blueprint import build_idea_blueprint
from synapse.core.idea_guided_mapping import (
    list_blueprint_mapping,
    list_guided_mapping,
    validate_guided_mapping,
)
from synapse.core.idea_review import build_idea_review_packet
from synapse.core.idea_session import IdeaSeed

GUIDED_PROFILE_IDS = frozenset({"general", "software", "research", "content", "automation"})


def build_idea_startup_bundle(seed: IdeaSeed, profile_id: str) -> dict[str, Any]:
    """Build the complete pre-apply bundle without invoking providers or writing files."""

    blueprint = build_idea_blueprint(seed, profile_id)
    review = build_idea_review_packet(blueprint)
    guard = evaluate_idea_apply_guard(review)
    guided_support = "catalog_ready" if profile_id.strip().lower() in GUIDED_PROFILE_IDS else "blueprint_only"
    guided_mapping = (
        [
            {"slot_id": item.slot_id, "zone": item.zone, "role": item.role, "rationale": item.rationale}
            for item in list_guided_mapping(profile_id)
        ]
        if guided_support == "catalog_ready"
        else []
    )
    guided_mapping_validation = (
        validate_guided_mapping(profile_id) if guided_support == "catalog_ready" else None
    )
    blueprint_mapping = (
        [
            {"slot_id": item.slot_id, "zone": item.zone, "role": item.role, "rationale": item.rationale}
            for item in list_blueprint_mapping(profile_id)
        ]
        if guided_support == "blueprint_only"
        else []
    )
    blueprint_catalog = (
        load_blueprint_catalog(profile_id) if guided_support == "blueprint_only" else None
    )
    blueprint_scaffold = (
        build_blueprint_scaffold_plan(profile_id) if guided_support == "blueprint_only" else []
    )
    return {
        "schema_version": "idea.startup.v1",
        "review": review,
        "apply_guard": guard,
        "guided_support": guided_support,
        "guided_mapping": guided_mapping,
        "guided_mapping_validation": guided_mapping_validation,
        "blueprint_mapping": blueprint_mapping,
        "blueprint_catalog": blueprint_catalog,
        "blueprint_scaffold": blueprint_scaffold,
        "proposal_only": True,
        "model_invoked": False,
        "filesystem_mutation": False,
        "external_execution": False,
    }


__all__ = ["build_idea_startup_bundle"]

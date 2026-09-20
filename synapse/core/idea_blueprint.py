"""Proposal-only blueprint compilation for novel-grade idea expansion."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from synapse.core.idea_profiles import ZONE_SPECS, ZonePlan, build_zone_plan, get_idea_profile
from synapse.core.idea_session import IdeaSeed, canonical_hash


@dataclass(frozen=True, slots=True)
class IdeaBlueprint:
    """A deterministic map of work to be generated; it has no side effects."""

    id: str
    seed_hash: str
    profile_id: str
    profile_version: str
    zone_contract_hash: str
    zones: tuple[ZonePlan, ...]
    metadata: MappingProxyType

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "seed_hash": self.seed_hash,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "zone_contract_hash": self.zone_contract_hash,
            "zones": [
                {
                    "zone": zone.zone,
                    "priority": zone.priority,
                    "artifact_family": zone.artifact_family,
                }
                for zone in self.zones
            ],
            "metadata": dict(self.metadata),
            "proposal_only": True,
            "filesystem_mutation": False,
            "model_invoked": False,
        }


def build_idea_blueprint(seed: IdeaSeed, profile_id: str) -> IdeaBlueprint:
    """Compile a stable profile plan bound to an idea seed."""

    if not isinstance(seed, IdeaSeed):
        raise TypeError("seed must be an IdeaSeed")
    zones = build_zone_plan(profile_id)
    profile = profile_id.strip().lower()
    profile_record = get_idea_profile(profile)
    profile_version = "1"
    zone_contract_hash = canonical_hash(
        {
            "zones": [spec.__dict__ for spec in ZONE_SPECS],
            "profile": profile_record.id,
            "families": profile_record.artifact_families,
        }
    )
    blueprint_id = f"idea-blueprint:{profile}:{profile_version}:{seed.raw_hash}"
    return IdeaBlueprint(
        id=blueprint_id,
        seed_hash=seed.raw_hash,
        profile_id=profile,
        profile_version=profile_version,
        zone_contract_hash=zone_contract_hash,
        zones=zones,
        metadata=MappingProxyType(
            {
                "compiler": "idea_blueprint.v1",
                "resume_safe": True,
                "approval_required_before_apply": True,
            }
        ),
    )


__all__ = ["IdeaBlueprint", "build_idea_blueprint"]

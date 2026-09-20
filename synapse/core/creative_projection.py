"""Proposal-only projection from creative map envelopes to existing structure maps."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from synapse.core.creative_contracts import validate_global_map_envelope, validate_zone_envelope
from synapse.core.idea_session import ProposalMetadata
from synapse.core.structure_map import StructureEdge, StructureMapProposal, StructureNode, WorkUnit

_ZONE_NODE_KIND = {
    "world": "context", "characters": "entity", "relations": "relation",
    "plot": "work", "chapters": "work", "foreshadowing": "work",
    "scenes": "work", "script_data_reactive": "work", "continuity": "rule",
}
_ZONE_ARTIFACT_ROLE = {
    "world": "premises", "characters": "entities", "relations": "relations",
    "plot": "outline", "chapters": "outline", "foreshadowing": "outline",
    "scenes": "outline", "script_data_reactive": "inputs_outputs", "continuity": "continuity",
}


def project_creative_map(
    global_map: Mapping[str, Any],
    zones: Sequence[Mapping[str, Any]],
    *,
    owner: str = "human-ui",
    source: str = "creative-projection",
) -> StructureMapProposal:
    """Convert validated creative envelopes into a locked, proposal-only map."""

    bound = validate_global_map_envelope(global_map)
    if len(zones) != len(bound["zone_plan"]):
        raise ValueError("zone envelopes must cover the complete zone_plan")
    validated = [validate_zone_envelope(zone, global_map=global_map) for zone in zones]
    by_type = {zone["zone_type"]: zone for zone in validated}
    if set(by_type) != set(bound["zone_plan"]):
        raise ValueError("zone envelopes must match zone_plan exactly")

    map_id = bound["map_id"]
    nodes: list[StructureNode] = []
    for index, invariant in enumerate(bound["invariants"]):
        nodes.append(StructureNode(
            id=f"{map_id}:invariant:{index + 1}", kind="rule", title=invariant,
            purpose="Creative map invariant", required=True,
            source_refs=(map_id,), completion_criteria="Invariant remains consistent across all zones",
            artifact_role="invariants",
        ))
    for zone_type in bound["zone_plan"]:
        nodes.append(StructureNode(
            id=f"{map_id}:zone:{zone_type}", kind=_ZONE_NODE_KIND[zone_type],
            title=f"Creative zone: {zone_type}", purpose=f"Generate and validate the {zone_type} zone",
            required=True, source_refs=tuple(by_type[zone_type]["source_refs"]),
            completion_criteria="Normal, boundary, and failure validation cases pass",
            artifact_role=_ZONE_ARTIFACT_ROLE[zone_type],
        ))

    edges: list[StructureEdge] = []
    invariant_ids = tuple(node.id for node in nodes if ":invariant:" in node.id)
    previous_zone_id: str | None = None
    for zone_type in bound["zone_plan"]:
        zone_id = f"{map_id}:zone:{zone_type}"
        for invariant_id in invariant_ids:
            edges.append(StructureEdge(source_id=invariant_id, target_id=zone_id, relation="constrains"))
        if previous_zone_id:
            edges.append(StructureEdge(source_id=zone_id, target_id=previous_zone_id, relation="depends_on"))
        previous_zone_id = zone_id

    work_units: list[WorkUnit] = []
    previous_work_id: str | None = None
    for zone_type in bound["zone_plan"]:
        zone_id = f"{map_id}:zone:{zone_type}"
        work_id = f"{map_id}:work:{zone_type}"
        work_units.append(WorkUnit(
            id=work_id, title=f"Generate {zone_type} zone",
            purpose=f"Produce a validated {zone_type} proposal bound to the creative map",
            prerequisites=(previous_work_id,) if previous_work_id else (),
            source_node_ids=(zone_id,) + invariant_ids,
            artifact_roles=(_ZONE_ARTIFACT_ROLE[zone_type], "tests"),
            completion_criteria="Zone envelope and all three validation cases pass",
            downstream_impacts=(f"{map_id}:zone:{bound['zone_plan'][bound['zone_plan'].index(zone_type) + 1]}",)
            if zone_type != bound["zone_plan"][-1] else (),
            status="locked",
        ))
        previous_work_id = work_id

    proposal_id = f"creative-structure:{map_id}:r{bound['revision']}"
    metadata = ProposalMetadata(
        schema_version="creative.structure-projection.v1", id=proposal_id,
        canonical_key=f"creative-structure:{proposal_id}", owner=owner, source=source,
        provenance={"creative_map_id": map_id, "creative_map_hash": bound["map_hash"]},
    )
    return StructureMapProposal(
        metadata=metadata, seed_hash=bound["source_seed_hash"], expansion_id=map_id,
        revision=int(bound["revision"] or 1), parent_map_hash=None, preset_hint="creative-novel",
        nodes=tuple(nodes), edges=tuple(edges), work_units=tuple(work_units),
        unresolved=(), next_recommended_action={"kind": "review_creative_projection"},
    )


__all__ = ["project_creative_map"]

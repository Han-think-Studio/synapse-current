"""Cross-zone consistency checks before synthesis."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from synapse.core.creative_contracts import (
    CREATIVE_REFERENCE_RELATIONS,
    CreativeContractError,
    validate_global_map_envelope,
    validate_precontract_materialization,
    validate_zone_envelope,
)


def validate_cross_zone_consistency(global_map: Mapping[str, Any], zones: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    bound = validate_global_map_envelope(global_map)
    if len(zones) != len(bound["zone_plan"]):
        raise CreativeContractError("cross-zone coverage does not match zone_plan")
    validated = [validate_zone_envelope(zone, global_map=global_map) for zone in zones]
    types = [zone["zone_type"] for zone in validated]
    if tuple(types) != tuple(bound["zone_plan"]):
        raise CreativeContractError("cross-zone order does not match zone_plan")
    ids = [zone["zone_id"] for zone in validated]
    if len(set(ids)) != len(ids):
        raise CreativeContractError("cross-zone zone_id values must be unique")
    known_ids: dict[str, set[str]] = {zone["zone_type"]: set() for zone in validated}
    all_links: list[tuple[str, str]] = []
    for zone in validated:
        if bound["precontract"] is not None:
            validate_precontract_materialization(
                zone["zone_type"], zone["outputs"], global_map=global_map
            )
        declared_ids = zone["outputs"].get("declared_ids", [])
        if not isinstance(declared_ids, list) or any(not isinstance(item, str) or not item.strip() for item in declared_ids):
            raise CreativeContractError(f"declared_ids must be a non-empty string array: {zone['zone_type']}")
        known_ids[zone["zone_type"]].update(item.strip() for item in declared_ids)

        def collect(value: Any, *, zone_type: str) -> None:
            if isinstance(value, Mapping):
                if isinstance(value.get("id"), str) and value["id"].strip():
                    known_ids[zone_type].add(value["id"].strip())
                for child in value.values():
                    collect(child, zone_type=zone_type)
            elif isinstance(value, list):
                for child in value:
                    collect(child, zone_type=zone_type)
        collect(zone["outputs"], zone_type=zone["zone_type"])
    for zone in validated:
        links = zone["outputs"].get("cross_refs", [])
        if not isinstance(links, list):
            raise CreativeContractError("cross_refs must be an array")
        for link in links:
            if not isinstance(link, Mapping):
                raise CreativeContractError("cross_refs items must be objects")
            source_zone, target_zone = link.get("source_zone"), link.get("target_zone")
            source_id, target_id = link.get("source_id"), link.get("target_id")
            if (
                not isinstance(source_zone, str)
                or source_zone != zone["zone_type"]
                or not isinstance(source_id, str)
                or source_id not in known_ids.get(source_zone, set())
            ):
                raise CreativeContractError("cross_refs source is unknown")
            if (
                not isinstance(target_zone, str)
                or target_zone not in known_ids
                or not isinstance(target_id, str)
                or target_id not in known_ids.get(target_zone, set())
            ):
                raise CreativeContractError("cross_refs target is unknown")
            relation = link.get("relation", "depends_on")
            if not isinstance(relation, str) or relation not in CREATIVE_REFERENCE_RELATIONS:
                raise CreativeContractError("cross_refs relation is unsupported")
            all_links.append((source_zone, target_zone))
    if bound["precontract"] is None:
        # Legacy v1 maps remain inspectable, but their conditional link check is
        # not sufficient for the v2 review gate and never grants quality-ready.
        required_pairs = (("characters", "plot"), ("plot", "chapters"), ("foreshadowing", "chapters"), ("chapters", "scenes"), ("scenes", "script_data_reactive"))
        for source_zone, target_zone in required_pairs:
            if known_ids.get(source_zone) and known_ids.get(target_zone) and (source_zone, target_zone) not in all_links:
                raise CreativeContractError(f"missing semantic link {source_zone}->{target_zone}")
    return {"map_id": bound["map_id"], "map_hash": bound["map_hash"], "zone_types": types, "zone_ids": ids, "consistent": True}


__all__ = ["validate_cross_zone_consistency"]

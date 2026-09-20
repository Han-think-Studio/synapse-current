"""Optional typed checks for the first rich creative output layer."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from synapse.core.creative_contracts import ZONE_OUTPUT_FIELDS, CreativeContractError


def validate_rich_zone_outputs(zone_type: str, outputs: Mapping[str, Any]) -> dict[str, Any]:
    """Validate world/character output containers without constraining prose."""

    if zone_type not in {"world", "characters", "relations", "plot", "chapters", "foreshadowing", "scenes", "script_data_reactive", "continuity"}:
        return dict(outputs)
    if not isinstance(outputs, Mapping):
        raise CreativeContractError("typed zone outputs must be an object")
    required = ZONE_OUTPUT_FIELDS[zone_type]
    for key in required:
        value = outputs.get(key)
        if key == "exit_condition" and zone_type == "scenes":
            if not isinstance(value, str) or not value.strip():
                raise CreativeContractError("typed outputs.exit_condition must be a non-empty string")
            continue
        if not isinstance(value, list):
            raise CreativeContractError(f"typed outputs.{key} must be an array")
        if any(not isinstance(item, (str, Mapping)) for item in value):
            raise CreativeContractError(f"typed outputs.{key} items must be strings or objects")
        object_ids = []
        for item in value:
            if isinstance(item, Mapping):
                identifier = item.get("id")
                if not isinstance(identifier, str) or not identifier.strip():
                    raise CreativeContractError(f"typed outputs.{key} objects require a non-empty id")
                object_ids.append(identifier.strip())
        if len(set(object_ids)) != len(object_ids):
            raise CreativeContractError(f"typed outputs.{key} contains duplicate ids")
    if zone_type == "relations":
        entity_ids = {item["id"].strip() for item in outputs["entities"] if isinstance(item, Mapping) and isinstance(item.get("id"), str)}
        for change in outputs["relationship_changes"]:
            if isinstance(change, Mapping):
                for field in ("from_id", "to_id"):
                    if field in change and (
                        not isinstance(change[field], str)
                        or not change[field].strip()
                        or change[field] not in entity_ids
                    ):
                        raise CreativeContractError(f"typed outputs.relationship_changes references unknown {field}")
    if zone_type == "plot":
        beat_ids = {item["id"].strip() for item in outputs["beats"] if isinstance(item, Mapping) and isinstance(item.get("id"), str)}
        for edge in outputs["causal_edges"]:
            if isinstance(edge, Mapping):
                for field in ("from_id", "to_id"):
                    if field in edge and (
                        not isinstance(edge[field], str)
                        or not edge[field].strip()
                        or edge[field] not in beat_ids
                    ):
                        raise CreativeContractError(f"typed outputs.causal_edges references unknown {field}")
    if zone_type == "chapters":
        chapter_ids = {item["id"].strip() for item in outputs["chapters"] if isinstance(item, Mapping) and isinstance(item.get("id"), str)}
        for key in ("reveals", "payoffs"):
            for item in outputs[key]:
                if isinstance(item, Mapping) and "chapter_id" in item:
                    chapter_id = item["chapter_id"]
                    if not isinstance(chapter_id, str) or not chapter_id.strip() or chapter_id not in chapter_ids:
                        raise CreativeContractError(f"typed outputs.{key} references unknown chapter_id")
    if zone_type == "foreshadowing":
        setup_ids = {item["id"].strip() for item in outputs["setups"] if isinstance(item, Mapping) and isinstance(item.get("id"), str)}
        for item in outputs["payoffs"]:
            if isinstance(item, Mapping) and "setup_id" in item:
                setup_id = item["setup_id"]
                if not isinstance(setup_id, str) or not setup_id.strip() or setup_id not in setup_ids:
                    raise CreativeContractError("typed outputs.payoffs references unknown setup_id")
    if zone_type == "scenes":
        for key in ("inputs", "outputs"):
            for item in outputs[key]:
                if isinstance(item, Mapping) and "id" in item and (not isinstance(item["id"], str) or not item["id"].strip()):
                    raise CreativeContractError(f"typed outputs.{key} object id must be non-empty")
    if zone_type == "script_data_reactive":
        entity_ids = {item["id"].strip() for item in outputs["data_entities"] if isinstance(item, Mapping) and isinstance(item.get("id"), str)}
        for transition in outputs["state_transitions"]:
            if isinstance(transition, Mapping) and "entity_id" in transition:
                entity_id = transition["entity_id"]
                if not isinstance(entity_id, str) or not entity_id.strip() or entity_id not in entity_ids:
                    raise CreativeContractError("typed outputs.state_transitions references unknown entity_id")
    if zone_type == "continuity":
        for key in ("timeline", "consistency_checks"):
            ids = []
            for item in outputs[key]:
                if isinstance(item, Mapping):
                    identifier = item.get("id")
                    if not isinstance(identifier, str) or not identifier.strip():
                        raise CreativeContractError(f"typed outputs.{key} objects require a non-empty id")
                    ids.append(identifier.strip())
            if len(set(ids)) != len(ids):
                raise CreativeContractError(f"typed outputs.{key} contains duplicate ids")
    return dict(outputs)


__all__ = ["validate_rich_zone_outputs"]

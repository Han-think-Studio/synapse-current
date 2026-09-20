"""Bounded contracts for novel-grade map and zone proposals.

This module validates proposal envelopes only. It does not mutate canonical
state, create files, call a provider, or approve model output.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from synapse.core.idea_session import canonical_hash


class CreativeContractError(ValueError):
    """Raised when a creative map/zone proposal violates its envelope."""


DETAILED_REACTIVE_CONTRACT_VERSION = "creative.reactive.v1"
MINIMUM_CONTENT_PLACEHOLDERS = frozenset({
    "todo", "tbd", "pending", "placeholder", "example", "n/a", "na", "none", "null",
    "미정", "예시", "추후 작성", "나중에 작성",
})
_OUTPUT_METADATA_KEYS = frozenset({
    "id", "status", "type", "kind", "schema_version", "version", "revision",
    "map_id", "map_hash", "zone_id", "created_at", "updated_at",
})
_RELATION_OUTPUT_FIELDS = frozenset({"relationship_changes", "causal_edges"})


def _contains_output_placeholder(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in MINIMUM_CONTENT_PLACEHOLDERS
    if isinstance(value, Mapping):
        return any(_contains_output_placeholder(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_output_placeholder(item) for item in value)
    return False


def _has_substantive_content(value: Any, *, output_field: str | None = None) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        metadata_keys = _OUTPUT_METADATA_KEYS
        if output_field in _RELATION_OUTPUT_FIELDS:
            metadata_keys = metadata_keys - {"type", "kind"}
        return any(
            _has_substantive_content(item, output_field=output_field)
            for key, item in value.items()
            if str(key).strip().casefold() not in metadata_keys
            and not str(key).strip().casefold().endswith(("_id", "_ids", "_ref", "_refs", "_hash", "_hashes"))
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_has_substantive_content(item, output_field=output_field) for item in value)
    return value is not None


def has_minimum_creative_content(value: Any, *, output_field: str | None = None) -> bool:
    """Apply the shared deterministic minimum-content rule to one value."""

    return not _contains_output_placeholder(value) and _has_substantive_content(
        value, output_field=output_field
    )


ZONE_TYPES = {"world", "characters", "relations", "plot", "chapters", "foreshadowing", "scenes", "script_data_reactive", "continuity"}
ZONE_STATUSES = {"PROPOSED", "REJECTED", "VERIFIED"}
CANONICAL_ZONE_ORDER = ("world", "characters", "relations", "plot", "chapters", "foreshadowing", "scenes", "script_data_reactive", "continuity")
REQUIRED_CROSS_ZONE_LINKS = (
    {"source_zone": "characters", "source_output": "entities", "target_zone": "plot", "target_output": "beats"},
    {"source_zone": "plot", "source_output": "beats", "target_zone": "chapters", "target_output": "chapters"},
    {"source_zone": "foreshadowing", "source_output": "setups", "target_zone": "chapters", "target_output": "chapters"},
    {"source_zone": "chapters", "source_output": "chapters", "target_zone": "scenes", "target_output": "outputs"},
    {"source_zone": "scenes", "source_output": "outputs", "target_zone": "script_data_reactive", "target_output": "data_entities"},
)
CREATIVE_REFERENCE_RELATIONS = frozenset({
    "defines", "constrains", "depends_on", "transforms", "reveals", "pays_off",
    "consumes", "emits", "updates", "informs",
})
PRIOR_ID_LIMIT = 64
PRIOR_ID_BYTES_LIMIT = 8 * 1024
ZONE_OUTPUT_FIELDS = {
    "world": ("locations", "rules", "history", "resources", "constraints"),
    "characters": ("entities", "needs_wants", "secrets", "arcs", "choice_points"),
    "relations": ("entities", "relationship_changes", "conflicts"),
    "plot": ("beats", "causal_edges", "turning_points"),
    "chapters": ("chapters", "reveals", "payoffs"),
    "continuity": ("timeline", "consistency_checks"),
    "scenes": ("inputs", "outputs", "exit_condition"),
    "script_data_reactive": ("data_entities", "state_transitions", "triggers", "handlers", "interfaces", "validation_cases"),
    "foreshadowing": ("setups", "payoffs"),
}


def build_creative_frame_contract(zone_plan: Sequence[str]) -> dict[str, Any]:
    """Return the code-owned schema the global-map stage must precontract."""

    if not isinstance(zone_plan, list | tuple) or not zone_plan:
        raise CreativeContractError("frame contract zone_plan must be a non-empty string array")
    planned = list(zone_plan)
    if any(not isinstance(item, str) or item not in ZONE_TYPES for item in planned):
        raise CreativeContractError("frame contract zone_plan contains unsupported zone type")
    if len(set(planned)) != len(planned):
        raise CreativeContractError("frame contract zone_plan contains duplicates")
    positions = [CANONICAL_ZONE_ORDER.index(item) for item in planned]
    if positions != sorted(positions):
        raise CreativeContractError("frame contract zone_plan must follow canonical zone order")

    order = {zone_type: index for index, zone_type in enumerate(planned)}
    required_links = [
        dict(item)
        for item in REQUIRED_CROSS_ZONE_LINKS
        if item["source_zone"] in order and item["target_zone"] in order
    ]
    dependency_sets: dict[str, set[str]] = {zone_type: set() for zone_type in planned}
    required_id_slots: set[tuple[str, str]] = set()
    for item in required_links:
        source_zone, target_zone = item["source_zone"], item["target_zone"]
        required_id_slots.add((source_zone, item["source_output"]))
        required_id_slots.add((target_zone, item["target_output"]))
        earlier, later = sorted((source_zone, target_zone), key=order.__getitem__)
        dependency_sets[later].add(earlier)
    payload = {
        "schema_version": "creative.frame-contract.v1",
        "zone_plan": planned,
        "required_output_fields": {
            zone_type: [*ZONE_OUTPUT_FIELDS[zone_type], "invariant_refs"]
            for zone_type in planned
        },
        "required_validation_cases": ["normal", "boundary", "failure"],
        "required_id_slots": [
            {"zone_type": zone_type, "output_field": output_field}
            for zone_type, output_field in sorted(required_id_slots, key=lambda item: (order[item[0]], item[1]))
        ],
        "required_link_pairs": required_links,
        "required_generation_dependencies": [
            {"zone_type": zone_type, "requires": sorted(dependency_sets[zone_type], key=order.__getitem__)}
            for zone_type in planned
        ],
        "allowed_relations": sorted(CREATIVE_REFERENCE_RELATIONS),
        "prior_id_limit": PRIOR_ID_LIMIT,
        "prior_id_bytes_limit": PRIOR_ID_BYTES_LIMIT,
    }
    return {**payload, "contract_hash": canonical_hash(payload)}


def _validate_precontract(value: Any, *, zone_plan: Sequence[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CreativeContractError("global map v2 requires a precontract object")
    if set(value) != {"id_slots", "generation_dependencies", "required_links"}:
        raise CreativeContractError("precontract fields must be id_slots, generation_dependencies, required_links")
    planned = list(zone_plan)
    order = {zone_type: index for index, zone_type in enumerate(planned)}
    frame = build_creative_frame_contract(planned)

    raw_slots = value["id_slots"]
    if not isinstance(raw_slots, list) or len(raw_slots) > PRIOR_ID_LIMIT:
        raise CreativeContractError(f"precontract id_slots must contain 0..{PRIOR_ID_LIMIT} entries")
    slots: list[dict[str, str]] = []
    slot_fields: dict[tuple[str, str], set[str]] = {}
    seen_ids: set[str] = set()
    for raw in raw_slots:
        if not isinstance(raw, Mapping) or set(raw) != {"zone_type", "output_field", "id"}:
            raise CreativeContractError("precontract id_slot fields must be zone_type, output_field, id")
        zone_type = _text(raw.get("zone_type"), "precontract.id_slot.zone_type")
        output_field = _text(raw.get("output_field"), "precontract.id_slot.output_field")
        identifier = _text(raw.get("id"), "precontract.id_slot.id", limit=160)
        if zone_type not in order or output_field not in ZONE_OUTPUT_FIELDS[zone_type]:
            raise CreativeContractError("precontract id_slot must target a required zone output field")
        if identifier.casefold() in MINIMUM_CONTENT_PLACEHOLDERS:
            raise CreativeContractError("precontract id_slot id must not be a placeholder")
        if identifier in seen_ids:
            raise CreativeContractError("precontract id_slot IDs must be globally unique")
        seen_ids.add(identifier)
        slot_fields.setdefault((zone_type, output_field), set()).add(identifier)
        slots.append({"zone_type": zone_type, "output_field": output_field, "id": identifier})
    slots.sort(key=lambda item: (order[item["zone_type"]], item["output_field"], item["id"]))

    for required_slot in frame["required_id_slots"]:
        key = (required_slot["zone_type"], required_slot["output_field"])
        if not slot_fields.get(key):
            raise CreativeContractError(f"precontract is missing required ID slot {key[0]}.{key[1]}")

    raw_dependencies = value["generation_dependencies"]
    if not isinstance(raw_dependencies, list) or len(raw_dependencies) != len(planned):
        raise CreativeContractError("precontract generation_dependencies must cover every planned zone")
    dependencies: list[dict[str, Any]] = []
    dependency_by_zone: dict[str, set[str]] = {}
    for raw in raw_dependencies:
        if not isinstance(raw, Mapping) or set(raw) != {"zone_type", "requires"}:
            raise CreativeContractError("precontract dependency fields must be zone_type and requires")
        zone_type = _text(raw.get("zone_type"), "precontract.dependency.zone_type")
        if zone_type not in order or zone_type in dependency_by_zone:
            raise CreativeContractError("precontract generation dependency has an unknown or duplicate zone")
        requires = _strings(raw.get("requires"), f"precontract.generation_dependencies.{zone_type}.requires")
        required_set = set(requires)
        if len(required_set) != len(requires) or any(item not in order or order[item] >= order[zone_type] for item in requires):
            raise CreativeContractError("precontract dependencies must be unique earlier planned zones")
        dependency_by_zone[zone_type] = required_set
        dependencies.append({"zone_type": zone_type, "requires": sorted(required_set, key=order.__getitem__)})
    if set(dependency_by_zone) != set(planned):
        raise CreativeContractError("precontract generation_dependencies must cover every planned zone")
    for required in frame["required_generation_dependencies"]:
        if set(required["requires"]) != dependency_by_zone[required["zone_type"]]:
            raise CreativeContractError("precontract generation dependencies do not match the frame contract")
    dependencies.sort(key=lambda item: order[item["zone_type"]])

    raw_links = value["required_links"]
    if not isinstance(raw_links, list) or len(raw_links) > PRIOR_ID_LIMIT:
        raise CreativeContractError("precontract required_links count is outside its bounded range")
    links: list[dict[str, str]] = []
    seen_links: set[tuple[str, str, str, str, str]] = set()
    for raw in raw_links:
        keys = {"source_zone", "source_id", "target_zone", "target_id", "relation"}
        if not isinstance(raw, Mapping) or set(raw) != keys:
            raise CreativeContractError("precontract required_link fields are incomplete or unknown")
        link = {key: _text(raw.get(key), f"precontract.required_link.{key}", limit=160) for key in keys}
        source_zone, target_zone = link["source_zone"], link["target_zone"]
        if source_zone not in order or target_zone not in order or source_zone == target_zone:
            raise CreativeContractError("precontract required_link zones must be distinct planned zones")
        if link["relation"] not in CREATIVE_REFERENCE_RELATIONS:
            raise CreativeContractError("precontract required_link relation is unsupported")
        if link["source_id"].casefold() in MINIMUM_CONTENT_PLACEHOLDERS or link["target_id"].casefold() in MINIMUM_CONTENT_PLACEHOLDERS:
            raise CreativeContractError("precontract required_link IDs must not be placeholders")
        if not any(slot["zone_type"] == source_zone and slot["id"] == link["source_id"] for slot in slots):
            raise CreativeContractError("precontract required_link source ID has no matching id_slot")
        if not any(slot["zone_type"] == target_zone and slot["id"] == link["target_id"] for slot in slots):
            raise CreativeContractError("precontract required_link target ID has no matching id_slot")
        identity = (source_zone, link["source_id"], target_zone, link["target_id"], link["relation"])
        if identity in seen_links:
            raise CreativeContractError("precontract required_links contains duplicates")
        seen_links.add(identity)
        links.append(link)
    links.sort(key=lambda item: (order[item["source_zone"]], order[item["target_zone"]], item["source_id"], item["target_id"], item["relation"]))

    for required_pair in frame["required_link_pairs"]:
        pair_links = [
            link for link in links
            if link["source_zone"] == required_pair["source_zone"]
            and link["target_zone"] == required_pair["target_zone"]
        ]
        if not pair_links:
            raise CreativeContractError(
                f"precontract is missing required link pair {required_pair['source_zone']}->{required_pair['target_zone']}"
            )
        source_ids = slot_fields.get((required_pair["source_zone"], required_pair["source_output"]), set())
        target_ids = slot_fields.get((required_pair["target_zone"], required_pair["target_output"]), set())
        if any(link["source_id"] not in source_ids or link["target_id"] not in target_ids for link in pair_links):
            raise CreativeContractError("precontract required_link must target the pair's declared output slots")

    normalized = {"id_slots": slots, "generation_dependencies": dependencies, "required_links": links}
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > PRIOR_ID_BYTES_LIMIT:
        raise CreativeContractError("precontract exceeds its 8 KiB UTF-8 budget")
    return normalized


def _text(value: Any, label: str, *, limit: int = 2_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CreativeContractError(f"{label} must be a non-empty string")
    result = value.strip()
    if len(result) > limit:
        raise CreativeContractError(f"{label} is too long")
    return result


def _strings(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise CreativeContractError(f"{label} must be a string array")
    normalized = tuple(item.strip() for item in value)
    if len(set(normalized)) != len(normalized):
        raise CreativeContractError(f"{label} must not contain duplicates")
    return normalized


def validate_global_map_envelope(
    value: Mapping[str, Any], *, require_precontract: bool = False
) -> dict[str, Any]:
    """Validate a creative map and its optional legacy-compatible precontract."""

    if not isinstance(value, Mapping):
        raise CreativeContractError("global map must be an object")
    schema = _text(value.get("schema_version"), "schema_version")
    if schema not in {"creative.global-map.v1", "creative.global-map.v2"}:
        raise CreativeContractError("unsupported global map schema")
    if require_precontract and schema != "creative.global-map.v2":
        raise CreativeContractError("GLOBAL_MAP_PRECONTRACT_REQUIRED: generated maps must use creative.global-map.v2")
    status = _text(value.get("status"), "status")
    if status not in ZONE_STATUSES:
        raise CreativeContractError("invalid global map status")
    zone_plan = _strings(value.get("zone_plan"), "zone_plan")
    if any(item not in ZONE_TYPES for item in zone_plan):
        raise CreativeContractError("zone_plan contains unsupported zone type")
    positions = [CANONICAL_ZONE_ORDER.index(item) for item in zone_plan]
    if positions != sorted(positions):
        raise CreativeContractError("zone_plan must follow canonical zone order")
    invariants = _strings(value.get("invariants"), "invariants")
    if len(invariants) < 3:
        raise CreativeContractError("global map requires at least three invariants")
    if any(item.casefold() in MINIMUM_CONTENT_PLACEHOLDERS for item in invariants):
        raise CreativeContractError("global map invariants must be substantive, not placeholders")
    revision = value.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise CreativeContractError("revision must be an integer >= 1")
    if len(set(zone_plan)) != len(zone_plan):
        raise CreativeContractError("zone_plan contains duplicates")
    normalized = {
        "schema_version": schema,
        "map_id": _text(value.get("map_id"), "map_id"),
        "map_hash": _text(value.get("map_hash"), "map_hash"),
        "source_seed_hash": _text(value.get("source_seed_hash"), "source_seed_hash"),
        "revision": revision,
        "invariants": invariants,
        "zone_plan": zone_plan,
        "status": status,
    }
    if schema == "creative.global-map.v1":
        normalized["precontract"] = None
        normalized["precontract_hash"] = None
        return normalized

    precontract = _validate_precontract(value.get("precontract"), zone_plan=zone_plan)
    hash_payload = {
        "map_id": normalized["map_id"],
        "map_hash": normalized["map_hash"],
        "source_seed_hash": normalized["source_seed_hash"],
        "revision": normalized["revision"],
        "invariants": list(invariants),
        "zone_plan": list(zone_plan),
        "precontract": precontract,
    }
    normalized["precontract"] = precontract
    normalized["precontract_hash"] = canonical_hash(hash_payload)
    return normalized


def inspect_precontract_materialization(
    zone_type: str,
    outputs: Mapping[str, Any],
    *,
    global_map: Mapping[str, Any],
    verified_prior_zones: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[tuple[dict[str, str], ...], tuple[str, ...]]:
    """Return valid materialized slots and every slot/link blocker for one zone."""

    bound = validate_global_map_envelope(global_map, require_precontract=True)
    if zone_type not in bound["zone_plan"]:
        raise CreativeContractError(f"precontract zone is not planned: {zone_type}")
    planned_zones = set(bound["zone_plan"])
    materialized: list[dict[str, str]] = []
    errors: list[str] = []
    for slot in bound["precontract"]["id_slots"]:
        if slot["zone_type"] != zone_type:
            continue
        id_count = _object_id_counts(outputs.get(slot["output_field"])).get(slot["id"], 0)
        if id_count == 0:
            errors.append(f"PRECONTRACT_ID_SLOT_MISSING: {zone_type}.{slot['output_field']}:{slot['id']}")
        elif id_count > 1:
            errors.append(f"PRECONTRACT_ID_SLOT_DUPLICATED: {zone_type}.{slot['output_field']}:{slot['id']}")
        slot_records = outputs.get(slot["output_field"])
        slot_records = slot_records if isinstance(slot_records, list | tuple) else (slot_records,)
        slot_record = next(
            (
                item for item in slot_records
                if isinstance(item, Mapping)
                and isinstance(item.get("id"), str)
                and item["id"].strip() == slot["id"]
            ),
            None,
        )
        has_content = has_minimum_creative_content(slot_record, output_field=slot["output_field"])
        if id_count and not has_content:
            errors.append(
                "PRECONTRACT_ID_SLOT_CONTENT_MISSING: "
                f"{zone_type}.{slot['output_field']}:{slot['id']}"
            )
        misplaced_fields = [
            field for field, values in outputs.items()
            if field not in {slot["output_field"], "cross_refs"}
            and _object_id_counts(values).get(slot["id"], 0)
        ]
        if misplaced_fields:
            errors.append(
                f"PRECONTRACT_ID_SLOT_WRONG_FIELD: {zone_type}.{slot['output_field']}:{slot['id']} "
                f"also appears in {', '.join(misplaced_fields)}"
            )
        if id_count == 1 and has_content and not misplaced_fields:
            materialized.append(slot)
    expected_links = [
        link for link in bound["precontract"]["required_links"] if link["source_zone"] == zone_type
    ]
    raw_links = outputs.get("cross_refs", [])
    if not isinstance(raw_links, list):
        errors.append(f"precontract cross_refs must be an array: {zone_type}")
        raw_links = []
    if verified_prior_zones is not None:
        current_ids = _collect_zone_output_ids(outputs)
        prior_ids = {
            prior_zone["zone_type"]: _collect_zone_output_ids(prior_zone["outputs"])
            for prior_zone in verified_prior_zones
            if isinstance(prior_zone, Mapping)
            and isinstance(prior_zone.get("zone_type"), str)
            and isinstance(prior_zone.get("outputs"), Mapping)
        }
        predeclared_ids: dict[str, set[str]] = {}
        for slot in bound["precontract"]["id_slots"]:
            predeclared_ids.setdefault(slot["zone_type"], set()).add(slot["id"])
        link_fields = {"source_zone", "source_id", "target_zone", "target_id", "relation"}
        for index, link in enumerate(raw_links):
            if not isinstance(link, Mapping) or set(link) != link_fields:
                errors.append(f"PRECONTRACT_CROSS_REF_INVALID: {zone_type}[{index}] fields")
                continue
            source_zone = link.get("source_zone")
            source_id = link.get("source_id")
            target_zone = link.get("target_zone")
            target_id = link.get("target_id")
            relation = link.get("relation")
            if (
                not all(isinstance(item, str) and item.strip() for item in (source_zone, source_id, target_zone, target_id, relation))
                or source_zone != zone_type
            ):
                errors.append(f"PRECONTRACT_CROSS_REF_INVALID: {zone_type}[{index}] identity")
                continue
            if source_id not in current_ids:
                errors.append(f"PRECONTRACT_CROSS_REF_SOURCE_UNKNOWN: {zone_type}:{source_id}")
                continue
            if target_zone not in planned_zones or target_zone == zone_type:
                errors.append(f"PRECONTRACT_CROSS_REF_TARGET_ZONE_UNKNOWN: {zone_type}:{target_zone}")
                continue
            if relation not in CREATIVE_REFERENCE_RELATIONS:
                errors.append(f"PRECONTRACT_CROSS_REF_RELATION_UNSUPPORTED: {zone_type}:{relation}")
                continue
            allowed_target_ids = predeclared_ids.get(target_zone, set()) | prior_ids.get(target_zone, set())
            if target_id not in allowed_target_ids:
                errors.append(f"PRECONTRACT_CROSS_REF_TARGET_UNKNOWN: {zone_type}:{target_zone}:{target_id}")
    for expected in expected_links:
        if any(
            isinstance(item, Mapping)
            and item.get("source_zone") == expected["source_zone"]
            and item.get("source_id") == expected["source_id"]
            and item.get("target_zone") == expected["target_zone"]
            and item.get("target_id") == expected["target_id"]
            and item.get("relation") == expected["relation"]
            for item in raw_links
        ):
            continue
        errors.append(
            "PRECONTRACT_LINK_MISSING: "
            f"{expected['source_zone']}:{expected['source_id']}->"
            f"{expected['target_zone']}:{expected['target_id']}"
        )
    return tuple(materialized), tuple(errors)


def _collect_zone_output_ids(outputs: Mapping[str, Any]) -> set[str]:
    """Collect IDs authored by one zone, excluding IDs mentioned only in links."""

    identifiers: set[str] = set()
    declared = outputs.get("declared_ids")
    if isinstance(declared, list):
        identifiers.update(item.strip() for item in declared if isinstance(item, str) and item.strip())

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            identifier = value.get("id")
            if isinstance(identifier, str) and identifier.strip():
                identifiers.add(identifier.strip())
            for child in value.values():
                visit(child)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for child in value:
                visit(child)

    for field, value in outputs.items():
        if field != "cross_refs":
            visit(value)
    return identifiers


def validate_precontract_materialization(
    zone_type: str,
    outputs: Mapping[str, Any],
    *,
    global_map: Mapping[str, Any],
    verified_prior_zones: Sequence[Mapping[str, Any]] | None = None,
) -> tuple[dict[str, str], ...]:
    """Validate one zone and preserve fail-fast behavior for generation callers."""

    materialized, errors = inspect_precontract_materialization(
        zone_type,
        outputs,
        global_map=global_map,
        verified_prior_zones=verified_prior_zones,
    )
    if errors:
        raise CreativeContractError(errors[0])
    return materialized


def _object_id_counts(value: Any) -> dict[str, int]:
    items = value if isinstance(value, list | tuple) else (value,)
    counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
            continue
        identifier = item["id"].strip()
        if identifier:
            counts[identifier] = counts.get(identifier, 0) + 1
    return counts


def validate_zone_envelope(value: Mapping[str, Any], *, global_map: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one zone's identity, map binding, and test-case envelope."""

    if not isinstance(value, Mapping):
        raise CreativeContractError("zone must be an object")
    bound = validate_global_map_envelope(global_map)
    if _text(value.get("schema_version"), "schema_version") != "creative.zone.v1":
        raise CreativeContractError("unsupported zone schema")
    zone_type = _text(value.get("zone_type"), "zone_type")
    if zone_type not in ZONE_TYPES:
        raise CreativeContractError("unsupported zone_type")
    if _text(value.get("map_id"), "map_id") != bound["map_id"]:
        raise CreativeContractError("zone map_id does not match global map")
    if _text(value.get("map_hash"), "map_hash") != bound["map_hash"]:
        raise CreativeContractError("zone map_hash does not match global map")
    status = _text(value.get("status"), "status")
    if status not in ZONE_STATUSES:
        raise CreativeContractError("invalid zone status")
    source_refs = _strings(value.get("source_refs"), "source_refs")
    if not source_refs or any(item.casefold() in MINIMUM_CONTENT_PLACEHOLDERS for item in source_refs):
        raise CreativeContractError("source_refs must contain at least one substantive reference")
    outputs = value.get("outputs")
    if not isinstance(outputs, Mapping):
        raise CreativeContractError("zone outputs must be an object")
    required_output_keys = ZONE_OUTPUT_FIELDS.get(zone_type, ())
    if bound["precontract"] is not None:
        required_output_keys = (*required_output_keys, "invariant_refs")
    missing_output_keys = [key for key in required_output_keys if key not in outputs]
    if missing_output_keys:
        raise CreativeContractError(f"zone outputs missing required fields: {missing_output_keys}")
    if bound["precontract"] is not None:
        invariant_refs = _strings(outputs.get("invariant_refs"), "outputs.invariant_refs")
        if not invariant_refs or any(item not in bound["invariants"] for item in invariant_refs):
            raise CreativeContractError("outputs.invariant_refs must cite one or more declared map invariants")
    validation = value.get("validation")
    if not isinstance(validation, Mapping):
        raise CreativeContractError("zone validation must be an object")
    for key in ("normal", "boundary", "failure"):
        _strings(validation.get(key), f"validation.{key}")
    reactive_contract = value.get("reactive_contract")
    if reactive_contract is not None and not isinstance(reactive_contract, Mapping):
        raise CreativeContractError("reactive_contract must be an object when supplied")
    return {
        "schema_version": "creative.zone.v1",
        "zone_id": _text(value.get("zone_id"), "zone_id"),
        "zone_type": zone_type,
        "map_id": bound["map_id"],
        "map_hash": bound["map_hash"],
        "source_refs": source_refs,
        "outputs": dict(outputs),
        "validation": {key: list(validation[key]) for key in ("normal", "boundary", "failure")},
        "reactive_contract": dict(reactive_contract) if isinstance(reactive_contract, Mapping) else None,
        "status": status,
    }


__all__ = [
    "CREATIVE_REFERENCE_RELATIONS",
    "MINIMUM_CONTENT_PLACEHOLDERS",
    "PRIOR_ID_BYTES_LIMIT",
    "PRIOR_ID_LIMIT",
    "REQUIRED_CROSS_ZONE_LINKS",
    "ZONE_OUTPUT_FIELDS",
    "CreativeContractError",
    "build_creative_frame_contract",
    "has_minimum_creative_content",
    "inspect_precontract_materialization",
    "validate_global_map_envelope",
    "validate_precontract_materialization",
    "validate_zone_envelope",
]

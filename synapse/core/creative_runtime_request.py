"""Provider-neutral request construction for creative stages."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from synapse.core.creative_contracts import (
    DETAILED_REACTIVE_CONTRACT_VERSION,
    ZONE_OUTPUT_FIELDS,
    build_creative_frame_contract,
)
from synapse.runtime.contracts import RuntimeRequest

DETAILED_REACTIVE_FIELDS = (
    "stable_id",
    "input_refs",
    "event_or_trigger",
    "guard_or_precondition",
    "effect_or_state_delta",
    "downstream_impact_refs",
    "normal_boundary_failure_cases",
)


def build_creative_stage_request(
    *,
    stage: str,
    model: str,
    seed_hash: str,
    payload: Mapping[str, Any],
    timeout_seconds: float = 30.0,
    max_tokens: int = 768,
) -> RuntimeRequest:
    if stage not in {"global_map", "zone"}:
        raise ValueError("unsupported creative stage")
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be an object")  # noqa: TRY004 - malformed request payloads use the public ValueError contract
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1 or max_tokens > 16_384:
        raise ValueError("max_tokens must be an integer between 1 and 16384")
    zone_type = payload.get("zone_type")
    zone_contract = payload.get("zone_contract")
    required_outputs = (
        tuple(zone_contract.get("required_output_fields", ()))
        if isinstance(zone_contract, Mapping)
        else ZONE_OUTPUT_FIELDS.get(zone_type, ())
    )
    required_links = (
        tuple(zone_contract.get("required_links", ()))
        if isinstance(zone_contract, Mapping)
        else ()
    )
    prompt_output_keys = required_outputs
    if "invariant_refs" not in prompt_output_keys:
        prompt_output_keys += ("invariant_refs",)
    if required_links:
        prompt_output_keys += ("cross_refs",)
    required_links_json = json.dumps(list(required_links), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    id_slots = (
        tuple(zone_contract.get("id_slots", ()))
        if isinstance(zone_contract, Mapping)
        else ()
    )
    id_slots_json = json.dumps(list(id_slots), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    detailed_reactive = payload.get("detailed_reactive") is True
    frame_contract = payload.get("frame_contract")
    if stage == "global_map" and not isinstance(frame_contract, Mapping):
        requested_zone_plan = payload.get("requested_zone_plan")
        frame_contract = build_creative_frame_contract(requested_zone_plan)
    legacy_instruction = (
        "Return exactly one compact JSON object with keys schema_version, map_id, map_hash, source_seed_hash, "
        "revision, invariants, zone_plan, status, precontract. Use schema_version creative.global-map.v2, revision 1, "
        "and status PROPOSED. zone_plan MUST be copied exactly from requested_zone_plan. The supplied frame_contract "
        "is Synapse-owned and binding: precontract.id_slots must materialize IDs only in the listed zone output fields; "
        "generation_dependencies must cover every planned zone and only name earlier zones; required_links must "
        "include at least one typed source/target ID link for every frame_contract.required_link_pairs entry. "
        "Every required link ID must resolve to a matching id_slot, and every required endpoint field must have an "
        "id_slot. Keep IDs unique and the precontract within the stated ID and UTF-8 size limits. "
        "The precontract object MUST contain exactly the keys id_slots, generation_dependencies, and required_links. "
        "Each id_slots item MUST contain exactly the keys zone_type, output_field, and id; do not use aliases such "
        "as zone, field, identifier, name, or extra keys. Each generation_dependencies item MUST contain exactly "
        "zone_type and requires. Each required_links item MUST contain exactly source_zone, source_id, target_zone, "
        "target_id, and relation. Copy those key names and nesting exactly from the contract; never emit a prose "
        "description, a second ID field, or a wrapper around any item. For example, an ID slot is shaped exactly as "
        "{\"zone_type\":\"characters\",\"output_field\":\"entities\",\"id\":\"character_id\"}. "
        "invariants MUST contain at least three concise, substantive, non-placeholder entries. "
        "Do not use empty arrays or exact placeholder values such as TODO, TBD, pending, placeholder, example, "
        "N/A, none, null, 미정, 예시, 추후 작성, or 나중에 작성 in required fields. "
        "When details are inferred rather than stated in the supplied seed or context, label them as proposals or "
        "assumptions in the relevant prose; do not present an inference as an established fact."
        if stage == "global_map"
        else f"Return exactly one compact JSON object with schema_version creative.zone.v1. Do not use markdown or explanations. "
        f"Generate only the {zone_type} zone bound to the supplied global map and zone_contract. Include keys zone_id, zone_type, map_id, "
        "map_hash, source_refs, outputs, validation, status. source_refs MUST be a string array such as [\"seed\"]. "
        "The top-level schema_version key is mandatory and its exact value MUST be \"creative.zone.v1\"; do not omit it. "
        "The top-level required keys are exactly schema_version, zone_id, zone_type, map_id, map_hash, source_refs, "
        "outputs, validation, and status. In the default contract, omit reactive_contract entirely; never emit "
        "reactive_contract: null. Add reactive_contract only when the explicit detailed contract is enabled. "
        f"outputs MUST contain these keys: {', '.join(prompt_output_keys)}. Every required outputs array, "
        "including invariant_refs, MUST contain at least one concise, substantive, non-placeholder entry. "
        f"For this {zone_type} zone, never return [] for any array field among: {', '.join(field for field in required_outputs if field != 'exit_condition')}. "
        "If the supplied context does not establish a fact, fill the field with a clearly labeled proposal or "
        "assumption string rather than leaving it empty. "
        "invariant_refs MUST copy one or more strings from global_map.invariants character-for-character, including "
        "final punctuation; do not paraphrase, shorten, or remove a period. Every required output array for this "
        "zone MUST be non-empty, and all three validation arrays MUST contain at least one string item. "
        "source_refs and each normal, boundary, and failure validation array also need at least one substantive entry. "
        "If exit_condition is required, it must be one concise, substantive, non-placeholder string. "
        "Never use empty arrays, blank strings, or exact placeholder values such as TODO, TBD, pending, placeholder, "
        "example, N/A, none, null, 미정, 예시, 추후 작성, or 나중에 작성 in required fields. "
        "invariant_refs MUST be a non-empty string array copied from the global map invariants. "
        f"For this zone, zone_contract.id_slots is exactly {id_slots_json}. Materialize every listed id_slot as an object "
        "with that exact id value inside outputs[output_field], not inside inputs, cross_refs, validation, or another "
        "output field. If output_field is outputs, the contracted object must be in outputs.outputs; if output_field "
        "is entities, it must be in outputs.entities. "
        "The declared id value is a literal, case-sensitive equality requirement: copy it character-for-character; "
        "never append or prepend a suffix/prefix such as _1, -1, _entity, or -item, and never substitute a related ID. "
        "For every required output array, a mapping/object item MUST use the exact key id with a non-empty unique "
        "value; never use id_slot, rule_id, entity_id, or another alias in place of id. String items are allowed "
        "where an identity is not needed. For example, the contracted world location must look like "
        "{\"id\":\"world_locations\",\"description\":\"...\"}, not "
        "{\"id_slot\":\"world_locations\",\"description\":\"...\"}. If an object is used in rules, "
        "history, resources, constraints, or any other array, give that object its own non-empty id as well. "
        "Include every required_links record owned by this source zone in outputs.cross_refs, preserving source/target "
        "direction, IDs, and relation exactly; the frame may name a target zone generated later. "
        "When zone_contract.required_links is non-empty, outputs MUST include a non-empty cross_refs array with one "
        "record for every listed link and no omissions. Each cross_refs record MUST contain exactly source_zone, "
        "source_id, target_zone, target_id, and relation, copied character-for-character from zone_contract.required_links. "
        f"For this zone, zone_contract.required_links is exactly {required_links_json}; cross_refs MUST equal this list "
        "and MUST NOT copy links from prior_zone_refs or invent links owned by another source zone. "
        "For example, a characters link must be shaped as {\"source_zone\":\"characters\",\"source_id\":\"character_id\","
        "\"target_zone\":\"plot\",\"target_id\":\"<the exact declared target id>\",\"relation\":\"defines\"}; "
        "never omit cross_refs or replace it with prose. "
        "Use each declared ID slot exactly in its named output field, and use those IDs for contracted link endpoints. "
        "Additional content IDs may be created where the zone needs them, but must not replace contracted IDs. "
        "Every output value may be a short array except exit_condition, which is a string. validation MUST contain "
        "exactly normal, boundary, and failure arrays, and every item in those three arrays MUST be a string, never "
        "an object. Use status PROPOSED. "
        "Before returning, self-check: schema_version is present and exact, every required output array is non-empty, "
        "every object item has an id key, invariant_refs exactly matches global_map.invariants, and validation items "
        "are strings; in default mode reactive_contract is absent, not null. "
        "For the world zone specifically, outputs.rules, outputs.history, outputs.resources, and outputs.constraints "
        "must each contain at least one substantive string or id-bearing object; an empty [] in any required field "
        "is invalid. A minimal valid pattern is locations:[{\"id\":\"world_locations\",\"description\":\"...\"}], "
        "rules:[\"...\"], history:[\"...\"], resources:[\"...\"], constraints:[\"...\"], plus three exact "
        "invariant_refs strings and one string in each validation array. "
        "For the relations zone specifically, outputs.conflicts MUST contain at least one substantive id-bearing object "
        "even when no conflict is known; use an object such as {\"id\":\"relations_conflicts\",\"description\":\"Assumption: "
        "no unresolved conflict is currently known.\"}, and never use a bare string for this non-conflict note. "
        "Record a scoped proposal or assumption instead of []. "
        "When details are inferred rather than stated in the supplied seed or context, label them as proposals or "
        "assumptions in the relevant prose; do not present an inference as an established fact."
    )
    if stage == "zone" and detailed_reactive:
        reactive_fields = ", ".join(DETAILED_REACTIVE_FIELDS)
        instruction = (
            f"{legacy_instruction} "
            f"This is the explicit opt-in detailed reactive contract {DETAILED_REACTIVE_CONTRACT_VERSION}. "
            f"Also include reactive_contract as an object with these seven required fields: {reactive_fields}, "
            "Set reactive_contract.contract_version to "
            f"{DETAILED_REACTIVE_CONTRACT_VERSION}. "
            "stable_id must be a deterministic non-empty string; input_refs and downstream_impact_refs must be "
            "string arrays; event_or_trigger, guard_or_precondition, and effect_or_state_delta must describe "
            "the observable trigger, precondition, and resulting state/effect. "
            "normal_boundary_failure_cases must be an array containing explicit cases with kinds normal, boundary, "
            "and failure, each with an expected result and failure or recovery action. "
            "Do not infer missing reactions from prose; leave the contract invalid rather than inventing values."
        )
    else:
        instruction = legacy_instruction
    context = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, default=str)
    return RuntimeRequest(
        request_id=f"creative:{stage}:{zone_type or 'root'}:{seed_hash}",
        model=model,
        messages=(
            {"role": "system", "content": instruction},
            {"role": "user", "content": f"seed_hash={seed_hash}\nframe_contract={json.dumps(frame_contract, ensure_ascii=False, sort_keys=True)}\ncontext={context}" if stage == "global_map" else f"seed_hash={seed_hash}\ncontext={context}"},
        ),
        parameters={"temperature": 0.2, "max_tokens": max_tokens},
        response_schema=None,
        timeout_seconds=timeout_seconds,
    )


__all__ = [
    "DETAILED_REACTIVE_CONTRACT_VERSION",
    "DETAILED_REACTIVE_FIELDS",
    "build_creative_stage_request",
]

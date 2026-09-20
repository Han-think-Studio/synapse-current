"""Read-only mapping from legacy general guided slots to novel-grade zones."""

from dataclasses import dataclass

from synapse.core.preset_catalog import load_preset_catalog


@dataclass(frozen=True, slots=True)
class GuidedZoneMapping:
    slot_id: str
    zone: str
    role: str
    rationale: str


GENERAL_GUIDED_MAPPING = (
    GuidedZoneMapping("general.scope", "context", "constraints", "범위와 경계를 고정합니다."),
    GuidedZoneMapping("general.premises", "context", "assumptions", "성립 전제를 기록합니다."),
    GuidedZoneMapping("general.entities", "actors", "entities", "핵심 대상과 주체를 식별합니다."),
    GuidedZoneMapping("general.entities", "relationships", "relations", "대상 사이의 관계를 확장합니다."),
    GuidedZoneMapping("general.goal", "causality", "outcome", "원하는 변화와 결과를 고정합니다."),
    GuidedZoneMapping("general.next_step", "milestones", "first_step", "첫 단계와 완료 조건을 정합니다."),
    GuidedZoneMapping("general.failure_criteria", "signals", "failure_signal", "실패 신호를 감지합니다."),
    GuidedZoneMapping("general.goal", "interactions", "expected_result", "입력과 결과의 기준을 연결합니다."),
    GuidedZoneMapping("general.rules", "state_reaction", "invariant", "상태 변화에도 지킬 규칙을 둡니다."),
    GuidedZoneMapping("general.failure_criteria", "integrity", "failure_gate", "중단·롤백 조건을 검증합니다."),
)

SOFTWARE_GUIDED_MAPPING = (
    GuidedZoneMapping("software.interfaces", "context", "boundary", "시스템 경계를 고정합니다."),
    GuidedZoneMapping("software.interfaces", "actors", "interfaces", "사용자와 서비스 주체를 연결합니다."),
    GuidedZoneMapping("software.interfaces", "relationships", "dependencies", "외부 연결과 의존성을 기록합니다."),
    GuidedZoneMapping("software.interfaces", "interactions", "io_flow", "입력·출력 흐름을 정의합니다."),
    GuidedZoneMapping("software.validation", "integrity", "tests", "정상·경계·실패 테스트를 고정합니다."),
    GuidedZoneMapping("software.validation", "state_reaction", "recovery", "실패와 복구 조건을 검증합니다."),
    GuidedZoneMapping("software.validation", "causality", "acceptance", "행동과 결과의 인과를 검증합니다."),
    GuidedZoneMapping("software.interfaces", "milestones", "delivery", "작은 릴리스 단계를 정합니다."),
    GuidedZoneMapping("software.validation", "signals", "observability", "오류와 위험 신호를 감지합니다."),
)

RESEARCH_GUIDED_MAPPING = (
    GuidedZoneMapping("research.sources", "context", "evidence_scope", "연구 범위와 자료 경계를 고정합니다."),
    GuidedZoneMapping("research.sources", "actors", "source_actors", "자료·연구자·도구를 주체로 기록합니다."),
    GuidedZoneMapping("research.sources", "relationships", "variable_links", "변수와 근거 사이의 연결을 기록합니다."),
    GuidedZoneMapping("research.method", "causality", "hypothesis", "가설과 인과 후보를 정의합니다."),
    GuidedZoneMapping("research.method", "milestones", "protocol", "실험 단계와 완료 조건을 정합니다."),
    GuidedZoneMapping("research.sources", "signals", "evidence_signal", "이상치와 반증 신호를 감지합니다."),
    GuidedZoneMapping("research.method", "interactions", "procedure", "반복 가능한 조사 절차를 정의합니다."),
    GuidedZoneMapping("research.method", "state_reaction", "reproducibility", "조건 변화와 결과 반응을 기록합니다."),
    GuidedZoneMapping("research.method", "integrity", "falsification", "재현·반증·경계 검증을 고정합니다."),
)

AUTOMATION_GUIDED_MAPPING = (
    GuidedZoneMapping("automation.inputs_outputs", "context", "execution_scope", "실행 환경과 데이터 경계를 고정합니다."),
    GuidedZoneMapping("automation.safety", "actors", "permissions", "실행 주체와 권한을 정의합니다."),
    GuidedZoneMapping("automation.inputs_outputs", "relationships", "connectors", "서비스와 단계 의존성을 연결합니다."),
    GuidedZoneMapping("automation.inputs_outputs", "causality", "trigger_chain", "트리거에서 결과까지의 인과를 정의합니다."),
    GuidedZoneMapping("automation.inputs_outputs", "milestones", "run_steps", "실행 단계와 완료 조건을 기록합니다."),
    GuidedZoneMapping("automation.safety", "signals", "alerts", "오류·지연·위험 신호를 감지합니다."),
    GuidedZoneMapping("automation.inputs_outputs", "interactions", "data_flow", "입력·변환·출력 흐름을 정의합니다."),
    GuidedZoneMapping("automation.safety", "state_reaction", "retry_recovery", "재시도·중단·복구 상태를 정의합니다."),
    GuidedZoneMapping("automation.safety", "integrity", "audit", "권한·멱등성·감사 조건을 검증합니다."),
)

CONTENT_GUIDED_MAPPING = (
    GuidedZoneMapping("content.subjects", "context", "audience_scope", "콘텐츠 범위와 대상 독자를 고정합니다."),
    GuidedZoneMapping("content.subjects", "actors", "audience_roles", "독자·학습자·진행 주체를 정의합니다."),
    GuidedZoneMapping("content.continuity", "relationships", "concept_links", "개념과 내용 사이의 연결을 기록합니다."),
    GuidedZoneMapping("content.outline", "causality", "learning_flow", "설명과 이해 결과의 인과를 구성합니다."),
    GuidedZoneMapping("content.outline", "milestones", "chapters", "챕터·수업·단계별 진행을 정합니다."),
    GuidedZoneMapping("content.continuity", "signals", "confusion_signals", "혼동·이탈·오해 신호를 감지합니다."),
    GuidedZoneMapping("content.outline", "interactions", "exercises", "예제·질문·연습 상호작용을 구성합니다."),
    GuidedZoneMapping("content.continuity", "state_reaction", "revision", "피드백에 따른 상태와 개정을 기록합니다."),
    GuidedZoneMapping("content.continuity", "integrity", "quality_gate", "사실성·연속성·접근성을 검증합니다."),
)

PRODUCT_BLUEPRINT_MAPPING = (
    GuidedZoneMapping("product.blueprint.context", "context", "market_scope", "시장·사용 환경·제약을 고정합니다."),
    GuidedZoneMapping("product.blueprint.actors", "actors", "personas", "고객·운영자·파트너를 정의합니다."),
    GuidedZoneMapping("product.blueprint.relationships", "relationships", "capability_dependencies", "기능·팀·채널 의존성을 연결합니다."),
    GuidedZoneMapping("product.blueprint.causality", "causality", "value_chain", "기능이 사용자 가치와 KPI로 이어지는 인과를 정의합니다."),
    GuidedZoneMapping("product.blueprint.milestones", "milestones", "rollout", "검증·출시·확장 단계를 정합니다."),
    GuidedZoneMapping("product.blueprint.signals", "signals", "kpi_risks", "성공 신호와 이탈·위험 신호를 감지합니다."),
    GuidedZoneMapping("product.blueprint.interactions", "interactions", "journeys", "사용자 여정과 서비스 접점을 정의합니다."),
    GuidedZoneMapping("product.blueprint.state_reaction", "state_reaction", "lifecycle", "계정·구독·지원 상태와 대응을 정의합니다."),
    GuidedZoneMapping("product.blueprint.integrity", "integrity", "support_gate", "보안·접근성·실패 대응을 검증합니다."),
)

GAME_BLUEPRINT_MAPPING = (
    GuidedZoneMapping("game.blueprint.context", "context", "world_rules", "세계·규칙·자원 제약을 고정합니다."),
    GuidedZoneMapping("game.blueprint.actors", "actors", "entities", "플레이어·NPC·아이템을 정의합니다."),
    GuidedZoneMapping("game.blueprint.relationships", "relationships", "factions_quests", "세력·관계·퀘스트 의존성을 연결합니다."),
    GuidedZoneMapping("game.blueprint.causality", "causality", "player_effects", "행동이 상태·보상·세계에 미치는 인과를 정의합니다."),
    GuidedZoneMapping("game.blueprint.milestones", "milestones", "progression", "챕터·레벨·진행 단계를 정합니다."),
    GuidedZoneMapping("game.blueprint.signals", "signals", "events_foreshadowing", "이벤트·위험·복선 신호를 감지합니다."),
    GuidedZoneMapping("game.blueprint.interactions", "interactions", "scenes_controls", "장면·조작·승패 흐름을 정의합니다."),
    GuidedZoneMapping("game.blueprint.state_reaction", "state_reaction", "ai_save_reaction", "AI·전투·인벤토리·세이브 반응을 정의합니다."),
    GuidedZoneMapping("game.blueprint.integrity", "integrity", "balance_continuity", "밸런스·세이브·연속성을 검증합니다."),
)


def list_general_guided_mapping() -> tuple[GuidedZoneMapping, ...]:
    """Return the stable mapping without loading or mutating the catalog."""

    return GENERAL_GUIDED_MAPPING


def list_software_guided_mapping() -> tuple[GuidedZoneMapping, ...]:
    """Return the stable software mapping without catalog mutation."""

    return SOFTWARE_GUIDED_MAPPING


def list_research_guided_mapping() -> tuple[GuidedZoneMapping, ...]:
    """Return the stable research mapping without catalog mutation."""

    return RESEARCH_GUIDED_MAPPING


def list_automation_guided_mapping() -> tuple[GuidedZoneMapping, ...]:
    """Return the stable automation mapping without catalog mutation."""

    return AUTOMATION_GUIDED_MAPPING


def list_content_guided_mapping() -> tuple[GuidedZoneMapping, ...]:
    """Return the stable content mapping without catalog mutation."""

    return CONTENT_GUIDED_MAPPING


def list_guided_mapping(profile_id: str) -> tuple[GuidedZoneMapping, ...]:
    """Return a profile mapping without loading or mutating catalog data."""

    normalized = profile_id.strip().lower()
    mapping_by_profile = {
        "general": GENERAL_GUIDED_MAPPING,
        "software": SOFTWARE_GUIDED_MAPPING,
        "research": RESEARCH_GUIDED_MAPPING,
        "automation": AUTOMATION_GUIDED_MAPPING,
        "content": CONTENT_GUIDED_MAPPING,
    }
    try:
        return mapping_by_profile[normalized]
    except KeyError as exc:
        raise KeyError(f"guided mapping is unavailable for profile: {profile_id}") from exc


def validate_guided_mapping(profile_id: str) -> dict[str, object]:
    """Verify that mapping slot IDs exist in the authoritative catalog."""

    mappings = list_guided_mapping(profile_id)
    category = profile_id.strip().lower()
    known = {slot.id for slot in load_preset_catalog().category(category).slots}
    missing = sorted({item.slot_id for item in mappings} - known)
    return {
        "profile_id": category,
        "valid": not missing,
        "missing_slot_ids": missing,
        "mapping_count": len(mappings),
    }


def guided_mapping_status() -> dict[str, object]:
    """Return a stable phase status for all declared idea profiles."""

    profiles = ("general", "software", "research", "automation", "content", "product", "game")
    ready = {profile: True for profile in profiles[:5]}
    return {
        "phase": "3-guided-mapping",
        "catalog_ready": sorted(ready),
        "blueprint_only": sorted(set(profiles) - set(ready)),
        "catalog_ready_count": len(ready),
        "blueprint_only_count": len(profiles) - len(ready),
    }


def list_blueprint_mapping(profile_id: str) -> tuple[GuidedZoneMapping, ...]:
    """Return synthetic blueprint mappings for profiles without a guided catalog."""

    normalized = profile_id.strip().lower()
    if normalized not in {"product", "game"}:
        raise KeyError(f"blueprint mapping is only for catalog-pending profiles: {profile_id}")
    return {"product": PRODUCT_BLUEPRINT_MAPPING, "game": GAME_BLUEPRINT_MAPPING}[normalized]


__all__ = [
    "AUTOMATION_GUIDED_MAPPING",
    "CONTENT_GUIDED_MAPPING",
    "GAME_BLUEPRINT_MAPPING",
    "GENERAL_GUIDED_MAPPING",
    "PRODUCT_BLUEPRINT_MAPPING",
    "RESEARCH_GUIDED_MAPPING",
    "SOFTWARE_GUIDED_MAPPING",
    "GuidedZoneMapping",
    "guided_mapping_status",
    "list_automation_guided_mapping",
    "list_blueprint_mapping",
    "list_content_guided_mapping",
    "list_general_guided_mapping",
    "list_guided_mapping",
    "list_research_guided_mapping",
    "list_software_guided_mapping",
    "validate_guided_mapping",
]

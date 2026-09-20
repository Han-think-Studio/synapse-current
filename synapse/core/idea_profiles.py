"""Read-only domain profiles for novel-grade idea expansion.

Profiles are declarative metadata. Routing, persistence, providers, and apply
authorization remain owned by their existing modules.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class IdeaProfile:
    id: str
    label: str
    priority_zones: tuple[str, ...]
    artifact_families: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.label.strip():
            raise ValueError("idea profile id and label are required")
        if len(set(self.priority_zones)) != len(self.priority_zones):
            raise ValueError("idea profile priority zones must be unique")
        if len(set(self.artifact_families)) != len(self.artifact_families):
            raise ValueError("idea profile artifact families must be unique")
        if len(self.artifact_families) != len(self.priority_zones):
            raise ValueError("artifact families must map one-to-one to priority zones")


@dataclass(frozen=True)
class ZoneSpec:
    zone: str
    purpose: str
    required_inputs: tuple[str, ...]
    required_outputs: tuple[str, ...]
    completion_gate: str


@dataclass(frozen=True)
class ZonePlan:
    zone: str
    priority: bool
    artifact_family: str | None


ZONE_ORDER = (
    "context",
    "actors",
    "relationships",
    "causality",
    "milestones",
    "signals",
    "interactions",
    "state_reaction",
    "integrity",
)

ZONE_SPECS = tuple(
    ZoneSpec(zone, purpose, inputs, outputs, gate)
    for zone, purpose, inputs, outputs, gate in (
        ("context", "작동 범위와 전제를 고정합니다.", ("seed",), ("scope", "constraints"), "범위와 제약이 명시됨"),
        ("actors", "핵심 주체와 권한을 정의합니다.", ("context",), ("actors", "roles"), "주체 ID와 역할이 고유함"),
        ("relationships", "주체와 대상 사이의 의존을 연결합니다.", ("actors",), ("relations", "dependencies"), "관계의 방향과 종류가 명시됨"),
        ("causality", "행동과 결과의 연결을 설명합니다.", ("relationships",), ("causal_chains",), "원인과 결과가 추적됨"),
        ("milestones", "시간과 단계별 변화를 정렬합니다.", ("causality",), ("milestones",), "순서와 완료 조건이 있음"),
        ("signals", "변화·위험·기회를 감지합니다.", ("context", "relationships"), ("signals", "triggers"), "감지 조건과 대응이 있음"),
        ("interactions", "입력과 출력의 실제 흐름을 정의합니다.", ("actors", "causality"), ("flows", "interfaces"), "입력·출력·실패 응답이 있음"),
        ("state_reaction", "상태 변화와 반응을 정의합니다.", ("interactions",), ("states", "transitions", "handlers", "recovery", "validation"), "트리거·전이·반응·복구·검증이 있음"),
        ("integrity", "모순·경계·실패를 검증합니다.", ("context", "state_reaction"), ("validation", "open_questions"), "정상·경계·실패 검증이 있음"),
    )
)


_PROFILES = (
    IdeaProfile("general", "General idea", ZONE_ORDER, ("context", "actors", "relations", "causality", "milestones", "signals", "interactions", "state", "integrity")),
    IdeaProfile("software", "Software / app", ("actors", "relationships", "interactions", "state_reaction", "integrity"), ("requirements", "interfaces", "state_machine", "tests", "recovery")),
    IdeaProfile("research", "Research", ("context", "causality", "milestones", "integrity"), ("hypothesis", "method", "evidence", "reproduction")),
    IdeaProfile("product", "Product / service", ("context", "actors", "interactions", "state_reaction", "integrity"), ("journeys", "capabilities", "kpis", "rollout", "support")),
    IdeaProfile("game", "Game", ZONE_ORDER, ("world", "entities", "relations", "causality", "progression", "signals", "events", "reactions", "balance")),
    IdeaProfile("automation", "Automation", ("relationships", "causality", "interactions", "state_reaction", "integrity"), ("triggers", "permissions", "steps", "retry", "audit")),
    IdeaProfile("content", "Content / education", ("context", "actors", "causality", "milestones", "interactions", "integrity"), ("audience", "outline", "examples", "exercises", "feedback", "revision")),
)


# Priority zones still guide scheduling, but every profile now has a complete
# artifact vocabulary so quality cannot pass with partial domain coverage.
_ZONE_ARTIFACTS = {
    "general": {"context": "context", "actors": "actors", "relationships": "relations", "causality": "causality", "milestones": "milestones", "signals": "signals", "interactions": "interactions", "state_reaction": "state", "integrity": "integrity"},
    "software": {"context": "scope", "actors": "roles", "relationships": "dependencies", "causality": "acceptance", "milestones": "delivery", "signals": "observability", "interactions": "interfaces", "state_reaction": "state_machine", "integrity": "tests"},
    "research": {"context": "source_scope", "actors": "research_roles", "relationships": "variable_links", "causality": "hypothesis", "milestones": "method", "signals": "evidence", "interactions": "procedure", "state_reaction": "reproducibility", "integrity": "reproduction"},
    "product": {"context": "market_scope", "actors": "personas", "relationships": "capability_dependencies", "causality": "value_chain", "milestones": "rollout", "signals": "kpi_risks", "interactions": "journeys", "state_reaction": "lifecycle", "integrity": "support_gate"},
    "game": {"context": "world_rules", "actors": "entities", "relationships": "factions_quests", "causality": "player_effects", "milestones": "progression", "signals": "events_foreshadowing", "interactions": "scenes_controls", "state_reaction": "ai_save_reaction", "integrity": "balance_continuity"},
    "automation": {"context": "execution_scope", "actors": "permissions", "relationships": "connectors", "causality": "trigger_chain", "milestones": "run_steps", "signals": "alerts", "interactions": "data_flow", "state_reaction": "retry_recovery", "integrity": "audit"},
    "content": {"context": "audience", "actors": "roles", "relationships": "concept_links", "causality": "learning_flow", "milestones": "chapters", "signals": "confusion_signals", "interactions": "exercises", "state_reaction": "revision", "integrity": "quality_gate"},
}


def artifact_family_for_zone(profile_id: str, zone: str) -> str:
    """Return the stable artifact family for every profile/zone pair."""

    normalized = profile_id.strip().lower()
    try:
        return _ZONE_ARTIFACTS[normalized][zone]
    except KeyError as exc:
        raise KeyError(f"unknown profile or zone: {profile_id}/{zone}") from exc


def list_idea_profiles() -> tuple[IdeaProfile, ...]:
    """Return the immutable built-in profile registry in stable order."""

    return _PROFILES


def get_idea_profile(profile_id: str) -> IdeaProfile:
    normalized = profile_id.strip().lower()
    for profile in _PROFILES:
        if profile.id == normalized:
            return profile
    raise KeyError(f"unknown idea profile: {profile_id}")


def build_zone_plan(profile_id: str) -> tuple[ZonePlan, ...]:
    """Build a stable, read-only nine-zone plan for a domain profile."""

    profile = get_idea_profile(profile_id)
    spec_zones = {spec.zone for spec in ZONE_SPECS}
    families = iter(profile.artifact_families)
    plans: list[ZonePlan] = []
    for zone in ZONE_ORDER:
        if zone not in spec_zones:
            raise ValueError(f"missing zone specification: {zone}")
        family = next(families, None) if zone in profile.priority_zones else None
        plans.append(ZonePlan(zone, zone in profile.priority_zones, family))
    return tuple(plans)

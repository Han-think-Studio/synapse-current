"""Deterministic failure triage for Cognitive Frames."""

from __future__ import annotations

from dataclasses import dataclass

from synapse.core.cognitive import CognitiveFrame

_SEVERITY = {"BLOCKER": 0, "REVIEW": 1}
_CLUSTER_ORDER = {
    "premise.blocked": 0,
    "premise.unresolved": 1,
    "evidence.missing": 2,
}


@dataclass(frozen=True, slots=True)
class FailureCluster:
    key: str
    severity: str
    subject_ids: tuple[str, ...]
    recommendation: str

    def __post_init__(self) -> None:
        if not self.key.strip() or self.severity not in _SEVERITY:
            raise ValueError("FailureCluster key와 유효한 severity가 필요합니다.")
        if not self.subject_ids:
            raise ValueError("FailureCluster에는 하나 이상의 subject가 필요합니다.")
        if not self.recommendation.strip():
            raise ValueError("FailureCluster recommendation이 비어 있습니다.")
        object.__setattr__(self, "subject_ids", tuple(sorted(set(self.subject_ids))))


@dataclass(frozen=True, slots=True)
class FailureReport:
    clusters: tuple[FailureCluster, ...]

    def __post_init__(self) -> None:
        ordered = sorted(
            self.clusters,
            key=lambda cluster: (
                _SEVERITY[cluster.severity],
                _CLUSTER_ORDER.get(cluster.key, 99),
                cluster.key,
            ),
        )
        if len({cluster.key for cluster in ordered}) != len(ordered):
            raise ValueError("FailureReport cluster key가 중복됩니다.")
        object.__setattr__(self, "clusters", tuple(ordered))

    @property
    def has_blockers(self) -> bool:
        return any(cluster.severity == "BLOCKER" for cluster in self.clusters)

    def to_record(self) -> dict[str, object]:
        return {
            "has_blockers": self.has_blockers,
            "clusters": [
                {
                    "key": cluster.key,
                    "severity": cluster.severity,
                    "subject_ids": list(cluster.subject_ids),
                    "recommendation": cluster.recommendation,
                }
                for cluster in self.clusters
            ],
        }


def triage_frame(frame: CognitiveFrame) -> FailureReport:
    """Group only explicit unresolved/blocked/missing-evidence conditions."""
    clusters: list[FailureCluster] = []
    if frame.premise.unresolved_ids:
        clusters.append(
            FailureCluster(
                key="premise.unresolved",
                severity="REVIEW",
                subject_ids=tuple(frame.premise.unresolved_ids),
                recommendation="근거를 추가하거나 명시적으로 unresolved 상태를 유지합니다.",
            )
        )
    if frame.premise.blocked_ids:
        clusters.append(
            FailureCluster(
                key="premise.blocked",
                severity="BLOCKER",
                subject_ids=tuple(frame.premise.blocked_ids),
                recommendation="blocked 원인을 해소하기 전에는 injection/commit하지 않습니다.",
            )
        )
    evidenced = {ref.node_id for ref in frame.evidence}
    missing = tuple(node_id for node_id in frame.subject_ids if node_id not in evidenced)
    if missing:
        clusters.append(
            FailureCluster(
                key="evidence.missing",
                severity="REVIEW",
                subject_ids=missing,
                recommendation="source/provenance를 추가한 뒤 다음 단계로 보냅니다.",
            )
        )
    return FailureReport(clusters=tuple(clusters))


__all__ = ["FailureCluster", "FailureReport", "triage_frame"]

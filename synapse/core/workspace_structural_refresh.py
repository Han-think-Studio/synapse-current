"""Workspace-bound explicit structural refresh for Phase 44.

This module composes the existing workspace observation/invalidation boundary
with the Phase 43 review pipeline.  It never rescans the workspace and never
emits events automatically; the caller supplies both the observation and the
explicit process request.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from synapse.core.idea_session import canonical_hash
from synapse.core.infigraph_process import (
    ExternalSensorProcessRequest,
    InfigraphProcessAdapter,
)
from synapse.core.structural_policy import (
    BUILT_IN_STRUCTURAL_POLICY_RULES,
    StructuralPolicyRule,
)
from synapse.core.structural_reactive import (
    StructuralInvalidationBatch,
    build_structural_invalidation_batch,
)
from synapse.core.structural_review import (
    StructuralReviewError,
    StructuralReviewResult,
    run_explicit_structural_review,
)
from synapse.core.workspace import WorkspaceObservation

WORKSPACE_STRUCTURAL_REFRESH_SCHEMA = "workspace.structural.refresh.v1"
WORKSPACE_STRUCTURAL_REFRESH_STAGES = (
    "WORKSPACE_OBSERVED",
    "INVALIDATION_BOUND",
    "PROCESS_ACQUIRED",
    "OBSERVATION_IMPORTED",
    "POLICY_EVALUATED",
    "POLICY_GATED",
)


class WorkspaceStructuralRefreshError(ValueError):
    """Raised when the explicit workspace-to-structural binding is unsafe."""


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkspaceStructuralRefreshResult:
    """Immutable binding of one supplied workspace observation to one review."""

    workspace_observation: WorkspaceObservation
    invalidation_batch: StructuralInvalidationBatch
    review: StructuralReviewResult
    stages: tuple[str, ...] = WORKSPACE_STRUCTURAL_REFRESH_STAGES
    schema_version: str = WORKSPACE_STRUCTURAL_REFRESH_SCHEMA
    id: str = ""
    automatic: bool = False
    canonical_mutation: bool = False
    filesystem_mutation: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != WORKSPACE_STRUCTURAL_REFRESH_SCHEMA:
            raise WorkspaceStructuralRefreshError(
                "지원하지 않는 workspace structural refresh schema입니다."
            )
        if not isinstance(self.workspace_observation, WorkspaceObservation):
            raise WorkspaceStructuralRefreshError("workspace_observation 타입이 잘못되었습니다.")
        if not isinstance(self.invalidation_batch, StructuralInvalidationBatch):
            raise WorkspaceStructuralRefreshError("invalidation_batch 타입이 잘못되었습니다.")
        if not isinstance(self.review, StructuralReviewResult):
            raise WorkspaceStructuralRefreshError("review 타입이 잘못되었습니다.")
        stages = tuple(self.stages)
        if stages != WORKSPACE_STRUCTURAL_REFRESH_STAGES:
            raise WorkspaceStructuralRefreshError("workspace refresh 단계 순서가 계약과 다릅니다.")
        object.__setattr__(self, "stages", stages)

        expected_batch = build_structural_invalidation_batch(self.workspace_observation)
        if self.invalidation_batch != expected_batch:
            raise WorkspaceStructuralRefreshError(
                "invalidation_batch가 supplied workspace observation에서 파생되지 않았습니다."
            )
        if self.review.expected_workspace_hash != self.invalidation_batch.workspace_snapshot_hash:
            raise WorkspaceStructuralRefreshError(
                "review expected workspace hash가 invalidation batch와 다릅니다."
            )
        if self.review.observation.workspace_hash != self.invalidation_batch.workspace_snapshot_hash:
            raise WorkspaceStructuralRefreshError(
                "imported observation workspace hash가 invalidation batch와 다릅니다."
            )
        for value, label in (
            (self.automatic, "refresh.automatic"),
            (self.canonical_mutation, "refresh.canonical_mutation"),
            (self.filesystem_mutation, "refresh.filesystem_mutation"),
        ):
            if not isinstance(value, bool) or value:
                raise WorkspaceStructuralRefreshError(f"{label}은(는) false여야 합니다.")

        refresh_hash = canonical_hash(self._hash_payload())
        expected_id = f"workspace-structural-refresh:{refresh_hash.removeprefix('sha256:')}"
        if self.id and self.id != expected_id:
            raise WorkspaceStructuralRefreshError("workspace refresh id가 payload와 일치하지 않습니다.")
        object.__setattr__(self, "id", expected_id)

    def _hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "workspace_snapshot_hash": self.invalidation_batch.workspace_snapshot_hash,
            "invalidation_batch_id": self.invalidation_batch.id,
            "review_id": self.review.id,
            "review_request_id": self.review.request_id,
            "observation_id": self.review.observation.id,
            "observation_hash": self.review.observation.observation_hash,
            "changed_paths": [item.path for item in self.invalidation_batch.invalidations],
            "stages": list(self.stages),
        }

    @property
    def passed(self) -> bool:
        return self.review.passed

    @property
    def changed_paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.invalidation_batch.invalidations)

    def to_record(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "id": self.id,
            "stages": list(self.stages),
            "workspace_observation": self.workspace_observation.to_record(),
            "invalidation_batch": self.invalidation_batch.to_record(),
            "review": self.review.to_record(),
            "passed": self.passed,
            "automatic": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }


def run_explicit_workspace_structural_refresh(
    observation: WorkspaceObservation,
    request: ExternalSensorProcessRequest,
    *,
    adapter: InfigraphProcessAdapter | None = None,
    rules: Sequence[StructuralPolicyRule] = BUILT_IN_STRUCTURAL_POLICY_RULES,
) -> WorkspaceStructuralRefreshResult:
    """Bind one supplied workspace observation to one explicit structural review."""

    if not isinstance(observation, WorkspaceObservation):
        raise WorkspaceStructuralRefreshError("observation은 WorkspaceObservation이어야 합니다.")
    if not isinstance(request, ExternalSensorProcessRequest):
        raise WorkspaceStructuralRefreshError(
            "request는 ExternalSensorProcessRequest이어야 합니다."
        )
    if adapter is not None and not isinstance(adapter, InfigraphProcessAdapter):
        raise WorkspaceStructuralRefreshError("adapter는 InfigraphProcessAdapter이어야 합니다.")

    invalidation_batch = build_structural_invalidation_batch(observation)
    try:
        review = run_explicit_structural_review(
            request,
            adapter=adapter,
            rules=rules,
            expected_workspace_hash=invalidation_batch.workspace_snapshot_hash,
        )
    except StructuralReviewError as exc:
        raise WorkspaceStructuralRefreshError(str(exc)) from exc
    return WorkspaceStructuralRefreshResult(
        workspace_observation=observation,
        invalidation_batch=invalidation_batch,
        review=review,
    )


__all__ = [
    "WORKSPACE_STRUCTURAL_REFRESH_SCHEMA",
    "WORKSPACE_STRUCTURAL_REFRESH_STAGES",
    "WorkspaceStructuralRefreshError",
    "WorkspaceStructuralRefreshResult",
    "run_explicit_workspace_structural_refresh",
]

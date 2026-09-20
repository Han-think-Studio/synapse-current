"""Existing-project reconstruction over the existing Synapse boundaries.

This module is deliberately an adapter, not a second registry or a new
orchestrator.  A reviewed :class:`BundleReport` is converted through the
existing bundle-to-IR intake path, deterministic observations are labelled,
and only unresolved or conflicting roles become ``OpenQuestion`` records.
Answers produce a new immutable proposal.  Nothing in this module mutates
``CanonicalRegistry`` unless a caller explicitly creates a normal
``Proposal`` and commits it through the existing validator/registry boundary.

The observation status vocabulary is intentionally separate from
``LifecycleStatus``.  ``CONFIRMED`` below means confirmed inside this
proposal; it is not Canonical State until the normal ProposalValidation and
CanonicalRegistry.commit steps succeed.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

from synapse.core.action import ActionRequest
from synapse.core.bundle import BundleError, BundleReport, inspect_bundle, load_bundle_report
from synapse.core.contracts import Entity, LifecycleStatus, Provenance, to_record
from synapse.core.idea_session import OpenQuestion, UserAnswer, canonical_hash
from synapse.core.intake import bundle_report_to_ir
from synapse.core.ir import SynapseIR
from synapse.core.registry import CanonicalRegistry, Proposal, ProposalOperation

PROJECT_RECONSTRUCTION_SCHEMA = "project-reconstruction.v1"


class ProjectReconstructionError(ValueError):
    """Raised when an existing-project proposal cannot cross its boundary."""


class ProjectReconstructionStaleError(ProjectReconstructionError):
    """Raised when the inspected source ZIP no longer matches the proposal."""


class ObservationStatus(str, Enum):
    """Proposal-local evidence labels, kept distinct from Canonical lifecycle."""

    OBSERVED = "OBSERVED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"
    CONFLICT = "CONFLICT"
    CONFIRMED = "CONFIRMED"


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectFinding:
    """One bounded observation or interpretation in a reconstruction proposal."""

    id: str
    subject: str
    status: ObservationStatus
    value: Any = None
    evidence_paths: tuple[str, ...] = ()
    question_id: str | None = None
    detail: str = ""
    provenance: tuple[Provenance, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.subject.strip():
            raise ProjectReconstructionError("finding에는 id와 subject가 필요합니다.")
        if not isinstance(self.status, ObservationStatus):
            raise ProjectReconstructionError("finding status가 잘못되었습니다.")
        if self.question_id is not None and not self.question_id.strip():
            raise ProjectReconstructionError("빈 question_id는 허용되지 않습니다.")
        object.__setattr__(self, "evidence_paths", tuple(self.evidence_paths))
        object.__setattr__(self, "provenance", tuple(self.provenance))

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "subject": self.subject,
            "status": self.status.value,
            "value": to_record(self.value),
            "evidence_paths": list(self.evidence_paths),
            "question_id": self.question_id,
            "detail": self.detail,
            "provenance": [to_record(item) for item in self.provenance],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ProjectReconstructionProposal:
    """Immutable, reviewable reconstruction state for one existing bundle."""

    project_id: str
    owner: str
    source_id: str
    archive_sha256: str
    archive_name: str
    ir: SynapseIR
    findings: tuple[ProjectFinding, ...]
    questions: tuple[OpenQuestion, ...]
    answers: tuple[UserAnswer, ...] = ()
    revision: int = 0
    schema_version: str = PROJECT_RECONSTRUCTION_SCHEMA
    canonical_mutation: bool = False
    source_report: BundleReport | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for value, label in (
            (self.project_id, "project_id"),
            (self.owner, "owner"),
            (self.source_id, "source_id"),
            (self.archive_sha256, "archive_sha256"),
            (self.archive_name, "archive_name"),
        ):
            if not value.strip():
                raise ProjectReconstructionError(f"{label}이(가) 필요합니다.")
        if self.schema_version != PROJECT_RECONSTRUCTION_SCHEMA:
            raise ProjectReconstructionError("지원하지 않는 reconstruction schema입니다.")
        if not isinstance(self.ir, SynapseIR):
            raise ProjectReconstructionError("reconstruction ir 타입이 잘못되었습니다.")
        if self.revision < 0:
            raise ProjectReconstructionError("reconstruction revision은 0 이상이어야 합니다.")
        if self.canonical_mutation:
            raise ProjectReconstructionError(
                "reconstruction proposal은 Canonical State를 직접 변경할 수 없습니다."
            )
        if self.source_report is not None:
            if not isinstance(self.source_report, BundleReport):
                raise ProjectReconstructionError("reconstruction source report 타입이 잘못되었습니다.")
            if not self.source_report.safe:
                raise ProjectReconstructionError("안전하지 않은 source report는 reconstruction에 묶을 수 없습니다.")
            if (
                self.source_report.archive_sha256 != self.archive_sha256
                or self.source_report.source_id != self.source_id
            ):
                raise ProjectReconstructionError(
                    "reconstruction proposal과 source report의 identity가 다릅니다."
                )
        findings = tuple(self.findings)
        questions = tuple(self.questions)
        answers = tuple(self.answers)
        if len({finding.id for finding in findings}) != len(findings):
            raise ProjectReconstructionError("finding id가 중복됩니다.")
        if len({question.id for question in questions}) != len(questions):
            raise ProjectReconstructionError("question id가 중복됩니다.")
        if len({answer.question_id for answer in answers}) != len(answers):
            raise ProjectReconstructionError("같은 reconstruction question에 답이 중복됩니다.")
        question_ids = {question.id for question in questions}
        if any(
            finding.question_id is not None and finding.question_id not in question_ids
            for finding in findings
        ):
            raise ProjectReconstructionError("finding이 존재하지 않는 question을 가리킵니다.")
        if any(answer.question_id not in question_ids for answer in answers):
            raise ProjectReconstructionError("answer가 존재하지 않는 question을 가리킵니다.")
        object.__setattr__(self, "findings", findings)
        object.__setattr__(self, "questions", questions)
        object.__setattr__(self, "answers", answers)

    @property
    def blocking_question_ids(self) -> tuple[str, ...]:
        return tuple(
            question.id
            for question in self.questions
            if question.blocking and question.status != "ANSWERED"
        )

    @property
    def ready_for_review(self) -> bool:
        """Whether required ambiguity has been answered, without implying approval."""

        return not self.blocking_question_ids

    def finding(self, subject: str) -> ProjectFinding:
        for finding in self.findings:
            if finding.subject == subject:
                return finding
        raise ProjectReconstructionError(f"finding을 찾을 수 없습니다: {subject}")

    def answer_questions(self, answers: Mapping[str, str]) -> ProjectReconstructionProposal:
        """Apply explicit user answers and return a new proposal revision.

        The answer is constrained by observed archive paths for path-like
        roles.  A caller cannot invent an entrypoint that was not observed in
        the bounded bundle.  This method never promotes anything to Canonical.
        """

        if not isinstance(answers, Mapping):
            raise ProjectReconstructionError("answers는 mapping이어야 합니다.")
        question_map = {question.id: question for question in self.questions}
        finding_list = list(self.findings)
        finding_by_question = {
            finding.question_id: index
            for index, finding in enumerate(finding_list)
            if finding.question_id is not None
        }
        answer_list = list(self.answers)
        answer_ids = {answer.question_id for answer in answer_list}
        available_paths = {
            str(node.attributes["path"]) for node in self.ir.nodes if "path" in node.attributes
        }

        for question_id, raw_answer in answers.items():
            if not isinstance(question_id, str) or question_id not in question_map:
                raise ProjectReconstructionError(
                    f"알 수 없는 reconstruction question입니다: {question_id}"
                )
            if not isinstance(raw_answer, str) or not raw_answer.strip():
                raise ProjectReconstructionError(f"질문 답이 비어 있습니다: {question_id}")
            question = question_map[question_id]
            if question.status == "ANSWERED" or question_id in answer_ids:
                raise ProjectReconstructionError(
                    f"이미 답변된 reconstruction question입니다: {question_id}"
                )
            finding_index = finding_by_question.get(question_id)
            if finding_index is None:
                raise ProjectReconstructionError(f"finding이 없는 question입니다: {question_id}")
            finding = finding_list[finding_index]
            answer = raw_answer.strip()
            value: Any = answer
            evidence_paths = finding.evidence_paths
            if finding.subject == "entrypoint":
                value = _normalise_answer_path(answer)
                if value not in available_paths:
                    raise ProjectReconstructionError(
                        f"entrypoint 답은 관찰된 bundle path여야 합니다: {answer}"
                    )
                evidence_paths = (value,)
            provenance = _user_answer_provenance(self.project_id, question_id, self.revision + 1)
            finding_list[finding_index] = replace(
                finding,
                status=ObservationStatus.CONFIRMED,
                value=value,
                evidence_paths=evidence_paths,
                provenance=finding.provenance + (provenance,),
            )
            question_map[question_id] = replace(question, status="ANSWERED", answer=answer)
            answer_list.append(
                UserAnswer(
                    question_id=question_id,
                    answer=answer,
                    source="user",
                    created_at=f"reconstruction:{self.project_id}:revision:{self.revision + 1}",
                )
            )
            answer_ids.add(question_id)

        updated_questions = tuple(question_map[question.id] for question in self.questions)
        return replace(
            self,
            findings=tuple(finding_list),
            questions=updated_questions,
            answers=tuple(answer_list),
            revision=self.revision + (1 if answers else 0),
        )

    def to_registry_proposal(
        self,
        *,
        approved: bool,
        actor: str,
        archive: str | Path,
    ) -> Proposal:
        """Create a Registry Proposal only after rechecking the bound source."""

        if not isinstance(approved, bool) or not approved:
            raise ProjectReconstructionError(
                "reconstruction은 approved=true인 명시적 검토 뒤에만 Proposal이 됩니다."
            )
        if not actor.strip():
            raise ProjectReconstructionError("canonical proposal actor가 필요합니다.")
        if not self.ready_for_review:
            raise ProjectReconstructionError(
                "blocking reconstruction question이 남아 있어 Proposal을 만들 수 없습니다."
            )
        if self.source_report is None:
            raise ProjectReconstructionError(
                "검증된 source report가 없는 reconstruction proposal은 Registry Proposal이 될 수 없습니다."
            )
        self.assert_source_current(archive)
        name = _finding_scalar(self.finding("project_name"), "project_name")
        entrypoint = _finding_scalar(self.finding("entrypoint"), "entrypoint")
        project_entity_id = self.project_id
        entity = Entity(
            id=project_entity_id,
            owner=self.owner,
            canonical_key=f"project:{self.archive_sha256}",
            status=LifecycleStatus.CONFIRMED,
            name=name,
            entity_type="project",
            provenance=(
                Provenance(
                    source_id=self.source_id,
                    method="project_reconstruction",
                    locator=f"archive:{self.archive_sha256}",
                    captured_at=f"archive:{self.archive_sha256}",
                ),
                Provenance(
                    source_id=f"proposal:{self.project_id}",
                    method="explicit_review",
                    locator="canonical_project_model",
                    captured_at=f"reconstruction:revision:{self.revision}",
                ),
            ),
            attributes={
                "schema_version": self.schema_version,
                "source_id": self.source_id,
                "archive_sha256": self.archive_sha256,
                "archive_name": self.archive_name,
                "entrypoint": entrypoint,
                "observed_paths": list(_observed_paths(self.ir)),
                "authority_candidates": list(_finding_paths(self.finding("authority_candidates"))),
                "legacy_artifacts": list(_finding_paths(self.finding("legacy_artifacts"))),
                "readme_paths": list(_finding_paths(self.finding("readme"))),
                "manifest_present": self.finding("manifest").status
                is not ObservationStatus.UNKNOWN,
                "finding_statuses": {
                    finding.subject: finding.status.value for finding in self.findings
                },
                "confirmed_question_ids": [
                    question.id for question in self.questions if question.status == "ANSWERED"
                ],
            },
            created_at=f"reconstruction:{self.project_id}:revision:{self.revision}",
            updated_at=f"reconstruction:{self.project_id}:revision:{self.revision}",
        )
        return Proposal(
            candidate=entity,
            operation=ProposalOperation.CREATE,
            actor=actor,
            id=f"proposal:{self.project_id}:create",
            created_at=f"reconstruction:{self.project_id}:revision:{self.revision}",
            rationale="Explicitly reviewed existing-project reconstruction from bounded observations.",
        )

    def assert_source_current(self, archive: str | Path) -> None:
        """Re-read the reviewed source before any later boundary can commit it."""

        try:
            current = inspect_bundle(archive)
        except (BundleError, OSError) as exc:
            raise ProjectReconstructionStaleError(
                "검토한 원본 ZIP을 다시 읽을 수 없습니다. 다시 inspect해 주세요."
            ) from exc
        # The archive path is a locator, not observed project content.  Keep
        # the content/hash comparison strict while allowing the reviewed ZIP
        # to be moved before the explicit Proposal boundary is crossed.
        expected = replace(self.source_report, archive=current.archive)
        if current != expected:
            raise ProjectReconstructionStaleError(
                "검토 후 Registry Proposal 경계에서 원본 ZIP 관찰 결과가 변경되었습니다."
            )

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "owner": self.owner,
            "source_id": self.source_id,
            "archive_sha256": self.archive_sha256,
            "archive_name": self.archive_name,
            "revision": self.revision,
            "ir": self.ir.to_record(),
            "findings": [finding.to_record() for finding in self.findings],
            "questions": [question.to_record() for question in self.questions],
            "answers": [answer.to_record() for answer in self.answers],
            "ready_for_review": self.ready_for_review,
            "blocking_question_ids": list(self.blocking_question_ids),
            "canonical_mutation": False,
        }


def reconstruct_project(
    report: BundleReport,
    *,
    owner: str = "human",
    project_name: str | None = None,
) -> ProjectReconstructionProposal:
    """Reconstruct a bounded project proposal from an inspected ZIP report."""

    if not isinstance(report, BundleReport):
        raise ProjectReconstructionError("reconstruction 입력은 BundleReport이어야 합니다.")
    if not report.safe:
        raise ProjectReconstructionError(
            "안전 검사를 통과하지 못한 bundle은 reconstruct할 수 없습니다."
        )
    if not owner.strip():
        raise ProjectReconstructionError("reconstruction owner가 필요합니다.")

    ir = bundle_report_to_ir(report, owner=owner)
    paths = tuple(sorted(entry.path for entry in report.entries))
    path_set = set(paths)
    entrypoint_candidates = _entrypoint_candidates(report)
    manifest_name_candidates = _manifest_name_candidates(report)
    explicit_name = project_name.strip() if isinstance(project_name, str) else ""
    if explicit_name:
        name_status = ObservationStatus.CONFIRMED
        name_value: Any = explicit_name
        name_detail = "caller supplied an explicit project name"
        name_paths: tuple[str, ...] = ()
        name_provenance = (_caller_input_provenance(report, "project_name"),)
    elif len(manifest_name_candidates) == 1:
        name_status = ObservationStatus.INFERRED
        name_value = manifest_name_candidates[0]
        name_detail = "name was read from an observed bundle manifest and needs confirmation"
        name_paths = ("synapse.bundle.json",)
        name_provenance = None
    elif len(manifest_name_candidates) > 1:
        name_status = ObservationStatus.CONFLICT
        name_value = list(manifest_name_candidates)
        name_detail = "multiple manifest name fields disagree"
        name_paths = ("synapse.bundle.json",)
        name_provenance = None
    else:
        name_status = ObservationStatus.INFERRED
        name_value = Path(report.archive).stem or "existing-project"
        name_detail = "archive filename is only a provisional project name"
        name_paths = ()
        name_provenance = None

    entrypoint_claim, normalised_entrypoint_claim = _manifest_entrypoint_claim(report)
    entrypoint_claim_conflict = (
        entrypoint_claim is not None
        and normalised_entrypoint_claim not in path_set
    )
    entrypoint_status = (
        ObservationStatus.CONFLICT
        if entrypoint_claim_conflict
        else _candidate_status(entrypoint_candidates)
    )
    entrypoint_value: Any = list(entrypoint_candidates)
    entrypoint_evidence = entrypoint_candidates
    entrypoint_detail = _entrypoint_detail(entrypoint_candidates)
    if entrypoint_claim_conflict:
        entrypoint_value = {
            "observed_candidates": list(entrypoint_candidates),
            "manifest_claim": entrypoint_claim,
        }
        entrypoint_evidence = tuple(
            dict.fromkeys((*entrypoint_candidates, "synapse.bundle.json"))
        )
        entrypoint_detail = (
            "manifest claimed an entrypoint that was not observed in the bounded "
            "bundle; the claim is preserved as a conflict"
        )

    findings: list[ProjectFinding] = [
        _finding(
            report,
            subject="artifact_inventory",
            status=ObservationStatus.OBSERVED,
            value={
                "entry_count": report.entry_count,
                "kinds": _kind_counts(report),
                "paths": list(paths),
            },
            evidence_paths=paths,
            detail="paths, sizes, hashes, headings, and marker signals came from BundleReport",
        ),
        _finding(
            report,
            subject="manifest",
            status=(
                ObservationStatus.OBSERVED
                if report.manifest is not None
                else ObservationStatus.UNKNOWN
            ),
            value=(
                {"present": True, "keys": sorted(str(key) for key in report.manifest)}
                if isinstance(report.manifest, Mapping)
                else None
            ),
            evidence_paths=("synapse.bundle.json",) if "synapse.bundle.json" in path_set else (),
            detail=(
                "manifest was observed but its claims are not Canonical"
                if report.manifest is not None
                else "no bundle manifest was observed"
            ),
        ),
        _finding(
            report,
            subject="project_name",
            status=name_status,
            value=name_value,
            evidence_paths=name_paths,
            detail=name_detail,
            question_id=(
                None
                if name_status is ObservationStatus.CONFIRMED
                else _question_id(report, "project_name")
            ),
            provenance=name_provenance,
        ),
        _finding(
            report,
            subject="entrypoint",
            status=entrypoint_status,
            value=entrypoint_value,
            evidence_paths=entrypoint_evidence,
            detail=entrypoint_detail,
            question_id=_question_id(report, "entrypoint"),
        ),
        _finding(
            report,
            subject="readme",
            status=(
                ObservationStatus.OBSERVED if _readme_paths(paths) else ObservationStatus.UNKNOWN
            ),
            value=list(_readme_paths(paths)),
            evidence_paths=_readme_paths(paths),
            detail="README presence is observed only; it is not treated as project authority",
        ),
        _finding(
            report,
            subject="authority_candidates",
            status=(
                ObservationStatus.OBSERVED
                if _authority_paths(report)
                else ObservationStatus.UNKNOWN
            ),
            value=list(_authority_paths(report)),
            evidence_paths=_authority_paths(report),
            detail="authority-looking paths are candidates and are not promoted automatically",
        ),
        _finding(
            report,
            subject="legacy_artifacts",
            status=(
                ObservationStatus.OBSERVED if _legacy_paths(paths) else ObservationStatus.UNKNOWN
            ),
            value=list(_legacy_paths(paths)),
            evidence_paths=_legacy_paths(paths),
            detail="legacy-looking artifacts remain evidence, not deletion instructions",
        ),
    ]
    questions = _questions_for_findings(report, findings, ir)
    return ProjectReconstructionProposal(
        project_id=f"project:{report.archive_sha256}",
        owner=owner,
        source_id=report.source_id,
        archive_sha256=report.archive_sha256,
        archive_name=Path(report.archive).name,
        ir=ir,
        findings=tuple(findings),
        questions=questions,
        source_report=report,
    )


def action_request_for_project(
    entity: Entity,
    *,
    registry: CanonicalRegistry,
    task_kind: str = "project_validation",
    required_capabilities: Sequence[str] = ("verify",),
    approval_required: bool = True,
    approved: bool = False,
) -> ActionRequest:
    """Adapt a committed project Entity to the existing routing contract."""

    if not isinstance(entity, Entity) or entity.entity_type != "project":
        raise ProjectReconstructionError("project Entity가 필요합니다.")
    if not isinstance(registry, CanonicalRegistry) or registry.get(entity.id) is not entity:
        raise ProjectReconstructionError(
            "project Entity는 기존 CanonicalRegistry에 commit된 항목이어야 합니다."
        )
    if entity.status is not LifecycleStatus.CONFIRMED:
        raise ProjectReconstructionError("CONFIRMED project Entity가 필요합니다.")
    entrypoint = str(entity.attributes.get("entrypoint", "")).strip()
    if not entrypoint:
        raise ProjectReconstructionError("project Entity에 entrypoint가 없습니다.")
    return ActionRequest(
        id=f"action:{entity.id}:validation",
        task_kind=task_kind,
        required_capabilities=tuple(required_capabilities),
        privacy="internal",
        approval_required=approval_required,
        approved=approved,
        attributes={
            "project_id": entity.id,
            "project_source_id": entity.attributes.get("source_id"),
            "entrypoint": entrypoint,
            "canonical_revision_required": True,
            "canonical_revision": registry.revision,
            "canonical_item_hash": canonical_hash(entity),
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reconstruct an existing ZIP project into a reviewable Synapse proposal"
    )
    parser.add_argument("archive", type=Path)
    parser.add_argument("--owner", default="human")
    parser.add_argument("--project-name")
    parser.add_argument("--report", type=Path, help="기존 report를 원본 ZIP hash와 함께 재검증")
    parser.add_argument("--answers", type=Path, help="question_id -> answer JSON object")
    parser.add_argument(
        "--approve", action="store_true", help="정본 commit이 아닌 Registry Proposal만 출력"
    )
    parser.add_argument("--actor", default="human")
    args = parser.parse_args(argv)
    try:
        report = (
            load_bundle_report(args.report, archive=args.archive)
            if args.report
            else inspect_bundle(args.archive)
        )
        proposal = reconstruct_project(
            report,
            owner=args.owner,
            project_name=args.project_name,
        )
        if args.answers:
            raw_answers = json.loads(
                args.answers.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
            proposal = proposal.answer_questions(raw_answers)
        output = proposal.to_record()
        if args.approve:
            output["registry_proposal"] = to_record(
                proposal.to_registry_proposal(
                    approved=True,
                    actor=args.actor,
                    archive=args.archive,
                )
            )
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (
        BundleError,
        ProjectReconstructionError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ) as exc:
        parser.error(str(exc))
        return 2


def _observation_provenance(report: BundleReport) -> Provenance:
    return Provenance(
        source_id=report.source_id,
        method="project_reconstruction",
        locator=f"archive:{report.archive_sha256}",
        captured_at=f"archive:{report.archive_sha256}",
    )


def _user_answer_provenance(project_id: str, question_id: str, revision: int) -> Provenance:
    return Provenance(
        source_id=f"proposal:{project_id}",
        method="user_answer",
        locator=question_id,
        captured_at=f"reconstruction:revision:{revision}",
    )


def _caller_input_provenance(report: BundleReport, field: str) -> Provenance:
    return Provenance(
        source_id=f"input:project:{report.archive_sha256}",
        method="explicit_input",
        locator=field,
        captured_at="reconstruction:revision:0",
    )


def _finding(
    report: BundleReport,
    *,
    subject: str,
    status: ObservationStatus,
    value: Any,
    evidence_paths: Sequence[str],
    detail: str,
    question_id: str | None = None,
    provenance: Sequence[Provenance] | None = None,
) -> ProjectFinding:
    return ProjectFinding(
        id=f"finding:{report.archive_sha256}:{subject}",
        subject=subject,
        status=status,
        value=value,
        evidence_paths=tuple(evidence_paths),
        question_id=question_id,
        detail=detail,
        provenance=(
            tuple(provenance)
            if provenance is not None
            else (_observation_provenance(report),)
        ),
    )


def _question_id(report: BundleReport, subject: str) -> str:
    return f"question:{report.archive_sha256}:{subject}"


def _questions_for_findings(
    report: BundleReport,
    findings: Sequence[ProjectFinding],
    ir: SynapseIR,
) -> tuple[OpenQuestion, ...]:
    root_id = f"bundle:{report.archive_sha256}"
    result: list[OpenQuestion] = []
    for finding in findings:
        if finding.question_id is None:
            continue
        if finding.status not in {
            ObservationStatus.INFERRED,
            ObservationStatus.UNKNOWN,
            ObservationStatus.CONFLICT,
        }:
            continue
        affected = [root_id]
        affected.extend(
            node.id
            for node in ir.nodes
            if node.attributes.get("path") in set(finding.evidence_paths)
        )
        suggestions = tuple(str(value) for value in _suggestions(finding))
        result.append(
            OpenQuestion(
                id=finding.question_id,
                question=_question_text(finding),
                why_needed=finding.detail,
                affected_nodes=tuple(dict.fromkeys(affected)),
                suggestions=suggestions,
                blocking=finding.subject in {"project_name", "entrypoint"},
            )
        )
    return tuple(result)


def _question_text(finding: ProjectFinding) -> str:
    if finding.subject == "project_name":
        return "이 기존 프로젝트의 Canonical 이름으로 사용할 값을 확인해 주세요."
    if finding.subject == "entrypoint":
        return "관찰된 파일 중 실제 프로젝트 진입점으로 사용할 경로를 확인해 주세요."
    return f"관찰된 프로젝트 정보 '{finding.subject}'를 확인해 주세요."


def _suggestions(finding: ProjectFinding) -> tuple[str, ...]:
    if finding.subject == "entrypoint":
        return tuple(finding.evidence_paths)
    if isinstance(finding.value, (list, tuple)):
        return tuple(str(value) for value in finding.value)
    if finding.value is None:
        return ()
    return (str(finding.value),)


def _candidate_status(candidates: Sequence[str]) -> ObservationStatus:
    if len(candidates) == 1:
        return ObservationStatus.INFERRED
    if len(candidates) > 1:
        return ObservationStatus.CONFLICT
    return ObservationStatus.UNKNOWN


def _entrypoint_detail(candidates: Sequence[str]) -> str:
    if not candidates:
        return "no bounded entrypoint candidate was observed"
    if len(candidates) == 1:
        return "one filename heuristic candidate was observed; user confirmation is required"
    return "multiple filename heuristic candidates were observed; automatic selection is forbidden"


def _entrypoint_candidates(report: BundleReport) -> tuple[str, ...]:
    known_names = {
        "main.py",
        "app.py",
        "run.py",
        "cli.py",
        "main.js",
        "index.js",
        "main.ts",
        "index.ts",
    }
    observed_paths = {entry.path for entry in report.entries}
    explicit: list[str] = []
    _, normalised_claim = _manifest_entrypoint_claim(report)
    if normalised_claim in observed_paths:
        explicit.append(normalised_claim)
    observed = [
        entry.path for entry in report.entries if Path(entry.path).name.casefold() in known_names
    ]
    return tuple(sorted(set(explicit + observed)))


def _manifest_entrypoint_claim(report: BundleReport) -> tuple[str | None, str | None]:
    if not isinstance(report.manifest, Mapping):
        return None, None
    claimed = report.manifest.get("entrypoint")
    if not isinstance(claimed, str) or not claimed.strip():
        return None, None
    raw = claimed.strip()
    try:
        return raw, _normalise_answer_path(raw)
    except ProjectReconstructionError:
        return raw, None


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProjectReconstructionError(f"answers JSON에 중복된 question_id가 있습니다: {key}")
        result[key] = value
    return result


def _manifest_name_candidates(report: BundleReport) -> tuple[str, ...]:
    if not isinstance(report.manifest, Mapping):
        return ()
    values: list[str] = []
    for key in ("name", "project_name", "title"):
        value = report.manifest.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return tuple(dict.fromkeys(values))


def _readme_paths(paths: Sequence[str]) -> tuple[str, ...]:
    return tuple(sorted(path for path in paths if Path(path).name.casefold() == "readme.md"))


def _authority_paths(report: BundleReport) -> tuple[str, ...]:
    return tuple(sorted(entry.path for entry in report.entries if entry.kind == "authority"))


def _legacy_paths(paths: Sequence[str]) -> tuple[str, ...]:
    markers = ("legacy", "deprecated", "/old/", "_old", "old_")
    return tuple(
        sorted(path for path in paths if any(marker in path.casefold() for marker in markers))
    )


def _kind_counts(report: BundleReport) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in report.entries:
        counts[entry.kind] = counts.get(entry.kind, 0) + 1
    return dict(sorted(counts.items()))


def _normalise_answer_path(value: str) -> str:
    clean = value.strip().replace("\\", "/")
    path = PurePosixPath(clean)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ProjectReconstructionError("entrypoint 답은 bundle 내부의 상대 경로여야 합니다.")
    return str(path)


def _finding_scalar(finding: ProjectFinding, subject: str) -> str:
    if finding.subject != subject:
        raise ProjectReconstructionError(f"잘못된 finding subject입니다: {finding.subject}")
    if finding.status is not ObservationStatus.CONFIRMED:
        raise ProjectReconstructionError(f"{subject}가 CONFIRMED가 아닙니다.")
    value = finding.value
    if not isinstance(value, str) or not value.strip():
        raise ProjectReconstructionError(f"{subject} 값이 비어 있습니다.")
    return value.strip()


def _finding_paths(finding: ProjectFinding) -> tuple[str, ...]:
    return tuple(finding.evidence_paths)


def _observed_paths(ir: SynapseIR) -> tuple[str, ...]:
    return tuple(
        sorted(str(node.attributes["path"]) for node in ir.nodes if "path" in node.attributes)
    )


__all__ = [
    "PROJECT_RECONSTRUCTION_SCHEMA",
    "ObservationStatus",
    "ProjectFinding",
    "ProjectReconstructionError",
    "ProjectReconstructionProposal",
    "ProjectReconstructionStaleError",
    "action_request_for_project",
    "main",
    "reconstruct_project",
]


if __name__ == "__main__":
    raise SystemExit(main())

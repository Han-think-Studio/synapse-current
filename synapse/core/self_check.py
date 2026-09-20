"""Small package self-check used by the root launcher."""

import json
import tempfile
from pathlib import Path

import yaml

from synapse.core.action import ActionRequest
from synapse.core.contracts import Entity, LifecycleStatus, Projection, Provenance, Source
from synapse.core.events import EventBus
from synapse.core.idea_session import create_idea_seed
from synapse.core.infigraph_adapter import InfigraphJsonAdapter
from synapse.core.infigraph_process import (
    ExternalProcessResult,
    ExternalSensorProcessRequest,
    InfigraphProcessAdapter,
)
from synapse.core.invariants import (
    assert_projection_is_read_only,
    validate_core,
    validate_propagation,
)
from synapse.core.preset_catalog import PresetCatalogError, load_preset_catalog
from synapse.core.registry import (
    CanonicalRegistry,
    Proposal,
    ProposalOperation,
    ProposalValidator,
)
from synapse.core.repair_rules import BUILT_IN_RULES
from synapse.core.resolver import propagate
from synapse.core.session_store import build_session_snapshot_payload
from synapse.core.spec_consistency import SpecConsistencyError, SpecConsistencyGate
from synapse.core.spec_drift import SpecDriftError, check_projection_drift
from synapse.core.structural_adapter import import_structural_observation
from synapse.core.structural_context import (
    StructuralContextRequest,
    build_bounded_structural_context,
)
from synapse.core.structural_dispatch import run_structural_dispatch
from synapse.core.structural_export_report import materialize_structural_export_report
from synapse.core.structural_export_report_inspection import (
    inspect_structural_export_report_file,
)
from synapse.core.structural_export_review import review_supplied_structural_export
from synapse.core.structural_observation import (
    StructuralEdge,
    StructuralEvidence,
    StructuralNode,
    build_structural_observation,
)
from synapse.core.structural_policy import evaluate_structural_policy
from synapse.core.structural_policy_gate import evaluate_structural_policy_gate
from synapse.core.structural_reactive import (
    STRUCTURAL_INVALIDATION_EVENT,
    build_structural_invalidation_batch,
    emit_structural_invalidations,
)
from synapse.core.structural_review import run_explicit_structural_review
from synapse.core.structure_projection import project_structural_observation
from synapse.core.workspace import WorkspaceChange, WorkspaceObservation, WorkspaceSnapshot
from synapse.core.workspace_structural_refresh import run_explicit_workspace_structural_refresh
from synapse.runtime.contracts import RuntimeRequest, RuntimeResponse
from synapse.runtime.lmstudio import LMStudioAdapter
from synapse.runtime.operation import run_explicit_runtime_operation


def run_self_check() -> dict[str, object]:
    root = Path(__file__).resolve().parents[2]
    source = Source(
        id="source:self-check",
        owner="synapse-core",
        source_type="self_check",
        locator="runtime://self-check",
    )
    entity = Entity(
        id="entity:self-check",
        canonical_key="self-check.entity",
        owner="synapse-core",
        name="Self Check",
        entity_type="test",
        status=LifecycleStatus.CONFIRMED,
        provenance=(Provenance(source_id=source.id, method="self_check"),),
    )
    projection = Projection(
        id="projection:self-check",
        owner="synapse-core",
        locale="ko",
        purpose="launcher",
        canonical_revision=1,
        canonical_ids=(entity.id,),
        content={"status": entity.status.value},
    )
    validate_core([entity])
    assert_projection_is_read_only(projection)
    registry = CanonicalRegistry()
    proposal = Proposal(
        candidate=Entity(
            id="entity:registry-check",
            owner="synapse-core",
            name="Registry Check",
            entity_type="test",
            status=LifecycleStatus.CONFIRMED,
            provenance=(Provenance(source_id=source.id, method="self_check"),),
        ),
        operation=ProposalOperation.CREATE,
        actor="self-check",
    )
    receipt = ProposalValidator().validate(proposal, registry)
    registry.commit(proposal, receipt)
    registry_is_valid = registry.revision == 1
    reactive_dependent = Entity(
        id="entity:self-check-dependent",
        owner="synapse-core",
        name="Self Check Dependent",
        entity_type="test",
        status=LifecycleStatus.CONFIRMED,
        depends_on=(entity.id,),
        provenance=(Provenance(source_id=source.id, method="self_check"),),
    )
    reactive_before = (entity, reactive_dependent)
    reactive_result = propagate(reactive_before, entity.id)
    validate_propagation(reactive_before, reactive_result)
    reactive_gate_is_valid = reactive_result.items[reactive_dependent.id].status is LifecycleStatus.UNRESOLVED
    relink_trigger = Entity(
        id="entity:self-check-deprecated",
        owner="synapse-core",
        name="Self Check Deprecated",
        entity_type="test",
        status=LifecycleStatus.DEPRECATED,
        provenance=(Provenance(source_id=source.id, method="self_check"),),
    )
    relink_successor = Entity(
        id="entity:self-check-successor",
        owner="synapse-core",
        name="Self Check Successor",
        entity_type="test",
        status=LifecycleStatus.CONFIRMED,
        provenance=(Provenance(source_id=source.id, method="self_check"),),
    )
    relink_dependent = Entity(
        id="entity:self-check-relink-dependent",
        owner="synapse-core",
        name="Self Check Relink Dependent",
        entity_type="fact",
        status=LifecycleStatus.CONFIRMED,
        depends_on=(relink_trigger.id,),
        provenance=(Provenance(source_id=source.id, method="self_check"),),
    )
    relink_before = (relink_trigger, relink_successor, relink_dependent)
    relink_result = propagate(
        relink_before,
        relink_trigger.id,
        mode="rule_based",
        rules=BUILT_IN_RULES,
        successor_id=relink_successor.id,
    )
    validate_propagation(relink_before, relink_result)
    relink_gate_is_valid = (
        relink_result.items[relink_dependent.id].status is LifecycleStatus.CONFIRMED
        and relink_result.items[relink_dependent.id].depends_on == (relink_successor.id,)
    )
    spec_path = root / "spec" / "README.md"
    spec_files = [
        root / "spec" / "architecture.yaml",
        root / "spec" / "principles.yaml",
        root / "spec" / "contracts.yaml",
        root / "spec" / "terminology.yaml",
        root / "spec" / "phases.yaml",
        root / "spec" / "document_manifest.yaml",
    ]
    spec_is_valid = spec_path.exists() and all(path.exists() for path in spec_files)
    spec_consistency = None
    current_phase = "unknown"
    if spec_is_valid:
        architecture = yaml.safe_load(
            (root / "spec" / "architecture.yaml").read_text(encoding="utf-8")
        )
        contracts = yaml.safe_load(
            (root / "spec" / "contracts.yaml").read_text(encoding="utf-8")
        )
        phases = yaml.safe_load((root / "spec" / "phases.yaml").read_text(encoding="utf-8"))
        current_phase = f"Phase {phases.get('current_phase', 'unknown')}"
        spec_is_valid = (
            architecture.get("authority") == "spec/"
            and contracts.get("canonical_source") == "spec/"
            and len(contracts.get("status_enum", [])) == 6
        )
        try:
            spec_consistency = SpecConsistencyGate(root).assert_valid()
        except SpecConsistencyError as exc:
            spec_is_valid = False
            spec_consistency = exc.report
    projections_are_present = all(
        (root / "docs" / locale).is_dir() for locale in ("ko", "en", "zh-CN")
    )
    preset_catalog_executed = False
    preset_catalog_report: dict[str, object]
    try:
        catalog = load_preset_catalog()
        preset_catalog_report = catalog.status()
        preset_catalog_executed = (
            catalog.preset_ids == ("general", "software", "research", "content", "automation")
            and catalog.category_ids == ("general", "software", "research", "content", "automation")
            and catalog.slot_count == 16
        )
    except PresetCatalogError as exc:
        preset_catalog_report = {"passed": False, "error": str(exc)}
    spec_drift_executed = False
    spec_drift_report: dict[str, object]
    try:
        spec_drift_report = check_projection_drift(root / "spec" / "document_manifest.yaml").as_dict()
        spec_drift_executed = True
    except SpecDriftError as exc:
        spec_drift_report = {
            "passed": False,
            "issue_count": 1,
            "error": str(exc),
        }
    guided_session_snapshot_executed = False
    guided_session_snapshot_report: dict[str, object]
    try:
        snapshot_raw_idea = "self-check guided session\r\n원문 보존"
        snapshot_seed = create_idea_seed(
            "Self Check Guided Session",
            snapshot_raw_idea,
            preset_hint="general",
            owner="self-check",
            source="self-check",
        )
        snapshot_payload = build_session_snapshot_payload(
            {
                "session_id": "self-check:guided-session",
                "stage": "IDEA_EXPANSION_READY",
                "seed": snapshot_seed.to_record(),
                "category_ids": ["general"],
                "detail_answers": {},
                "answers": {},
            }
        )
        guided_session_snapshot_executed = (
            snapshot_payload["seed"]["raw_text"] == snapshot_raw_idea
            and snapshot_payload["seed"]["raw_hash"] == snapshot_seed.raw_hash
            and snapshot_payload["candidate_refs"] == []
            and snapshot_payload["workspace_binding"] is None
        )
        guided_session_snapshot_report = {
            "passed": guided_session_snapshot_executed,
            "filesystem_mutation": False,
            "canonical_mutation": False,
            "automatic": False,
        }
    except (TypeError, ValueError) as exc:
        guided_session_snapshot_report = {
            "passed": False,
            "filesystem_mutation": False,
            "canonical_mutation": False,
            "automatic": False,
            "error": str(exc),
        }
    structural_intake_executed = False
    structural_intake_report: dict[str, object]
    try:
        structural_evidence = StructuralEvidence(
            id="evidence:self-check-structural",
            source_id="sensor:self-check",
            kind="self_check",
            locator="runtime://self-check/structural",
        )
        structural_observation = build_structural_observation(
            source_id="source:self-check-structural",
            sensor_type="self-check",
            sensor_version="1",
            workspace_hash="sha256:self-check-workspace",
            nodes=(
                StructuralNode(
                    id="node:self-check-structural",
                    kind="module",
                    label="Self Check Structural Node",
                    evidence_refs=(structural_evidence.id,),
                ),
            ),
            evidence=(structural_evidence,),
        )
        structural_import = import_structural_observation(structural_observation.to_record())
        structural_projection = project_structural_observation(structural_import.observation)
        structural_intake_executed = (
            structural_import.receipt.observation_id == structural_observation.id
            and structural_import.receipt.observation_hash == structural_observation.observation_hash
            and structural_projection.source_kind == "structural_observation"
            and structural_projection.metadata["sensor_type"] == "self-check"
            and structural_projection.canonical_mutation is False
            and structural_projection.filesystem_mutation is False
        )
        structural_intake_report = {
            "passed": structural_intake_executed,
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }
    except (TypeError, ValueError) as exc:
        structural_intake_report = {
            "passed": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
            "error": str(exc),
        }
    bounded_context_executed = False
    bounded_context_report: dict[str, object]
    try:
        context_evidence = StructuralEvidence(
            id="evidence:self-check-context",
            source_id="sensor:self-check",
            kind="self_check",
            locator="runtime://self-check/context",
        )
        context_observation = build_structural_observation(
            source_id="source:self-check-context",
            sensor_type="self-check",
            sensor_version="1",
            workspace_hash="sha256:self-check-context-workspace",
            nodes=(
                StructuralNode(
                    id="node:self-check-context-target",
                    kind="module",
                    label="Self Check Context Target",
                    evidence_refs=(context_evidence.id,),
                ),
                StructuralNode(
                    id="node:self-check-context-child",
                    kind="symbol",
                    label="Self Check Context Child",
                    evidence_refs=(context_evidence.id,),
                ),
            ),
            edges=(
                StructuralEdge(
                    source_id="node:self-check-context-target",
                    target_id="node:self-check-context-child",
                    relation="imports",
                    evidence_refs=(context_evidence.id,),
                ),
            ),
            evidence=(context_evidence,),
        )
        context_request = StructuralContextRequest(
            target_id="node:self-check-context-target",
            source_hash=context_observation.observation_hash,
            relations=("IMPORTS",),
            max_depth=1,
            max_nodes=2,
            direction="outbound",
        )
        bounded_context = build_bounded_structural_context(context_observation, context_request)
        bounded_context_executed = (
            bounded_context.source_hash == context_observation.observation_hash
            and len(bounded_context.nodes) <= context_request.max_nodes
            and all(node.depth <= context_request.max_depth for node in bounded_context.nodes)
            and bounded_context.canonical_mutation is False
            and bounded_context.filesystem_mutation is False
        )
        bounded_context_report = {
            "passed": bounded_context_executed,
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }
    except (TypeError, ValueError) as exc:
        bounded_context_report = {
            "passed": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
            "error": str(exc),
        }
    structural_policy_executed = False
    structural_policy_report: dict[str, object]
    policy_report = None
    try:
        policy_evidence = StructuralEvidence(
            id="evidence:self-check-policy",
            source_id="sensor:self-check",
            kind="self_check",
            locator="runtime://self-check/policy",
        )
        policy_observation = build_structural_observation(
            source_id="source:self-check-policy",
            sensor_type="self-check",
            sensor_version="1",
            workspace_hash="sha256:self-check-policy-workspace",
            nodes=(
                StructuralNode(
                    id="node:self-check-policy-core",
                    kind="module",
                    label="Self Check Policy Core",
                    attributes={"layer": "CORE"},
                    evidence_refs=(policy_evidence.id,),
                ),
                StructuralNode(
                    id="node:self-check-policy-runtime",
                    kind="module",
                    label="Self Check Policy Runtime",
                    attributes={"layer": "RUNTIME"},
                    evidence_refs=(policy_evidence.id,),
                ),
            ),
            edges=(
                StructuralEdge(
                    source_id="node:self-check-policy-core",
                    target_id="node:self-check-policy-runtime",
                    relation="imports",
                    evidence_refs=(policy_evidence.id,),
                ),
            ),
            evidence=(policy_evidence,),
        )
        policy_report = evaluate_structural_policy(policy_observation)
        structural_policy_executed = (
            len(policy_report.violations) == 1
            and policy_report.violations[0].status == "OBSERVED"
            and policy_report.violations[0].evidence_refs == (policy_evidence.id,)
            and policy_report.canonical_mutation is False
            and policy_report.filesystem_mutation is False
            and "score" not in policy_report.to_record()
        )
        structural_policy_report = {
            "passed": structural_policy_executed,
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }
    except (TypeError, ValueError) as exc:
        structural_policy_report = {
            "passed": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
            "error": str(exc),
        }
    structural_policy_gate_executed = False
    structural_policy_gate_report: dict[str, object]
    try:
        if policy_report is None:
            raise ValueError("policy report가 생성되지 않았습니다.")
        gate_result = evaluate_structural_policy_gate(policy_report)
        structural_policy_gate_executed = (
            gate_result.status == "FAIL"
            and gate_result.exit_code == 1
            and gate_result.observed_finding_ids == (policy_report.violations[0].id,)
            and gate_result.unresolved_finding_ids == ()
            and gate_result.canonical_mutation is False
            and gate_result.filesystem_mutation is False
            and "score" not in gate_result.to_record()
        )
        structural_policy_gate_report = {
            "passed": structural_policy_gate_executed,
            "status": gate_result.status,
            "exit_code": gate_result.exit_code,
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }
    except (TypeError, ValueError) as exc:
        structural_policy_gate_report = {
            "passed": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
            "error": str(exc),
        }
    structural_reactive_executed = False
    structural_reactive_report: dict[str, object]
    try:
        reactive_snapshot = WorkspaceSnapshot(
            workspace_path="runtime://self-check/workspace",
            status="READY",
            exists=True,
            is_directory=True,
            writable=False,
            plan_id="self-check-plan",
            files=(),
            progress={},
        )
        reactive_observation = WorkspaceObservation(
            snapshot=reactive_snapshot,
            changes=(
                WorkspaceChange(
                    path="src/main.py",
                    kind="MODIFIED",
                    previous_sha256="sha256:before",
                    current_sha256="sha256:after",
                ),
            ),
        )
        invalidation_batch = build_structural_invalidation_batch(reactive_observation)
        emitted_events = emit_structural_invalidations(EventBus(), invalidation_batch)
        structural_reactive_executed = (
            len(invalidation_batch.invalidations) == 1
            and len(emitted_events) == 1
            and emitted_events[0].type == STRUCTURAL_INVALIDATION_EVENT
            and emitted_events[0].payload["invalidation"]["path"] == "src/main.py"
            and emitted_events[0].payload["invalidation"]["canonical_mutation"] is False
            and emitted_events[0].payload["invalidation"]["filesystem_mutation"] is False
        )
        structural_reactive_report = {
            "passed": structural_reactive_executed,
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }
    except (TypeError, ValueError) as exc:
        structural_reactive_report = {
            "passed": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
            "error": str(exc),
        }
    infigraph_adapter_executed = False
    infigraph_adapter_report: dict[str, object]
    try:
        infigraph_import = InfigraphJsonAdapter().import_observation(
            {
                "schema_version": "infigraph.export.v1",
                "provider": "infigraph",
                "provider_version": "self-check",
                "source_id": "source:self-check-infigraph",
                "workspace_hash": "sha256:self-check-infigraph-workspace",
                "captured_at": "2026-08-30T00:00:00+00:00",
                "scope": {"root": ".", "include": [], "exclude": [], "max_depth": 1},
                "nodes": [
                    {
                        "id": "node:self-check-infigraph-source",
                        "kind": "module",
                        "label": "Self Check Infigraph Source",
                        "locator": "self_check.py",
                        "attributes": {},
                        "evidence_refs": ["evidence:self-check-infigraph"],
                    },
                    {
                        "id": "node:self-check-infigraph-target",
                        "kind": "module",
                        "label": "Self Check Infigraph Target",
                        "locator": "structural_policy.py",
                        "attributes": {},
                        "evidence_refs": ["evidence:self-check-infigraph"],
                    },
                ],
                "edges": [
                    {
                        "source_id": "node:self-check-infigraph-source",
                        "target_id": "node:self-check-infigraph-target",
                        "relation": "imports",
                        "evidence_refs": ["evidence:self-check-infigraph"],
                        "attributes": {},
                    }
                ],
                "issues": [],
                "evidence": [
                    {
                        "id": "evidence:self-check-infigraph",
                        "source_id": "infigraph:self-check",
                        "kind": "ast_export",
                        "locator": "runtime://self-check/infigraph",
                        "digest": None,
                        "metadata": {},
                    }
                ],
                "unresolved": [],
            }
        )
        infigraph_adapter_executed = (
            infigraph_import.receipt.adapter_id == "infigraph-json"
            and infigraph_import.observation.sensor_type == "infigraph"
            and infigraph_import.observation.edges[0].relation == "IMPORTS"
            and infigraph_import.receipt.canonical_mutation is False
            and infigraph_import.receipt.filesystem_mutation is False
        )
        infigraph_adapter_report = {
            "passed": infigraph_adapter_executed,
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }
    except (TypeError, ValueError) as exc:
        infigraph_adapter_report = {
            "passed": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
            "error": str(exc),
        }
    structural_export_review_executed = False
    structural_export_review_report: dict[str, object]
    try:
        supplied_export = {
            "schema_version": "infigraph.export.v1",
            "provider": "infigraph",
            "provider_version": "self-check-export-review",
            "source_id": "source:self-check-export-review",
            "workspace_hash": "sha256:self-check-export-review-workspace",
            "captured_at": "2026-08-30T00:00:00+00:00",
            "scope": {"root": ".", "include": [], "exclude": [], "max_depth": 0},
            "nodes": [
                {
                    "id": "node:self-check-export-review",
                    "kind": "module",
                    "label": "Self Check Export Review",
                    "locator": "runtime://self-check/export-review",
                    "attributes": {},
                    "evidence_refs": ["evidence:self-check-export-review"],
                }
            ],
            "edges": [],
            "issues": [],
            "evidence": [
                {
                    "id": "evidence:self-check-export-review",
                    "source_id": "infigraph:self-check-export-review",
                    "kind": "ast_export",
                    "locator": "runtime://self-check/export-review",
                    "digest": None,
                    "metadata": {},
                }
            ],
            "unresolved": [],
        }
        export_review = review_supplied_structural_export(
            supplied_export,
            expected_workspace_hash=supplied_export["workspace_hash"],
        )
        export_review_record = export_review.to_record()
        structural_export_review_executed = (
            export_review.input_kind == "inline"
            and export_review.stages == (
                "EXPORT_READ",
                "OBSERVATION_IMPORTED",
                "POLICY_EVALUATED",
                "POLICY_GATED",
            )
            and export_review.policy_gate.status == "PASS"
            and export_review.policy_gate.exit_code == 0
            and export_review.policy_report.source_id == export_review.observation.source_id
            and export_review.policy_gate.report_id == export_review.policy_report.id
            and export_review_record["automatic"] is False
            and export_review_record["canonical_mutation"] is False
            and export_review_record["filesystem_mutation"] is False
        )
        structural_export_review_report = {
            "passed": structural_export_review_executed,
            "stage_count": len(export_review.stages),
            "gate_status": export_review.policy_gate.status,
            "input_kind": export_review.input_kind,
            "automatic": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }
    except (TypeError, ValueError) as exc:
        structural_export_review_report = {
            "passed": False,
            "automatic": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
            "error": str(exc),
        }
    structural_export_report_executed = False
    structural_export_report_result: dict[str, object]
    structural_export_report_inspection_executed = False
    structural_export_report_inspection_result: dict[str, object]
    verified_report_context_executed = False
    verified_report_context_result: dict[str, object]
    structural_executor_handoff_executed = False
    structural_executor_handoff_result: dict[str, object]
    structural_runtime_plan_executed = False
    structural_runtime_plan_result: dict[str, object]
    structural_provider_plan_executed = False
    structural_provider_plan_result: dict[str, object]
    structural_provider_dispatch_executed = False
    structural_provider_dispatch_result: dict[str, object]
    if structural_export_review_executed:
        try:
            with tempfile.TemporaryDirectory(prefix="synapse-self-check-report-") as temp_dir:
                report_path = Path(temp_dir) / "structural-review-report.json"
                materialized_report = materialize_structural_export_report(
                    export_review,
                    report_path,
                )
                report_text = report_path.read_text(encoding="utf-8")
                report_record = json.loads(report_text)
                structural_export_report_executed = (
                    report_path.is_file()
                    and report_record["id"] == materialized_report.id
                    and report_record["review_id"] == export_review.id
                    and report_record["gate_exit_code"] == 0
                    and report_record["output_file_written"] is True
                    and report_record["filesystem_mutation"] is True
                    and report_record["source_mutation"] is False
                    and report_record["workspace_mutation"] is False
                    and report_record["canonical_mutation"] is False
                    and "provider_version" not in report_text
                    and list(Path(temp_dir).glob("*.tmp")) == []
                )
                report_bytes_before_inspection = report_path.read_bytes()
                inspected_report = inspect_structural_export_report_file(report_path)
                inspection_record = inspected_report.to_record()
                structural_export_report_inspection_executed = (
                    inspected_report.report.id == materialized_report.id
                    and inspected_report.report.review.id == export_review.id
                    and inspected_report.report.review.policy_gate.status == "PASS"
                    and inspected_report.gate_exit_code == 0
                    and inspection_record["valid"] is True
                    and inspected_report.automatic is False
                    and inspected_report.filesystem_mutation is False
                    and inspected_report.source_mutation is False
                    and inspected_report.workspace_mutation is False
                    and inspected_report.canonical_mutation is False
                    and report_path.read_bytes() == report_bytes_before_inspection
                    and list(Path(temp_dir).glob("*.tmp")) == []
                )
                structural_export_report_result = {
                    "passed": structural_export_report_executed,
                    "gate_exit_code": materialized_report.gate_exit_code,
                    "output_file_written": True,
                    "filesystem_mutation": True,
                    "source_mutation": False,
                    "workspace_mutation": False,
                    "canonical_mutation": False,
                }
                structural_export_report_inspection_result = {
                    "passed": structural_export_report_inspection_executed,
                    "gate_exit_code": inspected_report.gate_exit_code,
                    "valid": True,
                    "filesystem_mutation": False,
                    "source_mutation": False,
                    "workspace_mutation": False,
                    "canonical_mutation": False,
                }
                context_request = StructuralContextRequest(
                    target_id=inspected_report.report.review.observation.nodes[0].id,
                    source_hash=inspected_report.report.review.observation.observation_hash,
                    relations=("IMPORTS",),
                    max_depth=1,
                    max_nodes=10,
                    direction="both",
                )
                provider_adapter = LMStudioAdapter("http://127.0.0.1:1234/v1")

                class SelfCheckTransport:
                    def __init__(self) -> None:
                        self.calls: list[tuple[object, float]] = []

                    def send(self, request: object, *, timeout_seconds: float) -> dict[str, object]:
                        self.calls.append((request, timeout_seconds))
                        return {
                            "model": "self-check-structural-model",
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": "self-check structural dispatch response",
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {"completion_tokens": 1},
                        }

                dispatch_transport = SelfCheckTransport()
                action_request = ActionRequest(
                    id="action:self-check-structural-context",
                    task_kind="structural_context_review",
                    required_capabilities=("analysis",),
                    privacy="internal",
                    approval_required=True,
                    approved=True,
                    attributes={"context_id": context_request.target_id},
                )
                # One call, one owner. synapse.core.structural_dispatch holds the
                # review-to-dispatch order; this self-check verifies that order and
                # the CLI runs it. Neither keeps a second copy of the sequence.
                dispatch_run = run_structural_dispatch(
                    inspected_report,
                    context_request=context_request,
                    action_request=action_request,
                    model="self-check-structural-model",
                    adapter=provider_adapter,
                    transport=dispatch_transport,
                    dispatch_approved=True,
                    parameters={"temperature": 0.0},
                    clock=lambda: "2026-08-31T00:00:00+00:00",
                )
                report_context = dispatch_run.context_result
                handoff = dispatch_run.handoff
                runtime_plan = dispatch_run.runtime_plan
                provider_plan = dispatch_run.provider_plan
                dispatch = dispatch_run.dispatch
                context_record = report_context.to_record()
                verified_report_context_executed = (
                    report_context.inspection.id == inspected_report.id
                    and report_context.context.request == context_request
                    and report_context.context.source_hash
                    == inspected_report.report.review.observation.observation_hash
                    and report_context.report_gate_status == "PASS"
                    and report_context.report_gate_exit_code == 0
                    and context_record["context_projected"] is True
                    and report_context.automatic is False
                    and report_context.filesystem_mutation is False
                    and report_context.source_mutation is False
                    and report_context.workspace_mutation is False
                    and report_context.canonical_mutation is False
                    and report_path.read_bytes() == report_bytes_before_inspection
                    and "provider_version" not in report_context.to_json()
                )
                verified_report_context_result = {
                    "passed": verified_report_context_executed,
                    "gate_status": report_context.report_gate_status,
                    "gate_exit_code": report_context.report_gate_exit_code,
                    "context_projected": True,
                    "automatic": False,
                    "filesystem_mutation": False,
                    "source_mutation": False,
                    "workspace_mutation": False,
                    "canonical_mutation": False,
                }
                handoff_record = handoff.to_record()
                structural_executor_handoff_executed = (
                    handoff.context_result.id == report_context.id
                    and handoff.action_request == action_request
                    and handoff.route.request == action_request
                    and handoff.route.status == "READY"
                    and handoff.route.selected is not None
                    and handoff.route.selected.id == "lmstudio"
                    and handoff.status == "READY"
                    and handoff_record["verified_context"]["id"] == report_context.id
                    and handoff_record["dispatch_performed"] is False
                    and handoff_record["execution_allowed"] is False
                    and handoff.automatic is False
                    and handoff.filesystem_mutation is False
                    and handoff.source_mutation is False
                    and handoff.workspace_mutation is False
                    and handoff.canonical_mutation is False
                    and "RuntimeRequest" not in handoff.to_json()
                )
                structural_executor_handoff_result = {
                    "passed": structural_executor_handoff_executed,
                    "status": handoff.status,
                    "route_status": handoff.route.status,
                    "dispatch_performed": False,
                    "execution_allowed": False,
                    "automatic": False,
                    "filesystem_mutation": False,
                    "source_mutation": False,
                    "workspace_mutation": False,
                    "canonical_mutation": False,
                }
                runtime_plan_record = runtime_plan.to_record()
                runtime_prompt = json.loads(runtime_plan.runtime_request.messages[1]["content"])
                structural_runtime_plan_executed = (
                    runtime_plan.handoff.id == handoff.id
                    and runtime_plan.provider == "lmstudio"
                    and runtime_plan.model == "self-check-structural-model"
                    and runtime_plan.status == "READY"
                    and runtime_plan.stages == (
                        "HANDOFF_VERIFIED",
                        "RUNTIME_REQUEST_PREPARED",
                    )
                    and runtime_prompt["lineage"]["context_id"] == report_context.context.id
                    and runtime_prompt["structural_gate"] == {
                        "status": "PASS",
                        "passed": True,
                        "exit_code": 0,
                    }
                    and runtime_prompt["context"] == report_context.context.to_record()
                    and runtime_plan_record["dispatch_performed"] is False
                    and runtime_plan_record["execution_allowed"] is False
                    and runtime_plan.automatic is False
                    and runtime_plan.filesystem_mutation is False
                    and runtime_plan.source_mutation is False
                    and runtime_plan.workspace_mutation is False
                    and runtime_plan.canonical_mutation is False
                    and "raw_export" not in runtime_plan.to_json()
                )
                structural_runtime_plan_result = {
                    "passed": structural_runtime_plan_executed,
                    "status": runtime_plan.status,
                    "provider": runtime_plan.provider,
                    "model": runtime_plan.model,
                    "dispatch_performed": False,
                    "execution_allowed": False,
                    "automatic": False,
                    "filesystem_mutation": False,
                    "source_mutation": False,
                    "workspace_mutation": False,
                    "canonical_mutation": False,
                }
                provider_plan_record = provider_plan.to_record()
                structural_provider_plan_executed = (
                    provider_plan.runtime_plan.id == runtime_plan.id
                    and provider_plan.provider == "lmstudio"
                    and provider_plan.endpoint
                    == "http://127.0.0.1:1234/v1/chat/completions"
                    and provider_plan.prepared_request.request_id
                    == runtime_plan.runtime_request.request_id
                    and provider_plan.prepared_request.body["model"] == runtime_plan.model
                    and provider_plan.prepared_request.body["messages"]
                    == [dict(message) for message in runtime_plan.runtime_request.messages]
                    and provider_plan.status == "READY"
                    and provider_plan.stages == (
                        "RUNTIME_PLAN_VERIFIED",
                        "PROVIDER_REQUEST_PREPARED",
                    )
                    and provider_plan_record["network_dispatched"] is False
                    and provider_plan_record["dispatch_performed"] is False
                    and provider_plan_record["execution_allowed"] is False
                    and provider_plan.automatic is False
                    and provider_plan.filesystem_mutation is False
                    and provider_plan.source_mutation is False
                    and provider_plan.workspace_mutation is False
                    and provider_plan.canonical_mutation is False
                    and "raw_export" not in provider_plan.to_json()
                )
                structural_provider_plan_result = {
                    "passed": structural_provider_plan_executed,
                    "status": provider_plan.status,
                    "provider": provider_plan.provider,
                    "endpoint": provider_plan.endpoint,
                    "network_dispatched": False,
                    "dispatch_performed": False,
                    "execution_allowed": False,
                    "automatic": False,
                    "filesystem_mutation": False,
                    "source_mutation": False,
                    "workspace_mutation": False,
                    "canonical_mutation": False,
                }
                dispatch_record = dispatch.to_record()
                structural_provider_dispatch_executed = (
                    len(dispatch_transport.calls) == 1
                    and dispatch_transport.calls[0][0] is provider_plan.prepared_request
                    and dispatch.status == "SUCCEEDED"
                    and dispatch.stages == (
                        "PROVIDER_PLAN_VERIFIED",
                        "REQUEST_VALIDATED",
                        "DISPATCH_APPROVED",
                        "PROVIDER_INVOKED",
                        "RESPONSE_NORMALIZED",
                    )
                    and dispatch.provider_plan.id == provider_plan.id
                    and dispatch.operation.request == runtime_plan.runtime_request
                    and dispatch.operation.response is not None
                    and dispatch_record["operation"]["response_metadata"]["text_length"]
                    == len("self-check structural dispatch response")
                    and "self-check structural dispatch response" not in str(dispatch_record)
                    and dispatch.dispatch_approved is True
                    and dispatch.transport_invoked is True
                    and dispatch.provider_invoked is True
                    and dispatch.automatic is False
                    and dispatch.retry_count == 0
                    and dispatch.fallback_used is False
                    and dispatch.filesystem_mutation is False
                    and dispatch.source_mutation is False
                    and dispatch.workspace_mutation is False
                    and dispatch.canonical_mutation is False
                )
                structural_provider_dispatch_result = {
                    "passed": structural_provider_dispatch_executed,
                    "status": dispatch.status,
                    "provider": provider_plan.provider,
                    "endpoint": provider_plan.endpoint,
                    "dispatch_approved": True,
                    "transport_invoked": True,
                    "provider_invoked": True,
                    "automatic": False,
                    "retry_count": 0,
                    "fallback_used": False,
                    "filesystem_mutation": False,
                    "source_mutation": False,
                    "workspace_mutation": False,
                    "canonical_mutation": False,
                }
        except (OSError, TypeError, ValueError) as exc:
            structural_export_report_result = {
                "passed": False,
                "output_file_written": False,
                "filesystem_mutation": False,
                "source_mutation": False,
                "workspace_mutation": False,
                "canonical_mutation": False,
                "error": str(exc),
            }
            structural_export_report_inspection_result = {
                "passed": False,
                "valid": False,
                "filesystem_mutation": False,
                "source_mutation": False,
                "workspace_mutation": False,
                "canonical_mutation": False,
                "error": str(exc),
            }
            verified_report_context_result = {
                "passed": False,
                "context_projected": False,
                "automatic": False,
                "filesystem_mutation": False,
                "source_mutation": False,
                "workspace_mutation": False,
                "canonical_mutation": False,
                "error": str(exc),
            }
            structural_executor_handoff_result = {
                "passed": False,
                "dispatch_performed": False,
                "execution_allowed": False,
                "automatic": False,
                "filesystem_mutation": False,
                "source_mutation": False,
                "workspace_mutation": False,
                "canonical_mutation": False,
                "error": str(exc),
            }
            structural_runtime_plan_result = {
                "passed": False,
                "dispatch_performed": False,
                "execution_allowed": False,
                "automatic": False,
                "filesystem_mutation": False,
                "source_mutation": False,
                "workspace_mutation": False,
                "canonical_mutation": False,
                "error": str(exc),
            }
            structural_provider_plan_result = {
                "passed": False,
                "network_dispatched": False,
                "dispatch_performed": False,
                "execution_allowed": False,
                "automatic": False,
                "filesystem_mutation": False,
                "source_mutation": False,
                "workspace_mutation": False,
                "canonical_mutation": False,
                "error": str(exc),
            }
            structural_provider_dispatch_result = {
                "passed": False,
                "dispatch_approved": False,
                "transport_invoked": False,
                "provider_invoked": False,
                "automatic": False,
                "retry_count": 0,
                "fallback_used": False,
                "filesystem_mutation": False,
                "source_mutation": False,
                "workspace_mutation": False,
                "canonical_mutation": False,
                "error": str(exc),
            }
    else:
        structural_export_report_result = {
            "passed": False,
            "output_file_written": False,
            "filesystem_mutation": False,
            "source_mutation": False,
            "workspace_mutation": False,
            "canonical_mutation": False,
            "error": "structural export review prerequisite failed",
        }
        structural_export_report_inspection_result = {
            "passed": False,
            "valid": False,
            "filesystem_mutation": False,
            "source_mutation": False,
            "workspace_mutation": False,
            "canonical_mutation": False,
            "error": "structural export report prerequisite failed",
        }
        verified_report_context_result = {
            "passed": False,
            "context_projected": False,
            "automatic": False,
            "filesystem_mutation": False,
            "source_mutation": False,
            "workspace_mutation": False,
            "canonical_mutation": False,
            "error": "structural export report prerequisite failed",
        }
        structural_executor_handoff_result = {
            "passed": False,
            "dispatch_performed": False,
            "execution_allowed": False,
            "automatic": False,
            "filesystem_mutation": False,
            "source_mutation": False,
            "workspace_mutation": False,
            "canonical_mutation": False,
            "error": "structural export report prerequisite failed",
        }
        structural_runtime_plan_result = {
            "passed": False,
            "dispatch_performed": False,
            "execution_allowed": False,
            "automatic": False,
            "filesystem_mutation": False,
            "source_mutation": False,
            "workspace_mutation": False,
            "canonical_mutation": False,
            "error": "structural export report prerequisite failed",
        }
        structural_provider_plan_result = {
            "passed": False,
            "network_dispatched": False,
            "dispatch_performed": False,
            "execution_allowed": False,
            "automatic": False,
            "filesystem_mutation": False,
            "source_mutation": False,
            "workspace_mutation": False,
            "canonical_mutation": False,
            "error": "structural export report prerequisite failed",
        }
        structural_provider_dispatch_result = {
            "passed": False,
            "dispatch_approved": False,
            "transport_invoked": False,
            "provider_invoked": False,
            "automatic": False,
            "retry_count": 0,
            "fallback_used": False,
            "filesystem_mutation": False,
            "source_mutation": False,
            "workspace_mutation": False,
            "canonical_mutation": False,
            "error": "structural export report prerequisite failed",
        }
    infigraph_process_executed = False
    infigraph_process_report: dict[str, object]
    try:
        process_export = {
            "schema_version": "infigraph.export.v1",
            "provider": "infigraph",
            "provider_version": "self-check-process",
            "source_id": "source:self-check-infigraph-process",
            "workspace_hash": "sha256:self-check-infigraph-process-workspace",
            "captured_at": "2026-08-30T00:00:00+00:00",
            "scope": {"root": ".", "include": [], "exclude": [], "max_depth": 0},
            "nodes": [
                {
                    "id": "node:self-check-infigraph-process",
                    "kind": "module",
                    "label": "Self Check Process Sensor",
                    "locator": "runtime://self-check/process",
                    "attributes": {},
                    "evidence_refs": ["evidence:self-check-infigraph-process"],
                }
            ],
            "edges": [],
            "issues": [],
            "evidence": [
                {
                    "id": "evidence:self-check-infigraph-process",
                    "source_id": "infigraph:self-check-process",
                    "kind": "ast_export",
                    "locator": "runtime://self-check/process",
                    "digest": None,
                    "metadata": {},
                }
            ],
            "unresolved": [],
        }
        process_calls: list[ExternalSensorProcessRequest] = []

        def process_runner(request: ExternalSensorProcessRequest) -> ExternalProcessResult:
            process_calls.append(request)
            return ExternalProcessResult(
                returncode=0,
                stdout=json.dumps(process_export, ensure_ascii=False).encode("utf-8"),
                stderr=b"",
            )

        process_request = ExternalSensorProcessRequest(
            request_id="request:self-check-infigraph-process",
            command=("self-check-infigraph-sensor", "--json"),
            timeout_seconds=5,
        )
        process_import = InfigraphProcessAdapter(runner=process_runner).execute(process_request)
        infigraph_process_executed = (
            process_calls == [process_request]
            and process_import.observation.sensor_type == "infigraph"
            and process_import.observation.source_id == "source:self-check-infigraph-process"
            and process_import.process_receipt.command == process_request.command
            and process_import.process_receipt.process_invoked is True
            and process_import.process_receipt.automatic is False
            and process_import.process_receipt.canonical_mutation is False
            and process_import.process_receipt.filesystem_mutation is False
        )
        infigraph_process_report = {
            "passed": infigraph_process_executed,
            "process_invoked": True,
            "automatic": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }
    except (TypeError, ValueError) as exc:
        infigraph_process_report = {
            "passed": False,
            "process_invoked": False,
            "automatic": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
            "error": str(exc),
        }
    structural_review_executed = False
    structural_review_report: dict[str, object]
    try:
        review_export = {
            "schema_version": "infigraph.export.v1",
            "provider": "infigraph",
            "provider_version": "self-check-review",
            "source_id": "source:self-check-structural-review",
            "workspace_hash": "sha256:self-check-structural-review-workspace",
            "captured_at": "2026-08-30T00:00:00+00:00",
            "scope": {"root": ".", "include": [], "exclude": [], "max_depth": 0},
            "nodes": [
                {
                    "id": "node:self-check-structural-review",
                    "kind": "module",
                    "label": "Self Check Structural Review",
                    "locator": "runtime://self-check/review",
                    "attributes": {},
                    "evidence_refs": ["evidence:self-check-structural-review"],
                }
            ],
            "edges": [],
            "issues": [],
            "evidence": [
                {
                    "id": "evidence:self-check-structural-review",
                    "source_id": "infigraph:self-check-review",
                    "kind": "ast_export",
                    "locator": "runtime://self-check/review",
                    "digest": None,
                    "metadata": {},
                }
            ],
            "unresolved": [],
        }
        review_calls: list[ExternalSensorProcessRequest] = []

        def review_runner(request: ExternalSensorProcessRequest) -> ExternalProcessResult:
            review_calls.append(request)
            return ExternalProcessResult(
                returncode=0,
                stdout=json.dumps(review_export, ensure_ascii=False).encode("utf-8"),
                stderr=b"",
            )

        review_request = ExternalSensorProcessRequest(
            request_id="request:self-check-structural-review",
            command=("self-check-structural-review", "--json"),
            timeout_seconds=5,
        )
        review = run_explicit_structural_review(
            review_request,
            adapter=InfigraphProcessAdapter(runner=review_runner),
            expected_workspace_hash=review_export["workspace_hash"],
        )
        structural_review_executed = (
            review_calls == [review_request]
            and review.stages == (
                "PROCESS_ACQUIRED",
                "OBSERVATION_IMPORTED",
                "POLICY_EVALUATED",
                "POLICY_GATED",
            )
            and review.policy_gate.status == "PASS"
            and review.policy_gate.exit_code == 0
            and review.policy_report.source_id == review.observation.source_id
            and review.policy_gate.report_id == review.policy_report.id
            and review.automatic is False
            and review.canonical_mutation is False
            and review.filesystem_mutation is False
        )
        structural_review_report = {
            "passed": structural_review_executed,
            "stage_count": len(review.stages),
            "gate_status": review.policy_gate.status,
            "automatic": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }
    except (TypeError, ValueError) as exc:
        structural_review_report = {
            "passed": False,
            "automatic": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
            "error": str(exc),
        }
    workspace_structural_refresh_executed = False
    workspace_structural_refresh_report: dict[str, object]
    try:
        refresh_snapshot = WorkspaceSnapshot(
            workspace_path="runtime://self-check/refresh",
            status="READY",
            exists=True,
            is_directory=True,
            writable=False,
            plan_id="self-check-refresh-plan",
            files=(),
            progress={},
        )
        refresh_observation = WorkspaceObservation(
            snapshot=refresh_snapshot,
            changes=(
                WorkspaceChange(
                    path="src/main.py",
                    kind="MODIFIED",
                    previous_sha256="sha256:before",
                    current_sha256="sha256:after",
                ),
            ),
        )
        refresh_batch = build_structural_invalidation_batch(refresh_observation)
        refresh_export = {
            "schema_version": "infigraph.export.v1",
            "provider": "infigraph",
            "provider_version": "self-check-workspace-refresh",
            "source_id": "source:self-check-workspace-refresh",
            "workspace_hash": refresh_batch.workspace_snapshot_hash,
            "captured_at": "2026-08-30T00:00:00+00:00",
            "scope": {"root": ".", "include": [], "exclude": [], "max_depth": 0},
            "nodes": [
                {
                    "id": "node:self-check-workspace-refresh",
                    "kind": "module",
                    "label": "Self Check Workspace Refresh",
                    "locator": "runtime://self-check/refresh",
                    "attributes": {},
                    "evidence_refs": ["evidence:self-check-workspace-refresh"],
                }
            ],
            "edges": [],
            "issues": [],
            "evidence": [
                {
                    "id": "evidence:self-check-workspace-refresh",
                    "source_id": "infigraph:self-check-workspace-refresh",
                    "kind": "ast_export",
                    "locator": "runtime://self-check/refresh",
                    "digest": None,
                    "metadata": {},
                }
            ],
            "unresolved": [],
        }
        refresh_calls: list[ExternalSensorProcessRequest] = []

        def refresh_runner(request: ExternalSensorProcessRequest) -> ExternalProcessResult:
            refresh_calls.append(request)
            return ExternalProcessResult(
                returncode=0,
                stdout=json.dumps(refresh_export, ensure_ascii=False).encode("utf-8"),
                stderr=b"",
            )

        refresh_request = ExternalSensorProcessRequest(
            request_id="request:self-check-workspace-refresh",
            command=("self-check-workspace-refresh", "--json"),
            timeout_seconds=5,
        )
        refresh = run_explicit_workspace_structural_refresh(
            refresh_observation,
            refresh_request,
            adapter=InfigraphProcessAdapter(runner=refresh_runner),
        )
        workspace_structural_refresh_executed = (
            refresh_calls == [refresh_request]
            and refresh.changed_paths == ("src/main.py",)
            and refresh.review.expected_workspace_hash == refresh_batch.workspace_snapshot_hash
            and refresh.review.observation.workspace_hash == refresh_batch.workspace_snapshot_hash
            and refresh.review.policy_gate.status == "PASS"
            and refresh.stages == (
                "WORKSPACE_OBSERVED",
                "INVALIDATION_BOUND",
                "PROCESS_ACQUIRED",
                "OBSERVATION_IMPORTED",
                "POLICY_EVALUATED",
                "POLICY_GATED",
            )
            and refresh.automatic is False
            and refresh.canonical_mutation is False
            and refresh.filesystem_mutation is False
        )
        workspace_structural_refresh_report = {
            "passed": workspace_structural_refresh_executed,
            "stage_count": len(refresh.stages),
            "gate_status": refresh.review.policy_gate.status,
            "changed_paths": list(refresh.changed_paths),
            "automatic": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }
    except (TypeError, ValueError) as exc:
        workspace_structural_refresh_report = {
            "passed": False,
            "automatic": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
            "error": str(exc),
        }
    runtime_operation_executed = False
    runtime_operation_report: dict[str, object]
    try:
        operation_request = RuntimeRequest(
            request_id="request:self-check-runtime-operation",
            model="self-check-model",
            messages=({"role": "user", "content": "self-check"},),
        )
        operation_response = RuntimeResponse(
            request_id=operation_request.request_id,
            provider="lmstudio",
            model=operation_request.model,
            text="self-check response",
            usage={"completion_tokens": 1},
            finish_reason="stop",
        )
        operation_calls: list[RuntimeRequest] = []

        def operation_complete(request: RuntimeRequest) -> RuntimeResponse:
            operation_calls.append(request)
            return operation_response

        operation = run_explicit_runtime_operation(
            operation_request,
            provider="lmstudio",
            complete=operation_complete,
            dispatch_approved=True,
            endpoint="provider://lmstudio",
            clock=lambda: "2026-08-30T00:00:00+00:00",
        )
        operation_record = operation.to_record()
        runtime_operation_executed = (
            operation_calls == [operation_request]
            and operation.succeeded
            and operation.stages == (
                "REQUEST_VALIDATED",
                "DISPATCH_APPROVED",
                "PROVIDER_INVOKED",
                "RESPONSE_NORMALIZED",
            )
            and operation.observation.status == "SUCCEEDED"
            and operation.response_hash is not None
            and operation_record["response_metadata"]["text_length"] == len("self-check response")
            and "self-check response" not in str(operation_record)
            and operation.automatic is False
            and operation.retry_count == 0
            and operation.fallback_used is False
            and operation.filesystem_mutation is False
            and operation.canonical_mutation is False
        )
        runtime_operation_report = {
            "passed": runtime_operation_executed,
            "stage_count": len(operation.stages),
            "status": operation.status,
            "automatic": False,
            "retry_count": 0,
            "fallback_used": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
        }
    except (TypeError, ValueError) as exc:
        runtime_operation_report = {
            "passed": False,
            "automatic": False,
            "retry_count": 0,
            "fallback_used": False,
            "canonical_mutation": False,
            "filesystem_mutation": False,
            "error": str(exc),
        }
    checks_passed = (
        int(registry_is_valid)
        + int(reactive_gate_is_valid)
        + int(relink_gate_is_valid)
        + int(spec_is_valid)
        + int(projections_are_present)
        + int(preset_catalog_executed)
        + int(spec_drift_executed)
        + int(guided_session_snapshot_executed)
        + int(structural_intake_executed)
        + int(bounded_context_executed)
        + int(structural_policy_executed)
        + int(structural_policy_gate_executed)
        + int(structural_reactive_executed)
        + int(infigraph_adapter_executed)
        + int(structural_export_review_executed)
        + int(structural_export_report_executed)
        + int(structural_export_report_inspection_executed)
        + int(verified_report_context_executed)
        + int(structural_executor_handoff_executed)
        + int(structural_runtime_plan_executed)
        + int(structural_provider_plan_executed)
        + int(structural_provider_dispatch_executed)
        + int(infigraph_process_executed)
        + int(structural_review_executed)
        + int(workspace_structural_refresh_executed)
        + int(runtime_operation_executed)
    )
    return {
        "status": "passed" if checks_passed == 26 else "failed",
        "phase": current_phase,
        "canonical_spec": str(spec_path),
        "checks_passed": checks_passed,
        "checks_total": 26,
        "preset_catalog": preset_catalog_report,
        "spec_drift": spec_drift_report,
        "guided_session_snapshot": guided_session_snapshot_report,
        "structural_intake": structural_intake_report,
        "bounded_structural_context": bounded_context_report,
        "structural_policy": structural_policy_report,
        "structural_policy_gate": structural_policy_gate_report,
        "structural_reactive": structural_reactive_report,
        "infigraph_adapter": infigraph_adapter_report,
        "structural_export_review": structural_export_review_report,
        "structural_export_report": structural_export_report_result,
        "structural_export_report_inspection": structural_export_report_inspection_result,
        "verified_report_context": verified_report_context_result,
        "structural_executor_handoff": structural_executor_handoff_result,
        "structural_runtime_plan": structural_runtime_plan_result,
        "structural_provider_plan": structural_provider_plan_result,
        "structural_provider_dispatch": structural_provider_dispatch_result,
        "infigraph_process": infigraph_process_report,
        "structural_review": structural_review_report,
        "workspace_structural_refresh": workspace_structural_refresh_report,
        "runtime_operation": runtime_operation_report,
        "spec_consistency": (
            spec_consistency.as_dict() if spec_consistency is not None else None
        ),
    }

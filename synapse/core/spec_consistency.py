"""Deterministic consistency checks for the repository's specification authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PHASE_AUTHORITY = "spec/phases.yaml"
README_PHASE_AUTHORITY_MARKER = (
    "`phases.yaml` is the single owner of `current_phase` and gate status; "
    "this README is a human-readable projection."
)
ROOT_README_PHASE_AUTHORITY_MARKER = (
    "현재 phase·gate·scope의 단일 기준은 [`spec/phases.yaml`](spec/phases.yaml)입니다."
)


@dataclass(frozen=True)
class SpecConsistencyReport:
    """The deterministic result of one specification consistency evaluation."""

    passed: bool
    current_phase: int | None
    current_status: str | None
    phase_authority: str | None
    phase_owner_files: tuple[str, ...]
    checked_files: tuple[str, ...]
    errors: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "current_phase": self.current_phase,
            "current_status": self.current_status,
            "phase_authority": self.phase_authority,
            "phase_owner_files": self.phase_owner_files,
            "checked_files": self.checked_files,
            "errors": self.errors,
        }


class SpecConsistencyError(ValueError):
    """Raised when the specification contains contradictory phase ownership."""

    def __init__(self, report: SpecConsistencyReport) -> None:
        self.report = report
        details = "; ".join(report.errors) or "unknown consistency error"
        super().__init__(f"Spec consistency gate failed: {details}")


class SpecConsistencyGate:
    """Check that phase state has exactly one machine-readable owner."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
        self.spec_dir = self.root / "spec"

    def evaluate(self) -> SpecConsistencyReport:
        errors: list[str] = []
        checked_files: set[str] = set()

        architecture = self._load_yaml(Path("spec/architecture.yaml"), errors, checked_files)
        phases = self._load_yaml(Path("spec/phases.yaml"), errors, checked_files)
        readme_path = self.root / "spec" / "README.md"
        readme = ""
        if not readme_path.is_file():
            errors.append("spec/README.md is missing")
        else:
            try:
                readme = readme_path.read_text(encoding="utf-8")
                checked_files.add("spec/README.md")
            except OSError as exc:
                errors.append(f"spec/README.md cannot be read: {exc}")

        root_readme_path = self.root / "README.md"
        if root_readme_path.is_file():
            try:
                root_readme = root_readme_path.read_text(encoding="utf-8")
                checked_files.add("README.md")
                if ROOT_README_PHASE_AUTHORITY_MARKER not in root_readme:
                    errors.append(
                        "README.md must identify spec/phases.yaml as the phase authority"
                    )
            except OSError as exc:
                errors.append(f"README.md cannot be read: {exc}")

        phase_authority: str | None = None
        if isinstance(architecture, Mapping):
            if architecture.get("authority") != "spec/":
                errors.append("spec/architecture.yaml must declare authority: spec/")
            phase_authority = architecture.get("phase_authority")
            if phase_authority != PHASE_AUTHORITY:
                errors.append(
                    "spec/architecture.yaml must point phase_authority to spec/phases.yaml"
                )
            if "current_phase" in architecture:
                errors.append(
                    "spec/architecture.yaml must not own current_phase; use spec/phases.yaml"
                )
        else:
            errors.append("spec/architecture.yaml must contain a YAML mapping")

        phase_owner_files = self._find_phase_owner_files(errors, checked_files)
        if phase_owner_files != (PHASE_AUTHORITY,):
            errors.append(
                "spec/phases.yaml must be the only spec file that declares top-level current_phase"
            )

        current_phase: int | None = None
        current_status: str | None = None
        if isinstance(phases, Mapping):
            raw_current_phase = phases.get("current_phase")
            if isinstance(raw_current_phase, int) and not isinstance(raw_current_phase, bool):
                current_phase = raw_current_phase
            else:
                errors.append("spec/phases.yaml current_phase must be an integer")

            entries = phases.get("phases")
            if not isinstance(entries, list):
                errors.append("spec/phases.yaml phases must be a list")
            else:
                phase_numbers: list[int] = []
                for entry in entries:
                    if not isinstance(entry, Mapping):
                        errors.append("every phases.yaml phase entry must be a mapping")
                        continue
                    number = entry.get("number")
                    if isinstance(number, int) and not isinstance(number, bool):
                        phase_numbers.append(number)
                    else:
                        errors.append("every phases.yaml phase entry must have an integer number")
                if len(phase_numbers) != len(set(phase_numbers)):
                    errors.append("spec/phases.yaml phase numbers must be unique")
                if current_phase is not None:
                    current_entries = [
                        entry
                        for entry in entries
                        if isinstance(entry, Mapping) and entry.get("number") == current_phase
                    ]
                    if len(current_entries) != 1:
                        errors.append(
                            "spec/phases.yaml current_phase must identify exactly one phase entry"
                        )
                    else:
                        raw_status = current_entries[0].get("status")
                        if isinstance(raw_status, str):
                            current_status = raw_status
        else:
            errors.append("spec/phases.yaml must contain a YAML mapping")

        if README_PHASE_AUTHORITY_MARKER not in readme:
            errors.append(
                "spec/README.md must identify phases.yaml as the single phase authority"
            )

        report = SpecConsistencyReport(
            passed=not errors,
            current_phase=current_phase,
            current_status=current_status,
            phase_authority=phase_authority,
            phase_owner_files=phase_owner_files,
            checked_files=tuple(sorted(checked_files)),
            errors=tuple(errors),
        )
        return report

    def assert_valid(self) -> SpecConsistencyReport:
        report = self.evaluate()
        if not report.passed:
            raise SpecConsistencyError(report)
        return report

    def _load_yaml(
        self,
        relative_path: Path,
        errors: list[str],
        checked_files: set[str],
    ) -> Any:
        path = self.root / relative_path
        relative_name = relative_path.as_posix()
        if not path.is_file():
            errors.append(f"{relative_name} is missing")
            return None
        checked_files.add(relative_name)
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"{relative_name} cannot be parsed: {exc}")
            return None

    def _find_phase_owner_files(
        self,
        errors: list[str],
        checked_files: set[str],
    ) -> tuple[str, ...]:
        if not self.spec_dir.is_dir():
            errors.append("spec/ directory is missing")
            return ()

        owners: list[str] = []
        yaml_paths = sorted(self.spec_dir.rglob("*.yaml")) + sorted(self.spec_dir.rglob("*.yml"))
        for path in yaml_paths:
            relative_name = path.relative_to(self.root).as_posix()
            checked_files.add(relative_name)
            try:
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
            except (OSError, yaml.YAMLError) as exc:
                errors.append(f"{relative_name} cannot be parsed: {exc}")
                continue
            if isinstance(document, Mapping) and "current_phase" in document:
                owners.append(relative_name)
        return tuple(sorted(owners))


def assert_spec_consistency(root: Path | None = None) -> SpecConsistencyReport:
    """Run the repository-level Spec Consistency Gate and raise on drift."""

    return SpecConsistencyGate(root).assert_valid()

"""Read-only developer-log snapshots for creative stage diagnostics."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def read_log_snapshot(path: str | Path, *, tail_bytes: int = 4096) -> dict[str, Any]:
    log_path = Path(path)
    if tail_bytes < 1:
        raise ValueError("tail_bytes must be positive")
    try:
        stat = log_path.stat()
        with log_path.open("rb") as handle:
            handle.seek(max(0, stat.st_size - tail_bytes))
            tail = handle.read(tail_bytes)
    except OSError as exc:
        return {"path": str(log_path), "exists": False, "size": None, "modified_ns": None, "tail_sha256": None, "error": str(exc)[:240]}
    return {
        "path": str(log_path),
        "exists": True,
        "size": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
        "tail_sha256": hashlib.sha256(tail).hexdigest(),
        "error": None,
    }


def log_snapshot_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    if before.get("exists") is not True or after.get("exists") is not True:
        return False
    return (before.get("size"), before.get("modified_ns"), before.get("tail_sha256")) != (after.get("size"), after.get("modified_ns"), after.get("tail_sha256"))


def build_creative_observer_snapshot(
    log_snapshot: Mapping[str, Any], receipt: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Add bounded lifecycle diagnostics to a read-only log snapshot.

    The log snapshot remains the source observation; receipt fields are copied
    as diagnostics only.  No receipt is revalidated, persisted, or promoted.
    Missing fields stay ``None`` so an observer never guesses provider state.
    """

    if not isinstance(log_snapshot, Mapping):
        raise TypeError("log_snapshot must be a mapping")
    snapshot = dict(log_snapshot)
    source = receipt if isinstance(receipt, Mapping) else {}
    sequence = source.get("sequence_receipt") if isinstance(source.get("sequence_receipt"), Mapping) else source
    reactive_mode = sequence.get("reactive_mode")
    failed_stage = sequence.get("failed_stage")
    blocked_zone = failed_stage if isinstance(failed_stage, str) and failed_stage.startswith("zone:") else None
    synthesis = source.get("synthesis") if isinstance(source.get("synthesis"), Mapping) else {}
    quality = synthesis.get("quality") if isinstance(synthesis.get("quality"), Mapping) else {}
    evidence = quality.get("evidence") if isinstance(quality.get("evidence"), Mapping) else {}
    versions = synthesis.get("reactive_contract_versions", evidence.get("reactive_contract_versions"))
    if not isinstance(versions, Mapping):
        versions = None
    hashes = synthesis.get("reactive_contract_hashes", source.get("reactive_contract_hashes"))
    if not isinstance(hashes, Mapping):
        hashes = None
    snapshot["creative_diagnostics"] = {
        "reactive_mode": reactive_mode if reactive_mode in {"LEGACY", "DETAILED"} else None,
        "blocked_zone": blocked_zone,
        "contract_versions": dict(versions) if versions is not None else None,
        "contract_hashes": dict(hashes) if hashes is not None else None,
        "receipt_schema_version": sequence.get("schema_version"),
    }
    return snapshot


__all__ = ["build_creative_observer_snapshot", "log_snapshot_changed", "read_log_snapshot"]

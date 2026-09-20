"""A thin local launcher for the canonical Synapse core.

This folder is a convenience surface, not a second Synapse implementation.
It imports the planner and substantive-content verifier from the canonical
repository and never calls a model or mutates Canonical State.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

SOURCE_ROOT_ENV = "SYNAPSE_SOURCE_ROOT"


def _source_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    configured = os.environ.get(SOURCE_ROOT_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    script = Path(__file__).resolve()
    # Support the intended in-repository layout (Synapse/_mini) and the
    # earlier sibling layout (Synapse_mini) without duplicating the core.
    candidates = (script.parents[1], script.parents[1].parent / "Synapse")
    for candidate in candidates:
        if (candidate / "synapse").is_dir():
            return candidate.resolve()
    return candidates[0].resolve()


def _describe_source(root: Path) -> str:
    """Name the core without disclosing where this copy happens to sit."""

    return "bundled" if (Path(__file__).resolve().parents[1] == root) else str(root)


def _core(source_root: str | None):
    root = _source_root(source_root)
    if not (root / "synapse").is_dir():
        raise FileNotFoundError(
            "canonical Synapse source was not found; pass --source-root or set "
            + SOURCE_ROOT_ENV
        )
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from synapse.core.scaffold import ScaffoldError, build_scaffold_plan
    from synapse.core.workspace import has_substantive_workspace_content

    return root, ScaffoldError, build_scaffold_plan, has_substantive_workspace_content


def _read_idea(value: str) -> str:
    candidate = Path(value).expanduser()
    try:
        if not candidate.is_file():
            return value
        # utf-8-sig, not utf-8: a file saved by Notepad begins with a byte order
        # mark, and reading it as plain utf-8 leaves that mark inside the first
        # line, where it becomes the first character of the project name.
        return candidate.read_text(encoding="utf-8-sig")
    except OSError:
        return value
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{candidate} is not UTF-8 text. Save it again as UTF-8 and re-run."
        ) from exc


def _build_plan(args: argparse.Namespace):
    root, scaffold_error, build_scaffold_plan, _ = _core(args.source_root)
    idea = _read_idea(args.idea)
    plan = build_scaffold_plan(
        idea,
        preset_id=args.preset,
        project_name=args.project_name,
    )
    return root, plan, scaffold_error


def _evaluate(plan: Any, contents: dict[str, Any], verifier: Any) -> dict[str, Any]:
    rows = []
    for item in plan.files:
        value = contents.get(item.path)
        if not isinstance(value, str):
            rows.append(
                {
                    "path": item.path,
                    "required": item.required,
                    "status": "MISSING",
                    "utf8_bytes": 0,
                }
            )
            continue
        passed = verifier(value, scaffold_purpose=item.purpose)
        rows.append(
            {
                "path": item.path,
                "required": item.required,
                "status": "PASS" if passed else "EMPTY_OR_TEMPLATE",
                "utf8_bytes": len(value.encode("utf-8")),
            }
        )
    required = [row for row in rows if row["required"]]
    passed = [row for row in required if row["status"] == "PASS"]
    return {
        "schema_version": "synapse.mini.content-check.v1",
        "plan_id": plan.id,
        "project_name": plan.project_name,
        "required_files": len(required),
        "passed_files": len(passed),
        "status": "READY" if len(passed) == len(required) else "BLOCKED",
        "files": rows,
    }


def _emit(record: dict[str, Any], output: str | None) -> int:
    rendered = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True)
    if output:
        destination = Path(output).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if record.get("status") in {"READY", "PLAN_ONLY"} else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synapse mini: canonical idea planning and read-only content checks"
    )
    parser.add_argument(
        "--source-root",
        help="path to the canonical Synapse repository (default: sibling Synapse)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="build a deterministic plan without writes")
    plan_parser.add_argument("idea", help="idea text or a UTF-8 text file")
    plan_parser.add_argument("--preset", default="general")
    plan_parser.add_argument("--project-name", default="")
    plan_parser.add_argument("--out", default=None)

    check_parser = subparsers.add_parser(
        "check", help="read an existing workspace and check substantive required files"
    )
    check_parser.add_argument("workspace", type=Path)
    check_parser.add_argument("--idea", required=True, help="idea text or a UTF-8 text file")
    check_parser.add_argument("--preset", default="general")
    check_parser.add_argument("--project-name", default="")
    check_parser.add_argument("--out", default=None)

    demo_parser = subparsers.add_parser(
        "demo", help="check a JSON path-to-content proposal without writing it"
    )
    demo_parser.add_argument("--idea", required=True, help="idea text or a UTF-8 text file")
    demo_parser.add_argument("--content-json", required=True, type=Path)
    demo_parser.add_argument("--preset", default="general")
    demo_parser.add_argument("--project-name", default="")
    demo_parser.add_argument("--out", default=None)

    args = parser.parse_args()
    try:
        root, plan, _ = _build_plan(args)
        if args.command == "plan":
            return _emit(
                {
                    "schema_version": "synapse.mini.plan.v1",
                    "status": "PLAN_ONLY",
                    "canonical_source": _describe_source(root),
                    "plan": plan.to_record(),
                },
                args.out,
            )

        _, _, _, verifier = _core(args.source_root)
        if args.command == "check":
            workspace = args.workspace.expanduser().resolve()
            contents = {}
            for item in plan.files:
                path = workspace / item.path
                if path.is_file():
                    try:
                        contents[item.path] = path.read_text(encoding="utf-8-sig")
                    except UnicodeDecodeError as exc:
                        # Name the file. A workspace this command did not write
                        # can hold anything, and a bare codec error says only
                        # that some byte somewhere was wrong.
                        raise ValueError(f"{item.path} is not UTF-8 text") from exc
            result = _evaluate(plan, contents, verifier)
        else:
            payload = json.loads(
                args.content_json.expanduser().read_text(encoding="utf-8-sig")
            )
            if not isinstance(payload, dict):
                raise ValueError("content JSON must be an object keyed by relative path")
            result = _evaluate(plan, payload, verifier)
        result["canonical_source"] = _describe_source(root)
        return _emit(result, args.out)
    except (ImportError, KeyError, OSError, TypeError, ValueError) as exc:
        print("synapse-mini: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


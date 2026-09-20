"""Read-only loader for product/game blueprint catalogs."""

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BLUEPRINT_ROOT = ROOT / "domain_packs" / "_presets" / "blueprints"


def load_blueprint_catalog(profile_id: str) -> dict[str, Any]:
    normalized = profile_id.strip().lower()
    if normalized not in {"product", "game"}:
        raise KeyError(f"blueprint catalog unavailable: {profile_id}")
    path = BLUEPRINT_ROOT / f"{normalized}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    zones = payload.get("zones")
    files = payload.get("files")
    if payload.get("profile_id") != normalized or payload.get("status") != "BLUEPRINT_ONLY":
        raise ValueError("invalid blueprint catalog identity")
    if not isinstance(zones, list) or len(zones) != 9:
        raise ValueError("blueprint catalog must contain nine zones")
    if not isinstance(files, list) or len(files) != 9 or len({item.get("path") for item in files}) != 9:
        raise ValueError("blueprint catalog must contain nine unique starter files")
    ids = [zone.get("id") for zone in zones]
    if len(set(ids)) != 9 or any(
        not zone.get("artifact")
        or not zone.get("acceptance")
        or not isinstance(zone.get("focus_questions"), list)
        or len(zone["focus_questions"]) < 3
        or any(not isinstance(question, str) or not question.strip() for question in zone["focus_questions"])
        or not isinstance(zone.get("required_fields"), list)
        or len(zone["required_fields"]) < 4
        or len(set(zone["required_fields"])) != len(zone["required_fields"])
        for zone in zones
    ):
        raise ValueError("blueprint catalog zones must have unique IDs and acceptance criteria")
    if {item.get("zone") for item in files} != set(ids):
        raise ValueError("starter files must cover every blueprint zone")
    return payload


def build_blueprint_scaffold_plan(profile_id: str) -> tuple[dict[str, object], ...]:
    """Return proposed starter files without creating them."""

    payload = load_blueprint_catalog(profile_id)
    zones = {zone["id"]: zone for zone in payload["zones"]}
    ordered = [zone["id"] for zone in payload["zones"]]
    return tuple(
        {
            "path": item["path"],
            "zone": item["zone"],
            "artifact": zones[item["zone"]]["artifact"],
            "status": "PROPOSED",
            "purpose": zones[item["zone"]]["acceptance"],
            "focus_questions": tuple(zones[item["zone"]]["focus_questions"]),
            "required_fields": tuple(zones[item["zone"]]["required_fields"]),
            "validation": {
                "normal": "기본 입력에서 산출물이 생성되고 추적됨",
                "boundary": "빈 값·최대 범위·권한 경계가 명시적으로 처리됨",
                "failure": "실패 원인과 복구 또는 중단 조건이 기록됨",
            },
            "depends_on": ordered[: ordered.index(item["zone"])],
            "template": "\n".join(
                [
                    f"# {zones[item['zone']]['artifact']}",
                    "",
                    "## Required fields",
                    *[f"- **{field}**: " for field in zones[item["zone"]]["required_fields"]],
                    "",
                    "## Focus questions",
                    *[f"- {question}" for question in zones[item["zone"]]["focus_questions"]],
                    "",
                    "## Validation notes",
                    "- Normal: 기본 입력과 기대 결과를 기록합니다.",
                    "- Boundary: 빈 값·최대 범위·권한 경계를 기록합니다.",
                    "- Failure: 실패 원인과 복구 또는 중단 조건을 기록합니다.",
                ]
            ),
        }
        for item in payload["files"]
    )


__all__ = ["build_blueprint_scaffold_plan", "load_blueprint_catalog"]

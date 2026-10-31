"""Strict validation for the model's part of the report, and assembly of report v1."""
import json
import re

from .config import OBSERVATION_CATEGORIES

CATEGORIES = set(OBSERVATION_CATEGORIES) | {"other"}
VISIBILITY = {"visible", "not_visible", "unclear", "not_assessable"}
EVIDENCE = {"visual", "user_report", "both"}
MODEL_KEYS = {"image_description", "visual_findings", "identified_issues", "recommended_actions"}
REPORT_NOTICE = "AI-assisted observation; not an official inspection or agency submission."
MAX_TEXT = 1000
MAX_ITEMS = 12


class ReportValidationError(ValueError):
    pass


def _text(v, where: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ReportValidationError(f"{where}: expected non-empty string")
    v = v.strip()
    if len(v) > MAX_TEXT:
        raise ReportValidationError(f"{where}: too long")
    return v


def _list(v, where: str, allow_empty=True) -> list:
    if not isinstance(v, list) or len(v) > MAX_ITEMS or (not allow_empty and not v):
        raise ReportValidationError(f"{where}: expected list")
    return v


def extract_json(raw: str) -> dict:
    """Parse the model's text as a JSON object, tolerating a ```json fence."""
    text = (raw or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.S)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ReportValidationError("not valid JSON") from exc
    if not isinstance(data, dict):
        raise ReportValidationError("top level must be an object")
    return data


def validate_model_output(data: dict) -> dict:
    extra = set(data) - MODEL_KEYS
    missing = MODEL_KEYS - set(data)
    if missing or extra:
        raise ReportValidationError(f"keys mismatch: missing={sorted(missing)} extra={sorted(extra)}")

    findings = []
    for i, f in enumerate(_list(data["visual_findings"], "visual_findings")):
        if not isinstance(f, dict) or set(f) != {"category", "visibility", "description"}:
            raise ReportValidationError(f"visual_findings[{i}]: bad shape")
        if f["category"] not in CATEGORIES or f["visibility"] not in VISIBILITY:
            raise ReportValidationError(f"visual_findings[{i}]: bad enum")
        findings.append({"category": f["category"], "visibility": f["visibility"],
                         "description": _text(f["description"], f"visual_findings[{i}].description")})

    issues = []
    for i, it in enumerate(_list(data["identified_issues"], "identified_issues")):
        if not isinstance(it, dict) or set(it) != {"category", "description", "evidence_source", "requires_human_review"}:
            raise ReportValidationError(f"identified_issues[{i}]: bad shape")
        if it["category"] not in CATEGORIES or it["evidence_source"] not in EVIDENCE:
            raise ReportValidationError(f"identified_issues[{i}]: bad enum")
        if not isinstance(it["requires_human_review"], bool):
            raise ReportValidationError(f"identified_issues[{i}]: requires_human_review must be boolean")
        issues.append({"category": it["category"],
                       "description": _text(it["description"], f"identified_issues[{i}].description"),
                       "evidence_source": it["evidence_source"],
                       "requires_human_review": it["requires_human_review"]})

    actions = [_text(a, f"recommended_actions[{i}]")
               for i, a in enumerate(_list(data["recommended_actions"], "recommended_actions"))]

    return {
        "image_description": _text(data["image_description"], "image_description"),
        "visual_findings": findings,
        "identified_issues": issues,
        "recommended_actions": actions,
    }


def assemble_report(model_part: dict, *, reference_id: str, submitted_at: str, location: dict,
                    observations: list[str], model: str, organization: dict | None = None) -> dict:
    """Server-authoritative fields come from the stored record, never from the model."""
    org = organization or {"name": None, "contact": None, "source_url": None, "verified_on": None}
    return {
        "schema_version": "1",
        "reference_id": reference_id,
        "submitted_at": submitted_at,
        "location": location,
        "image_description": model_part["image_description"],
        "visual_findings": model_part["visual_findings"],
        "identified_issues": model_part["identified_issues"],
        "user_observations": list(observations),
        "recommended_actions": model_part["recommended_actions"],
        "responsible_organization": org,
        "model": model,
        "report_notice": REPORT_NOTICE,
    }

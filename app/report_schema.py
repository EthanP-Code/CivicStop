"""Strict schemas for untrusted model output and the stored report."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import OBSERVATION_CATEGORIES

Category = Literal[
    "trees_vegetation",
    "missing_shelter",
    "missing_seating",
    "trash_cans",
    "bus_info_posters",
    "signage",
]
Visibility = Literal["visible", "not_visible", "unclear", "not_assessable"]
EvidenceSource = Literal["visual", "user_report", "both"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VisualFinding(StrictModel):
    category: Category
    visibility: Visibility
    description: str = Field(min_length=1, max_length=500)


class IdentifiedIssue(StrictModel):
    category: Category
    evidence_source: EvidenceSource
    description: str = Field(min_length=1, max_length=500)
    requires_human_review: bool


class ModelAnalysis(StrictModel):
    image_description: str = Field(min_length=1, max_length=1200)
    visual_findings: list[VisualFinding] = Field(max_length=len(OBSERVATION_CATEGORIES))
    identified_issues: list[IdentifiedIssue] = Field(max_length=len(OBSERVATION_CATEGORIES))
    recommended_actions: list[str] = Field(max_length=6)


class ResponsibleOrganization(StrictModel):
    name: str | None = None
    contact: str | None = None
    source_url: str | None = None
    verified_on: str | None = None


class StoredReport(StrictModel):
    schema_version: Literal["1"] = "1"
    report_notice: str
    reference_id: str
    submitted_at: str
    location: dict
    user_observations: list[Category]
    image_description: str
    visual_findings: list[VisualFinding]
    identified_issues: list[IdentifiedIssue]
    recommended_actions: list[str]
    responsible_organization: ResponsibleOrganization
    model: str

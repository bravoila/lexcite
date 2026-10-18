"""Structured contracts for research and independent evidence review."""
from typing import Literal
from pydantic import BaseModel, Field, field_validator
from ..law_names import clean_title


class Evidence(BaseModel):
    article_id: str
    quote: str = Field(min_length=8, max_length=8000)

class Claim(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    kind: Literal["rule", "inference"]
    section: Literal["answer", "rule", "application", "limitation"] = "rule"
    evidence: list[Evidence] = Field(min_length=1, max_length=5)

class AuthorityNeed(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    articles: list[str] = Field(default_factory=list, max_length=8)
    reason: str = Field(default="", max_length=300)

    _clean_name = field_validator('name', mode='before')(clean_title)

class FactItem(BaseModel):
    topic: str = Field(min_length=2, max_length=160)
    text: str = Field(min_length=1, max_length=600)
    state: Literal["stated", "unknown", "conflicting"]
    source_id: str = Field(max_length=100)
    quote: str = Field(min_length=1, max_length=1000)
    question_id: str = Field(default="", max_length=100)
    question: str = Field(default="", max_length=350)
    supersedes: list[str] = Field(default_factory=list, max_length=5)

class FactExtraction(BaseModel):
    intent: Literal["new_question", "supplement", "correction", "followup"]
    research_question: str = Field(default="", max_length=800)
    facts: list[FactItem] = Field(default_factory=list, max_length=35)

class CoverageAudit(BaseModel):
    selected_ids: list[str] = Field(max_length=40)
    coverage: Literal["sufficient", "partial", "missing"]
    missing_authorities: list[AuthorityNeed] = Field(default_factory=list, max_length=6)
    reason: str = Field(max_length=600)

class SourceMetadata(BaseModel):
    is_primary_law: bool
    name: str = Field(max_length=200)
    title_quote: str = Field(max_length=600)
    document_number: str = Field(default="", max_length=100)
    effective_from: str = Field(max_length=10)
    effective_quote: str = Field(max_length=800)
    jurisdiction: str = Field(max_length=40)
    jurisdiction_quote: str = Field(max_length=600)
    kind: Literal["法律", "行政法规", "司法解释", "地方性法规", "部门规章"]
    contains_complete_text: bool
    temporal_note: str = Field(default="", max_length=600)

    _clean_name = field_validator('name', mode='before')(clean_title)

class Draft(BaseModel):
    coverage: Literal["sufficient", "partial", "missing"] | None = None
    claims: list[Claim] = Field(default_factory=list, max_length=8)
    missing_facts: list[str] = Field(default_factory=list, max_length=6)
    insufficient: bool = True
    missing_authorities: list[AuthorityNeed] = Field(default_factory=list, max_length=6)
    limitation: str = Field(default="", max_length=600)

class Plan(BaseModel):
    queries: list[str] = Field(default_factory=list, max_length=3)
    action: Literal["retrieve", "clarify"] = "retrieve"
    questions: list[str] = Field(default_factory=list, max_length=6)
    authorities: list[AuthorityNeed] = Field(default_factory=list, max_length=6)

class ClaimPatch(BaseModel):
    claim_index: int = Field(ge=0, le=7)
    claim: Claim | None

class Repair(BaseModel):
    patches: list[ClaimPatch] = Field(max_length=8)
    missing_facts: list[str] = Field(default_factory=list, max_length=3)

class Check(BaseModel):
    claim_index: int
    supported: bool
    reason: str = Field(max_length=400)
    action: Literal["rewrite", "retrieve"] = "rewrite"

class Verdict(BaseModel):
    checks: list[Check]

class ClarificationCheck(BaseModel):
    question_index: int = Field(ge=0)
    state: Literal["answered", "unknown", "missing", "conflicting"]
    source_id: str = Field(default="", max_length=100)
    quote: str = Field(default="", max_length=1200)

class ClarificationAudit(BaseModel):
    checks: list[ClarificationCheck] = Field(max_length=6)

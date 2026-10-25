"""Request/response models (A05) and LLM output schemas (LLM05).

Every field that reaches the LLM prompt or the database has a type, an allowlist or a
maximum length. Unknown fields are dropped (so a caller can't smuggle `expected`,
`id` or `route` into a case), and the LLM's JSON is validated with the same rigour
as a request body: the model is just another untrusted client.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Plan = Literal["Commercial", "Medicare Advantage", "Medicaid"]
Sex = Literal["F", "M", "U"]
ReviewAction = Literal["approve", "deny", "send_back"]
Role = Literal["submitter", "reviewer", "admin"]

_CTRL = {c: None for c in range(32) if c not in (9, 10, 13)}  # strip control chars except \t \n \r


def _clean(value: str) -> str:
    return value.translate(_CTRL).strip()


class CaseIn(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    requested_procedure: str = Field(min_length=2, max_length=120)
    condition: str = Field(min_length=2, max_length=200)
    plan: Plan = "Commercial"
    notes: str = Field(min_length=5, max_length=4000)
    age: int | None = Field(default=None, ge=0, le=120)
    sex: Sex = "U"

    @field_validator("requested_procedure", "condition", "notes")
    @classmethod
    def _no_control_chars(cls, v: str) -> str:
        return _clean(v)

    @field_validator("age", mode="before")
    @classmethod
    def _age_blank_is_none(cls, v):
        if v in ("", None, "—"):
            return None
        return v


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case: CaseIn


class ReviewRequest(BaseModel):
    """`send_back` re-runs the agent with reviewer notes; `approve`/`deny` finalize.

    `case` is only used for anonymous demo re-runs (nothing is stored). For a stored
    case the server loads the case by `id` under row-level security and ignores `case`.
    """
    model_config = ConfigDict(extra="forbid")
    action: ReviewAction
    id: str | None = Field(default=None, max_length=40, pattern=r"^[A-Z0-9-]+$")
    case: CaseIn | None = None
    notes: str = Field(default="", max_length=2000)
    attempt: int = Field(default=1, ge=1, le=10)

    @field_validator("notes")
    @classmethod
    def _clean_notes(cls, v: str) -> str:
        return _clean(v)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8, max_length=256)


class RoleChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Role


# ── LLM output (LLM05): the model's JSON is untrusted input ──────────────────
class ProposalOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    decision: Literal["approve", "deny"]
    rationale: str = Field(default="", max_length=1500)
    cited_criteria: list[str] = Field(default_factory=list, max_length=10)
    met_requirements: list[str] = Field(default_factory=list, max_length=15)
    unmet_requirements: list[str] = Field(default_factory=list, max_length=15)
    confidence: float = Field(ge=0.0, le=1.0)
    injection_detected: bool = False

    @field_validator("cited_criteria", "met_requirements", "unmet_requirements")
    @classmethod
    def _bounded_strings(cls, v: list) -> list[str]:
        return [str(x)[:300] for x in v]


class CritiqueOut(BaseModel):
    model_config = ConfigDict(extra="ignore")
    agree: bool
    confidence: float = Field(ge=0.0, le=1.0)
    gaps: list[str] = Field(default_factory=list, max_length=15)
    red_flag_detected: bool = False
    recommend_escalate: bool = False
    concern: str = Field(default="", max_length=600)
    injection_detected: bool = False

    @field_validator("gaps")
    @classmethod
    def _bounded_strings(cls, v: list) -> list[str]:
        return [str(x)[:300] for x in v]

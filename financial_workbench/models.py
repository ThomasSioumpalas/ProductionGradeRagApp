from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Settings(StrictModel):
    company: str = Field(min_length=1, max_length=200)
    language: Literal["en", "el"] = "en"
    latest_year: int = Field(ge=1900, le=2100)
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")
    scope: Literal["consolidated", "standalone"] = "consolidated"
    money_scale: Literal[1, 1000, 1000000] = 1000000
    share_scale: Literal[1, 1000, 1000000] = 1000000


class ExtractedFact(StrictModel):
    metric_id: str
    year: int
    scope: Literal["consolidated", "standalone"]
    currency: str
    # Raw number and explicit separator make conversion deterministic, not LLM arithmetic.
    raw_value: str
    decimal_separator: Literal[".", ","]
    scale: Literal[1, 1000, 1000000]
    source_file: str = Field(min_length=1, max_length=200)
    page: int
    quote: str
    context_quote: str


class Extraction(StrictModel):
    facts: list[ExtractedFact]


class Decision(StrictModel):
    metric_id: str
    year: int
    candidate_id: str | None = None
    manual_value: Decimal | None = None
    note: str = Field(default="", max_length=2000)


class Review(StrictModel):
    decisions: list[Decision] = Field(max_length=792)


class Question(StrictModel):
    language: Literal["en", "el"] = "en"
    question: str = Field(min_length=3, max_length=2000)

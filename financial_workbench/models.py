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
    raw_value: str = Field(min_length=1, max_length=60)
    decimal_separator: Literal[".", ","]
    scale: Literal[1, 1000, 1000000]
    source_file: str = Field(min_length=1, max_length=200)
    page: int
    quote: str = Field(min_length=6, max_length=240)
    context_quote: str = Field(min_length=1, max_length=180)


class Extraction(StrictModel):
    # A single chunk can contain many rows across comparative annual columns.
    # Keep enough room for 10 metrics x 3 years; oversized generations are
    # still handled by the extractor's adaptive metric-group splitting.
    facts: list[ExtractedFact] = Field(max_length=30)


class RowMapping(StrictModel):
    row_id: str
    metric_id: str


class RowMappings(StrictModel):
    mappings: list[RowMapping] = Field(max_length=16)


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

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from app.mediafile import MAX_BATCH_CLIPS
from app.transcript import AUTO, is_language

Output = Literal["video", "audio", "m4a"]
Quality = Literal["best", "1080", "720", "480"]
# A time in a video: a JSON number (never a string or a boolean), finite, 0 or more.
Seconds = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]


class InspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    output: Output
    quality: Quality = "best"


class SourceRequest(BaseModel):
    """Open a link in Clip Studio: download it at `quality` as a source to clip from."""

    model_config = ConfigDict(extra="forbid")

    url: str
    quality: Quality = "best"


class ClipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: Seconds
    end: Seconds


class BatchRequest(BaseModel):
    """Several ranges of one source, exported together as one ZIP."""

    model_config = ConfigDict(extra="forbid")

    clips: Annotated[list[ClipRequest], Field(min_length=1, max_length=MAX_BATCH_CLIPS)]


class AnalysisRequest(BaseModel):
    """What AI Highlights should look for in a source. The count is also capped by AI_MAX_CLIPS."""

    model_config = ConfigDict(extra="forbid")

    instruction: Annotated[str, Field(max_length=500)] = ""
    min_seconds: Annotated[float, Field(strict=True, ge=3, le=600, allow_inf_nan=False)] = 20.0
    max_seconds: Annotated[float, Field(strict=True, ge=5, le=900, allow_inf_nan=False)] = 90.0
    count: Annotated[int, Field(strict=True, ge=1, le=MAX_BATCH_CLIPS)] = 5
    min_score: Annotated[int, Field(strict=True, ge=0, le=100)] = 0
    language: str = AUTO
    retranscribe: StrictBool = False
    transcribe_only: StrictBool = False

    @field_validator("language")
    @classmethod
    def _language(cls, value: str) -> str:
        if value != AUTO and not is_language(value):
            raise ValueError("unknown language")
        return value

    @model_validator(mode="after")
    def _lengths(self) -> "AnalysisRequest":
        if self.max_seconds < self.min_seconds + 1:
            raise ValueError("max_seconds must be at least 1 more than min_seconds")
        return self

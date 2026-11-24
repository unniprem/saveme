from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

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

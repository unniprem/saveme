from typing import Literal

from pydantic import BaseModel, ConfigDict

Output = Literal["video", "audio"]
Quality = Literal["best", "1080", "720", "480"]


class InspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    output: Output
    quality: Quality = "best"

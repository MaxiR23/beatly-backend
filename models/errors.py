# INFO: Playback error report request model.

import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# U+2028 and U+2029 are category Zl/Zp, not Cc, but break lines just the same.
_LINE_SEPARATORS = {"\u2028", "\u2029"}


class PlaybackErrorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track_id: str = Field(min_length=1, max_length=64)
    platform: Literal["ios", "android"]
    os_version: str = Field(min_length=1, max_length=32)
    app_version: str = Field(min_length=1, max_length=32)
    stage: Literal["resolve", "playback"]
    error_code: str = Field(min_length=1, max_length=64)
    error_message: str = Field(min_length=1, max_length=1000)
    http_status: int | None = Field(default=None, ge=100, le=599)

    # The text is stored and logged: without control characters there is no
    # line injection in the logs or in the dashboard. Runs before the length
    # checks, so the limits apply to the cleaned value. A non-str value goes
    # through untouched so pydantic answers 422; raising here would be a 500.
    @field_validator(
        "track_id",
        "os_version",
        "app_version",
        "error_code",
        "error_message",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        cleaned = "".join(
            ch
            for ch in value
            if unicodedata.category(ch) != "Cc" and ch not in _LINE_SEPARATORS
        )
        return cleaned.strip()

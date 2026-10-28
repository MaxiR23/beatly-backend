# INFO: Image size tiers shared by every model that carries a provider image.

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, StringConstraints

# Leaf module on purpose: core/thumbnails.py imports it and the models import
# core/thumbnails.py. This file imports nothing from the repo, so there is no
# cycle. Keep it that way.


class ImageTiers(BaseModel):
    # Response only. The three URLs are derived in core/thumbnails.py and
    # never stored; color is read per request from public.image_colors
    # (ADR 021) and is null until it has been computed.
    small: str
    medium: str
    large: str
    color: str | None = None


class StoredImageColor(BaseModel):
    # A row of get_image_colors. The pattern is the same as the CHECK of the
    # table: a stored value that does not match is a failed read, not a
    # color sent to a client. failed_at (since migration 043) tells a key
    # being computed (color and failed_at null) from a recent failure.
    image_key: str
    color: Annotated[str, StringConstraints(pattern=r"^#[0-9a-f]{6}$")] | None
    attempted_at: datetime
    failed_at: datetime | None

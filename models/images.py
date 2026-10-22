# INFO: Image size tiers shared by every model that carries a provider image.

from pydantic import BaseModel

# Leaf module on purpose: core/thumbnails.py imports it and the models import
# core/thumbnails.py. This file imports nothing from the repo, so there is no
# cycle. Keep it that way.


class ImageTiers(BaseModel):
    # Response only. Derived in core/thumbnails.py, never stored.
    small: str
    medium: str
    large: str

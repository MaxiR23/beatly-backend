# INFO: Response models of the new releases (GET /releases).

from typing import Literal

from pydantic import BaseModel, computed_field

from core.thumbnails import image_tiers
from models.images import ImageTiers

# The only definition: models/ingest.py imports it for the request.
ReleaseType = Literal["album", "single", "ep"]


class NewReleaseArtist(BaseModel):
    id: str
    name: str


class NewRelease(BaseModel):
    album_id: str
    title: str
    type: ReleaseType
    artists: list[NewReleaseArtist]
    thumbnail_url: str

    # image is derived when the response is serialized, from thumbnail_url.
    # A computed field never goes in a request or a write model.
    @computed_field
    @property
    def image(self) -> ImageTiers | None:
        return image_tiers(self.thumbnail_url)

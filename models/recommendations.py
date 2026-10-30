# INFO: Personalized recommendations response models (ADR 025).

from typing import Literal

from pydantic import BaseModel, computed_field

from core.thumbnails import image_tiers
from models.album import AlbumRef
from models.artist import ArtistRef
from models.images import ImageTiers


class RecommendationSeed(BaseModel):
    # The artist or album the recommendation is similar to.
    type: Literal["artist", "album"]
    id: str
    name: str
    thumbnail_url: str | None = None

    @computed_field
    @property
    def image(self) -> ImageTiers | None:
        return image_tiers(self.thumbnail_url)


class RecommendationReason(BaseModel):
    # The field is named `reason` in the items, but it is not the `reason`
    # of an error envelope: it only says why the item is recommended.
    type: Literal["similar_to"] = "similar_to"
    seed: RecommendationSeed


class RecommendedArtist(ArtistRef):
    reason: RecommendationReason


class RecommendedAlbum(AlbumRef):
    reason: RecommendationReason


class Recommendations(BaseModel):
    # Two sections of a fixed cap, not a growing list: not paginated. No
    # stored set means two empty lists and both timestamps null.
    artists: list[RecommendedArtist]
    albums: list[RecommendedAlbum]
    generated_at: str | None = None
    valid_until: str | None = None


class StoredRecommendation(BaseModel):
    # A row of public.user_recommendations as the service reads it. No
    # computed fields: it comes from the database and never goes to a
    # client. A row that does not fit is a ValidationError, which ends as a
    # 502 and never as an empty list.
    item_type: Literal["artist", "album"]
    item_id: str
    rank: int
    seed_type: Literal["artist", "album"]
    seed_id: str
    seed_name: str
    seed_thumb: str | None = None
    payload: dict
    generated_at: str
    valid_until: str

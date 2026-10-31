# INFO: Personalized recommendations response models (ADR 025).

from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, computed_field, model_validator

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


# `type` uses the names and values of RecommendationSeed.type: the section
# items are the only mixed list.
class SeedSectionArtist(RecommendedArtist):
    type: Literal["artist"] = "artist"


class SeedSectionAlbum(RecommendedAlbum):
    type: Literal["album"] = "album"


SeedSectionItem = Annotated[
    SeedSectionArtist | SeedSectionAlbum, Field(discriminator="type")
]


class SeedSection(BaseModel):
    seed: RecommendationSeed
    items: list[SeedSectionItem]


class Recommendations(BaseModel):
    # Lists of fixed caps, not a growing list: not paginated. No stored set
    # means every list empty and both timestamps null. `artists` and `albums`
    # are deprecated (#227): every item of the set, flattened, kept until the
    # new home screen uses the other four lists; a later issue removes them.
    albums_for_you: list[RecommendedAlbum]
    seed_sections: list[SeedSection]
    recommended_albums: list[RecommendedAlbum]
    recommended_artists: list[RecommendedArtist]
    artists: list[RecommendedArtist]
    albums: list[RecommendedAlbum]
    generated_at: str | None = None
    valid_until: str | None = None


class StoredPlacement(BaseModel):
    # Where a stored row sits in the response, read from its payload (the
    # other keys, name and title, are ignored). A row that does not fit is a
    # ValidationError, which ends as a 502 and never as an empty list.
    section: Literal[
        "albums_for_you", "seed_sections", "recommended_albums", "recommended_artists"
    ]
    section_index: int | None = None

    @model_validator(mode="after")
    def _index_matches_section(self) -> Self:
        if self.section == "seed_sections":
            if self.section_index is None or not 0 <= self.section_index <= 2:
                raise ValueError("seed_sections needs a section_index from 0 to 2")
        elif self.section_index is not None:
            raise ValueError("only seed_sections has a section_index")
        return self


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

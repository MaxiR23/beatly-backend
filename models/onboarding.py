# INFO: Onboarding request/response models.

from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    computed_field,
    field_validator,
)

from core.thumbnails import image_tiers
from models.images import ImageTiers

MIN_ARTIST_IDS = 5
MAX_ARTIST_IDS = 20
MAX_EXCLUDE_IDS = 200

# The pattern of routes/artist.py, closed with the characters of a provider
# id (letters, digits, "_" and "-"). The ids go into the in_() filter of the
# curated table, which is split on commas and parentheses: an id with a
# quote, a comma, a parenthesis or a space would break the filter and come
# out as a 502, so it is a 422 here.
ArtistId = Annotated[str, StringConstraints(pattern=r"^(MPLA)?UC[A-Za-z0-9_-]*$")]


class CompleteOnboardingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artist_ids: list[ArtistId] = Field(
        min_length=MIN_ARTIST_IDS, max_length=MAX_ARTIST_IDS
    )

    @field_validator("artist_ids")
    @classmethod
    def _reject_repeated(cls, ids: list[str]) -> list[str]:
        if len(set(ids)) != len(ids):
            raise ValueError("artist_ids must not repeat")
        return ids


class SimilarArtistsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artist_id: ArtistId
    exclude_ids: list[ArtistId] = Field(
        default_factory=list, max_length=MAX_EXCLUDE_IDS
    )

    @field_validator("exclude_ids")
    @classmethod
    def _reject_repeated(cls, ids: list[str]) -> list[str]:
        if len(set(ids)) != len(ids):
            raise ValueError("exclude_ids must not repeat")
        return ids


class OnboardingArtist(BaseModel):
    id: str
    name: str
    thumbnail_url: str | None = None

    # image is derived when the response is serialized, from thumbnail_url.
    @computed_field
    @property
    def image(self) -> ImageTiers | None:
        return image_tiers(self.thumbnail_url)


class SimilarArtists(BaseModel):
    artists: list[OnboardingArtist]


class OnboardingCompletion(BaseModel):
    onboarding_completed_at: str
    # The valid artists left in the library, new or already there.
    saved: int
    # The ids the provider confirmed do not exist, in the order of the body.
    skipped: list[str]

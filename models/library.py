# INFO: Library item request/response models.

from typing import Literal

from pydantic import BaseModel, computed_field

from core.thumbnails import image_tiers, mosaic_tiers
from models.images import ImageTiers

LibraryItemKind = Literal["album", "playlist"]

# Mirrors library_items_source_check (017), so a value outside the CHECK is
# a 422 instead of a 502.
LibraryItemSource = Literal["genre", "replay", "presenting", "external"]


class LibraryItem(BaseModel):
    kind: LibraryItemKind
    external_id: str
    title: str
    thumbnail_url: str | None = None
    artist: str | None = None
    artist_id: str | None = None
    album_id: str | None = None
    album_name: str | None = None
    source: str
    added_at: str
    updated_at: str

    # image is derived when the response is serialized, from thumbnail_url.
    # A computed field never goes in a model that is a request or that is
    # dumped to the database.
    @computed_field
    @property
    def image(self) -> ImageTiers | None:
        return image_tiers(self.thumbnail_url)


class LibraryEntry(BaseModel):
    kind: LibraryItemKind
    id: str
    title: str
    thumbnail_url: str | None = None
    subtitle: str | None = None
    source: str
    # Always present, never null; [] when the entry has no mosaic (#160).
    thumbnail_urls: list[str]

    @computed_field
    @property
    def image(self) -> ImageTiers | None:
        return image_tiers(self.thumbnail_url)

    @computed_field
    @property
    def mosaic(self) -> list[ImageTiers]:
        return mosaic_tiers(self.thumbnail_urls)


# False covers both "never saved" and ids that are never saved (an own
# playlist, "liked"): no extra validation, decided on #187.
class LibraryItemSavedState(BaseModel):
    saved: bool


class AddLibraryItemRequest(BaseModel):
    kind: LibraryItemKind
    external_id: str
    title: str
    source: LibraryItemSource
    thumbnail_url: str | None = None
    artist: str | None = None
    artist_id: str | None = None
    album_id: str | None = None
    album_name: str | None = None

# INFO: Library item request/response models.

from typing import Literal

from pydantic import BaseModel

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


class LibraryEntry(BaseModel):
    kind: LibraryItemKind
    id: str
    title: str
    thumbnail_url: str | None = None
    subtitle: str | None = None
    source: str
    # Always present, never null; [] when the entry has no mosaic (#160).
    thumbnail_urls: list[str]


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

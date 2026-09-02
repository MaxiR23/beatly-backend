# INFO: Library item request/response models.

from typing import Literal

from pydantic import BaseModel

LibraryItemKind = Literal["album", "playlist"]


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


class AddLibraryItemRequest(BaseModel):
    kind: LibraryItemKind
    external_id: str
    title: str
    source: str
    thumbnail_url: str | None = None
    artist: str | None = None
    artist_id: str | None = None
    album_id: str | None = None
    album_name: str | None = None

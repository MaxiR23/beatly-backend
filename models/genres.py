# INFO: Genre response models.

from pydantic import BaseModel


class Genre(BaseModel):
    slug: str
    name: str
    description: str | None = None


class GenreList(BaseModel):
    genres: list[Genre]


class GenrePlaylist(BaseModel):
    id: str
    title: str
    description: str | None = None
    thumbnail_url: str | None = None
    track_count: int
    category: str | None = None


class GenrePlaylistList(BaseModel):
    playlists: list[GenrePlaylist]


class GenreCategoryList(BaseModel):
    categories: list[str]

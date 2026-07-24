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


class TrackArtist(BaseModel):
    id: str
    name: str


class GenrePlaylistTrack(BaseModel):
    track_id: str
    title: str
    artists: list[TrackArtist]
    album: str
    album_id: str
    duration_seconds: int
    thumbnail_url: str
    position: int


class GenrePlaylistTrackList(BaseModel):
    tracks: list[GenrePlaylistTrack]

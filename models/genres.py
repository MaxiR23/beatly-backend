# INFO: Genre response models.

from pydantic import BaseModel


class Genre(BaseModel):
    slug: str
    name: str
    description: str | None = None


class GenrePlaylist(BaseModel):
    id: str
    title: str
    description: str | None = None
    thumbnail_url: str | None = None
    track_count: int
    category: str | None = None


# Item of GET /genres/{slug}/playlists only. thumbnail_urls is required, no
# default, so get_genre_playlists() cannot forget it; get_genre_playlist()
# and the public share keep using GenrePlaylist (#160).
class GenrePlaylistListItem(GenrePlaylist):
    thumbnail_urls: list[str]


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

# INFO: Search request/response models.

from pydantic import BaseModel, Field


class SearchArtistRef(BaseModel):
    # Not models.likes.TrackArtist: this is a reference to an artist as it
    # appears inside a song or an album, and the external provider can
    # mention an artist without a link, which arrives with no id. TrackArtist
    # requires an id, so a shared model here would fail on that case instead
    # of surfacing it as an unmatched, still-present item.
    id: str | None = None
    name: str


class SearchArtist(BaseModel):
    id: str
    name: str


class SearchSong(BaseModel):
    track_id: str
    title: str
    artists: list[SearchArtistRef] = Field(min_length=1)
    album: str
    album_id: str
    duration_seconds: int
    thumbnail_url: str


class SearchAlbum(BaseModel):
    id: str
    playlist_id: str
    title: str
    artists: list[SearchArtistRef] = Field(min_length=1)
    year: str | None = None
    thumbnail_url: str | None = None


class SearchResult(BaseModel):
    artist: SearchArtist | None
    songs: list[SearchSong]
    albums: list[SearchAlbum]

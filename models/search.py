# INFO: Search request/response models.

from pydantic import BaseModel, Field

from models.artist_refs import AlbumArtistRef, TrackArtists


class SearchArtist(BaseModel):
    id: str
    name: str
    # Nullable like SearchAlbum.thumbnail_url; the default lets a SearchResult
    # cached before this field existed keep validating.
    thumbnail_url: str | None = None


class SearchSong(BaseModel):
    track_id: str
    title: str
    # The track artists rule (models/artist_refs.py) applies. Required, with
    # no default: pydantic does not validate defaults, so a default would
    # let a song without artists skip the rule.
    artists: TrackArtists
    album: str
    album_id: str
    duration_seconds: int
    thumbnail_url: str


class SearchAlbum(BaseModel):
    id: str
    title: str
    # Albums keep their own rule: the id is optional and an empty list is
    # valid. See AlbumArtistRef.
    artists: list[AlbumArtistRef] = Field(default_factory=list)
    year: str | None = None
    thumbnail_url: str | None = None


class SearchResult(BaseModel):
    artist: SearchArtist | None
    songs: list[SearchSong]
    albums: list[SearchAlbum]

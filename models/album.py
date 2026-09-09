# INFO: Album request/response models.

from pydantic import BaseModel, Field

from models.search import SearchArtistRef


class AlbumTrack(BaseModel):
    # track_id, duration_seconds: the external provider omits these for a
    # track that is not available (region-locked or taken down), which is a
    # documented branch of the provider, not a broken layout.
    track_id: str | None = None
    title: str
    artists: list[SearchArtistRef] = Field(default_factory=list)
    duration_seconds: int | None = None
    # is_available comes straight from the provider (isAvailable), never
    # derived from track_id being null: it is the field the client checks to
    # gray out a track, so it must mean exactly what it says.
    is_available: bool
    # track_number falls back to the track's position in the list when the
    # provider omits it (unavailable tracks), so the album's numbering never
    # has gaps.
    track_number: int


class AlbumRef(BaseModel):
    # The summarized album shown inside other_versions and
    # related_recommendations. No `type` or `is_explicit`: they are
    # conditional keys the provider does not guarantee, and no accepted
    # criteria asks for them.
    id: str
    title: str
    artists: list[SearchArtistRef] = Field(default_factory=list)
    year: str | None = None
    audio_playlist_id: str | None = None
    thumbnail_url: str | None = None


class Album(BaseModel):
    id: str
    title: str
    year: str | None = None
    # No min_length: an album without a strapline arrives with no artist
    # data at all, and that is a normal, successful response, not a
    # malformed one. models/search.py applies the same criterion.
    artists: list[SearchArtistRef] = Field(default_factory=list)
    track_count: int | None = None
    duration_seconds: int
    audio_playlist_id: str | None = None
    thumbnail_url: str | None = None
    tracks: list[AlbumTrack]
    other_versions: list[AlbumRef] = Field(default_factory=list)
    related_recommendations: list[AlbumRef] = Field(default_factory=list)

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
    # No min_length: the external provider does not list an artist for
    # every row (a compilation or a soundtrack), and that is a normal,
    # successful response, not a malformed one. models/album.py applies
    # the same criterion. The artists lists in models/likes.py and
    # models/playlists.py deliberately keep min_length=1: those are built
    # from our own Supabase rows, where an empty list is broken data, not
    # a provider that had nothing to say about that result.
    artists: list[SearchArtistRef] = Field(default_factory=list)
    album: str
    album_id: str
    duration_seconds: int
    thumbnail_url: str


class SearchAlbum(BaseModel):
    id: str
    playlist_id: str
    title: str
    artists: list[SearchArtistRef] = Field(default_factory=list)
    year: str | None = None
    thumbnail_url: str | None = None


class SearchResult(BaseModel):
    artist: SearchArtist | None
    songs: list[SearchSong]
    albums: list[SearchAlbum]

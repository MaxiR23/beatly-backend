# INFO: Public share response models.

from typing import Literal

from pydantic import BaseModel, Field

from models.album import AlbumTrack
from models.artist import AlbumRef, ArtistRelease, ArtistSong
from models.likes import TrackArtist
from models.search import SearchArtistRef


class PublicOwner(BaseModel):
    # An object, not a bare string: adding owner.name later (a display
    # name, if the product ever wants one) does not break a client, while
    # widening a string into an object would. owner_id is deliberately not
    # here at all -- profiles are not readable by third parties, so this
    # payload must never resolve to a person. Literal, not str, so a third
    # value is a construction error caught here, not a string the client
    # has to defend against.
    type: Literal["app", "user"]


class PublicAlbum(BaseModel):
    # A flat mapping of models.album.Album, minus audio_playlist_id,
    # other_versions and related_recommendations: a share card shows the
    # album itself, not its browse carousels or the internal id used to
    # fetch its tracks. Reuses AlbumTrack and SearchArtistRef as-is, so this
    # is a projection of services/album_service.py::get_album(), never a
    # second implementation of its mapping.
    id: str
    title: str
    year: str | None = None
    artists: list[SearchArtistRef] = Field(default_factory=list)
    track_count: int | None = None
    duration_seconds: int
    thumbnail_url: str | None = None
    tracks: list[AlbumTrack]


class PublicArtist(BaseModel):
    # A flat mapping of models.artist.Artist, minus `related`: the issue
    # asks for top songs, albums and EPs, and a share card does not need a
    # jumping-off point to other artists. singles travels whole, with its
    # `type`, per the repo owner's decision (R4): the app's own artist
    # endpoint (#104) already exposes singles unfiltered, and filtering
    # here would create a difference between the two with no accepted
    # criterion asking for it.
    id: str
    name: str
    thumbnail_url: str | None = None
    songs: list[ArtistSong] = Field(default_factory=list)
    albums: list[AlbumRef] = Field(default_factory=list)
    singles: list[ArtistRelease] = Field(default_factory=list)


class PublicTrack(BaseModel):
    # A flat mapping of models.track.TrackRef, the model /tracks/{id}/upnext
    # and /tracks/{id}/related already return; declared apart instead of
    # reusing TrackRef so the public payload does not grow just because a
    # model shared with two authenticated endpoints grows -- the same
    # containment PublicAlbum already applies by enumerating its own
    # top-level fields instead of reusing models.album.Album directly.
    #
    # artists uses SearchArtistRef, not TrackArtist, for the same reason
    # models/search.py:7-11 already gives: the provider can name an artist
    # with no link, and that item arrives with id: null. TrackArtist
    # requires id and would fail to validate instead of showing it.
    # Measured: the id was present in 28/28 cases, but the model tolerates
    # the documented exception.
    #
    # album/album_id always travel together and are null together: a music
    # video (videoType == "MUSIC_VIDEO_TYPE_OMV", measured 3/3) is the
    # provider not sending an album at all, not an error.
    #
    # duration_seconds is nullable because it comes from parsing "length"
    # ("5:38"), and _seconds_from_length() returns None rather than raise on
    # an unexpected format.
    #
    # No `position`: a standalone track is not inside a playlist, unlike
    # PublicPlaylistTrack below.
    track_id: str
    title: str
    artists: list[SearchArtistRef] = Field(default_factory=list)
    album: str | None = None
    album_id: str | None = None
    duration_seconds: int | None = None
    thumbnail_url: str | None = None


class PublicPlaylistTrack(BaseModel):
    # Same shape as models.genres.GenrePlaylistTrack, and deliberately
    # without `id`: the internal catalog uuid never crosses an endpoint
    # boundary in either direction (docs/api/conventions.md, "Track
    # identity"). GET /playlists/{id} and GET /playlists/liked are the
    # only two endpoints that already expose it, for shape parity with an
    # endpoint that predates the rule -- a brand-new public DTO has no such
    # precedent, so it follows the rule as written.
    # No min_length here, unlike models.playlists.PlaylistTrack: this
    # shape is shared with a genre playlist track, whose own model
    # (models.genres.GenrePlaylistTrack) does not require one either.
    track_id: str
    title: str
    artists: list[TrackArtist]
    album: str
    album_id: str
    duration_seconds: int
    thumbnail_url: str
    position: int


class PublicPlaylist(BaseModel):
    # Shared shape for a public user playlist. A playlist of genre adds
    # exactly one field on top of this one: see PublicGenrePlaylist below.
    id: str
    title: str
    description: str | None = None
    owner: PublicOwner
    track_count: int
    has_more: bool
    total_duration_seconds: int
    tracks: list[PublicPlaylistTrack]
    # Up to 4 miniatures for the share card's mosaic, in position order,
    # filled by a database RPC (the domain-specific one: SEE the mapping
    # table in services/playlist_service.py and services/genre_service.py),
    # never derived from `tracks` in Python. Can come back empty -- no
    # tracks, or none of the first 4 have a thumbnail -- and that is a
    # normal 200, not an error. 4 is a cap, not a guarantee: the RPC takes
    # the first 4 tracks by position and only then drops the ones with no
    # thumbnail, so a longer playlist can still return fewer than 4.
    thumbnails: list[str] = Field(default_factory=list, max_length=4)


class PublicGenrePlaylist(PublicPlaylist):
    # Everything PublicPlaylist has, plus the curated cover art that only
    # a genre playlist carries (genre_playlists.thumbnail_url). A user
    # playlist's table has no cover column at all, so this field does not
    # exist on PublicPlaylist -- adding it there as an always-null field
    # would be the same antipattern models/artist.py already rejects for
    # ArtistRelease ("always returning them would tell the client
    # something that is never true"). `thumbnails` is the mosaic and is
    # populated on both; `thumbnail_url`, when present, is the one curated
    # cover a page can choose to show instead.
    thumbnail_url: str | None = None

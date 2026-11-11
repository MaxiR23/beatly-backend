# INFO: Request and result models of the internal ingests (POST /ingest/genres and POST /ingest/releases).

from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from models.artist_refs import TrackArtists
from models.releases import ReleaseType

# Limits of one submission. A body over any of them is a 422; the numbers are
# a ceiling well above an expected payload (10 genres x 6 playlists x 60
# tracks), not a tuning knob. Same idea as BULK_TRACKS_LIMIT in
# models/playlists.py.
MAX_GENRES = 50
MAX_PLAYLISTS_PER_GENRE = 50
MAX_TRACKS_PER_PLAYLIST = 500
MAX_TRACKS = 20_000
MAX_ARTISTS_PER_TRACK = 20
MAX_DURATION_SECONDS = 86_400

# Ids of the external provider: letters, digits, "_" and "-". They go into
# SQL parameters and filters, so nothing else gets in.
ProviderId = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9_-]+$", min_length=1, max_length=128)
]
# A slug goes into the in_() filter of the genres lookup, which is split on
# commas and parentheses: whitespace, quotes and those characters are out.
Slug = Annotated[str, StringConstraints(pattern=r"^[^\s,()\"'\\]{1,100}$")]
Title = Annotated[str, StringConstraints(min_length=1, max_length=300)]
HttpsUrl = Annotated[str, StringConstraints(pattern=r"^https://", max_length=2048)]


class IngestTrack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    track_id: ProviderId
    title: Title
    # The rule of every song of the app (first artist with an id), not copied.
    artists: Annotated[TrackArtists, Field(max_length=MAX_ARTISTS_PER_TRACK)]
    album: Title
    album_id: ProviderId
    duration_seconds: int = Field(ge=1, le=MAX_DURATION_SECONDS)
    thumbnail_url: HttpsUrl


class IngestGenrePlaylist(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_playlist_id: ProviderId
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    tracks: list[ProviderId] = Field(min_length=1, max_length=MAX_TRACKS_PER_PLAYLIST)

    @field_validator("tracks")
    @classmethod
    def _reject_repeated_tracks(cls, ids: list[str]) -> list[str]:
        if len(set(ids)) != len(ids):
            raise ValueError("a playlist must not repeat a track")
        return ids


class IngestGenre(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: Slug
    playlists: list[IngestGenrePlaylist] = Field(
        min_length=1, max_length=MAX_PLAYLISTS_PER_GENRE
    )

    @field_validator("playlists")
    @classmethod
    def _reject_repeated_sources(
        cls, playlists: list[IngestGenrePlaylist]
    ) -> list[IngestGenrePlaylist]:
        sources = [p.source_playlist_id for p in playlists]
        if len(set(sources)) != len(sources):
            raise ValueError("a genre must not repeat a source playlist")
        return playlists


class GenreIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    genres: list[IngestGenre] = Field(min_length=1, max_length=MAX_GENRES)
    tracks: list[IngestTrack] = Field(min_length=1, max_length=MAX_TRACKS)

    # All or nothing: an incoherent submission is rejected whole. The
    # messages carry no values.
    @model_validator(mode="after")
    def _check_coherence(self) -> GenreIngestRequest:
        slugs = [g.slug for g in self.genres]
        if len(set(slugs)) != len(slugs):
            raise ValueError("a genre slug must not repeat")
        rows = [t.track_id for t in self.tracks]
        if len(set(rows)) != len(rows):
            raise ValueError("a track row must not repeat")
        referenced = {
            track_id
            for genre in self.genres
            for playlist in genre.playlists
            for track_id in playlist.tracks
        }
        if referenced != set(rows):
            raise ValueError("tracks and the ids the playlists reference differ")
        return self


class GenreIngestResult(BaseModel):
    genres: int
    playlists: int
    tracks: int


# Limits of the releases submission. Same idea as the block above: a ceiling,
# not a tuning knob.
MAX_RELEASES = 500
MAX_ARTISTS_PER_RELEASE = 20

# Ids of the new releases page. They go into SQL parameters.
ReleaseAlbumId = Annotated[
    str, StringConstraints(pattern=r"^MPREb_[A-Za-z0-9_-]+$", max_length=128)
]
ReleaseAudioPlaylistId = Annotated[
    str, StringConstraints(pattern=r"^OLAK5uy_[A-Za-z0-9_-]+$", max_length=128)
]
ReleaseArtistId = Annotated[str, StringConstraints(pattern=r"^UC[A-Za-z0-9_-]{22}$")]

FORBIDDEN_ARTIST_NAMES = frozenset({"null", "none", "undefined"})


def _check_artist_name(name: str) -> str:
    # The messages carry no value: the name is the caller's data.
    if name.strip() == "":
        raise ValueError("an artist name must not be blank")
    if name.strip().casefold() in FORBIDDEN_ARTIST_NAMES:
        raise ValueError("an artist name must not be a placeholder")
    return name


ReleaseArtistName = Annotated[Title, AfterValidator(_check_artist_name)]


class IngestReleaseArtist(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: ReleaseArtistId
    name: ReleaseArtistName


class IngestRelease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    album_id: ReleaseAlbumId
    audio_playlist_id: ReleaseAudioPlaylistId
    type: ReleaseType
    title: Title
    artists: list[IngestReleaseArtist] = Field(
        min_length=1, max_length=MAX_ARTISTS_PER_RELEASE
    )
    thumbnail_url: HttpsUrl


class ReleaseIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    releases: list[IngestRelease] = Field(min_length=1, max_length=MAX_RELEASES)

    @field_validator("releases")
    @classmethod
    def _reject_repeated_albums(
        cls, releases: list[IngestRelease]
    ) -> list[IngestRelease]:
        ids = [release.album_id for release in releases]
        if len(set(ids)) != len(ids):
            raise ValueError("a release must not repeat")
        return releases


class ReleaseIngestResult(BaseModel):
    releases: int

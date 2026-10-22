# INFO: Artist request/response models.

from pydantic import BaseModel, Field, computed_field

from core.thumbnails import image_tiers
from models.album import AlbumRef
from models.artist_refs import TrackArtists
from models.images import ImageTiers


class ArtistSong(BaseModel):
    # album/album_id use the same field names SearchSong already exposes
    # in /search, so the client has a single "song" mapping regardless of
    # where it came from. The two fields always travel together: if
    # album_id is null, album is too, even when the provider sent a
    # name with no id. The normalization that enforces that invariant
    # lives in services/artist_service.py, not here.
    track_id: str | None = None
    title: str
    # The track artists rule (models/artist_refs.py) applies.
    artists: TrackArtists
    album: str | None = None
    album_id: str | None = None
    duration_seconds: int | None = None
    thumbnail_url: str | None = None

    # image is derived when the response is serialized, from thumbnail_url.
    # A computed field never goes in a model that is a request or that is
    # dumped to the database.
    @computed_field
    @property
    def image(self) -> ImageTiers | None:
        return image_tiers(self.thumbnail_url)


class ArtistRelease(BaseModel):
    # No artists and no audio_playlist_id: the provider's parse_single()
    # never includes those keys for a single/EP (not even empty/null), so
    # always returning them would tell the client something that is never
    # true. type (Single/EP) lives here and deliberately not on AlbumRef,
    # whose exclusion of `type` was already decided in plan-100.md.
    id: str
    title: str
    year: str | None = None
    type: str | None = None
    thumbnail_url: str | None = None

    @computed_field
    @property
    def image(self) -> ImageTiers | None:
        return image_tiers(self.thumbnail_url)


class ArtistRef(BaseModel):
    # Not SearchArtist (no thumbnail_url) and not a song or album artist
    # reference (whose id is nullable because it models a mention with no
    # link): a related artist always arrives with its browseId.
    id: str
    name: str
    thumbnail_url: str | None = None

    @computed_field
    @property
    def image(self) -> ImageTiers | None:
        return image_tiers(self.thumbnail_url)


class Artist(BaseModel):
    id: str
    name: str
    thumbnail_url: str | None = None
    songs: list[ArtistSong] = Field(default_factory=list)
    albums: list[AlbumRef] = Field(default_factory=list)
    singles: list[ArtistRelease] = Field(default_factory=list)
    related: list[ArtistRef] = Field(default_factory=list)

    @computed_field
    @property
    def image(self) -> ImageTiers | None:
        return image_tiers(self.thumbnail_url)

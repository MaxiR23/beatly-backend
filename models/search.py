# INFO: Search request/response models.

from pydantic import BaseModel, Field, computed_field

from core.thumbnails import image_tiers
from models.artist_refs import AlbumArtistRef, TrackArtists
from models.images import ImageTiers


class SearchArtist(BaseModel):
    id: str
    name: str
    # Nullable like SearchAlbum.thumbnail_url; the default lets a SearchResult
    # cached before this field existed keep validating.
    thumbnail_url: str | None = None

    # image is derived when the response is serialized, from thumbnail_url.
    # A computed field never goes in a model that is a request or that is
    # dumped to the database.
    @computed_field
    @property
    def image(self) -> ImageTiers | None:
        return image_tiers(self.thumbnail_url)


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

    @computed_field
    @property
    def image(self) -> ImageTiers | None:
        return image_tiers(self.thumbnail_url)


class SearchAlbum(BaseModel):
    id: str
    title: str
    # Albums keep their own rule: the id is optional and an empty list is
    # valid. See AlbumArtistRef.
    artists: list[AlbumArtistRef] = Field(default_factory=list)
    year: str | None = None
    thumbnail_url: str | None = None

    @computed_field
    @property
    def image(self) -> ImageTiers | None:
        return image_tiers(self.thumbnail_url)


class SearchResult(BaseModel):
    artist: SearchArtist | None
    songs: list[SearchSong]
    albums: list[SearchAlbum]

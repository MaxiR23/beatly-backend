# INFO: Genre response models.

from pydantic import BaseModel, computed_field

from core.thumbnails import image_tiers, mosaic_tiers
from models.artist_refs import TrackArtists
from models.images import ImageTiers


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

    # image is derived when the response is serialized, from thumbnail_url.
    # A computed field never goes in a model that is a request or that is
    # dumped to the database.
    @computed_field
    @property
    def image(self) -> ImageTiers | None:
        return image_tiers(self.thumbnail_url)


# Item of GET /genres/{slug}/playlists only. thumbnail_urls is required, no
# default, so get_genre_playlists() cannot forget it; get_genre_playlist()
# and the public share keep using GenrePlaylist (#160).
class GenrePlaylistListItem(GenrePlaylist):
    thumbnail_urls: list[str]

    @computed_field
    @property
    def mosaic(self) -> list[ImageTiers]:
        return mosaic_tiers(self.thumbnail_urls)


class GenrePlaylistTrack(BaseModel):
    track_id: str
    title: str
    artists: TrackArtists
    album: str
    album_id: str
    duration_seconds: int
    thumbnail_url: str
    position: int

    @computed_field
    @property
    def image(self) -> ImageTiers | None:
        return image_tiers(self.thumbnail_url)

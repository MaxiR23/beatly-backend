# INFO: Like request/response models.

from pydantic import BaseModel, computed_field

from core.thumbnails import image_tiers
from models.artist_refs import TrackArtists
from models.images import ImageTiers
from models.responses import Paginated


# Track metadata as the clients send it. A like does not store these fields:
# POST /likes upserts them into the catalog (tracks) and user_likes only
# references that row. The activity domain reuses the same shape for a play
# event. SEE: models/activity.py
class TrackMetadata(BaseModel):
    title: str
    artists: TrackArtists
    album: str
    album_id: str
    thumbnail_url: str
    duration_seconds: int | None = None


class Like(BaseModel):
    track_id: str
    title: str
    artists: TrackArtists
    album: str
    album_id: str
    thumbnail_url: str
    duration_seconds: int | None = None
    created_at: str
    updated_at: str
    deleted_at: str | None = None

    # image is derived when the response is serialized. A computed field never
    # goes in a model that is a request or that is dumped to the database:
    # TrackMetadata is inherited by three requests and the services model_dump()
    # it straight into tracks / play_events.
    @computed_field
    @property
    def image(self) -> ImageTiers | None:
        return image_tiers(self.thumbnail_url)


class AddLikeRequest(TrackMetadata):
    track_id: str


# The checkpoint belongs to the read, not to the pagination, so it is a sibling
# of items and page and not a field of PageBlock.
class LikesPage(Paginated[Like]):
    checkpoint: str

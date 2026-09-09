# INFO: Track request/response models.

from pydantic import BaseModel, Field

from models.album import AlbumRef
from models.artist import ArtistRef
from models.search import SearchArtistRef


class TrackRef(BaseModel):
    # (a) The shape shared by /upnext items and by related.songs, with the
    # same field names SearchSong and ArtistSong already use, so the client
    # has a single "song" mapping regardless of where it came from.
    #
    # (b) track_id is required: in a watch track the library indexes
    # data["videoId"] directly, and related only lets ATV items through, so
    # an item without an id is a layout change (502), not a documented
    # branch -- unlike AlbumTrack.track_id, which is nullable because the
    # provider *does* document the unavailable-track branch.
    #
    # (c) album/album_id always travel together, the same invariant
    # docs/api/artists.md documents and services/artist_service.py
    # implements.
    track_id: str
    title: str
    artists: list[SearchArtistRef] = Field(default_factory=list)
    album: str | None = None
    album_id: str | None = None
    duration_seconds: int | None = None
    thumbnail_url: str | None = None


class TrackUpNext(BaseModel):
    # An object, not a bare list: every provider-backed endpoint in this
    # repo returns an object in data, and it lets a field be added later
    # without breaking the client.
    tracks: list[TrackRef] = Field(default_factory=list)


class TrackLyricLine(BaseModel):
    # Milliseconds because that is the unit the provider sends (measured:
    # 10 -> 5990 on the first line of Bohemian Rhapsody); not converted to
    # seconds to avoid losing precision when highlighting the active line.
    text: str
    start_ms: int | None = None
    end_ms: int | None = None


class TrackLyrics(BaseModel):
    # has_timestamps is the only field the client needs to branch on. When
    # it is false, start_ms/end_ms are explicit null on every line, never
    # absent.
    has_timestamps: bool
    source: str | None = None
    lines: list[TrackLyricLine] = Field(default_factory=list)


class TrackLyricsResult(BaseModel):
    # The wrapper exists so data.lyrics can be null without data itself
    # being null: a track with no lyrics is a 200 with data, not an empty
    # resource.
    lyrics: TrackLyrics | None = None


class TrackRelated(BaseModel):
    # ArtistRef comes from models/artist.py and AlbumRef from
    # models/album.py, neither of which is modified: the same reuse
    # criterion models/artist.py already applies to AlbumRef and
    # SearchArtistRef.
    songs: list[TrackRef] = Field(default_factory=list)
    artists: list[ArtistRef] = Field(default_factory=list)
    albums: list[AlbumRef] = Field(default_factory=list)


class TrackCreditSection(BaseModel):
    # (a) The provider calls the list of names "data"; renamed to "names"
    # because data.performed_by.data is unreadable in a contract whose own
    # envelope is already called "data", the same reasoning that already
    # renames videoId -> track_id in this domain.
    # (b) localized_title comes localized by the provider: it is for
    # display, never for the client to branch on -- the same "never by
    # title" rule docs/api/tracks.md already documents for /related.
    localized_title: str
    names: list[str] = Field(default_factory=list)


class TrackCredits(BaseModel):
    # (a) The four typed fields are nullable because the provider only
    # sends them when their localized title matches the library's map
    # (get_song_credit_section_map); a section it does not recognize is not
    # lost, it falls into other_sections.
    # (b) TrackCredits() with no arguments IS the expected-empty response --
    # four explicit nulls and other_sections: [] -- the same explicit-null
    # rule TrackLyricLine already applies to start_ms/end_ms.
    # (c) An object, not a bare list, like the other three endpoints of this
    # domain.
    performed_by: TrackCreditSection | None = None
    written_by: TrackCreditSection | None = None
    produced_by: TrackCreditSection | None = None
    music_metadata_provided_by: TrackCreditSection | None = None
    other_sections: list[TrackCreditSection] = Field(default_factory=list)

# INFO: Reads the new releases stored by the ingest (replace_new_releases, migration 053).

import logging

from supabase import Client

from core.pagination import PageRequest, SortKey, ValueType, apply_page, build_page
from core.thumbnails import square_thumbnail_url
from core.upstream import translate_upstream_errors
from models.releases import NewRelease
from models.responses import PageBlock

# setup_logging() is called once, in app.py, and is not called again here.
logger = logging.getLogger(__name__)

# album_id and not a uuid as the tie-break: it is stable across ingests, and
# unique per source.
_SORT = SortKey(
    "position",
    ValueType.INT,
    descending=False,
    id_column="album_id",
    id_type=ValueType.TEXT,
)
# position is not exposed: it is the order of the list, and the cursor needs
# it on every row. audio_playlist_id is stored and not exposed yet.
_COLUMNS = "album_id, title, type, artists, thumbnail_url, position"

# The literal of replace_new_releases (migration 053). The integration test
# writes with the function and reads with this route, so it fails if they
# diverge. The read is filtered by source so the pair (position, album_id) of
# the cursor is unique.
_SOURCE = "provider_new_releases"

_THUMBNAIL_SIZE = 544


def list_new_releases(
    db: Client, page: PageRequest
) -> tuple[list[NewRelease], PageBlock]:
    with translate_upstream_errors():
        # get_db (service-role): the table has no privileges for the client
        # roles (migration 053).
        query = (
            db.table("new_releases")
            .select(_COLUMNS, count=page.count_mode)
            .eq("source", _SOURCE)
        )
        response = apply_page(query, _SORT, page).execute()
        rows, page_block = build_page(response.data or [], page, _SORT, response.count)
        return [NewRelease(**_with_square_thumbnail(row)) for row in rows], page_block


# Returns a copy of the row with thumbnail_url rewritten. The key is indexed
# on purpose: it is in every select, and if it were missing the KeyError is
# a 502 inside translate_upstream_errors(). It is a copy in each database
# service, not an import, like the other private mapping helpers.
def _with_square_thumbnail(row: dict) -> dict:
    return {
        **row,
        "thumbnail_url": square_thumbnail_url(
            row["thumbnail_url"], _THUMBNAIL_SIZE, smart_crop=True
        ),
    }

## GET /releases

Lists the new releases the app shows. A cursor-paginated list ordered by the
position of the provider's page. See the Pagination section of `conventions.md`
for the shared `limit`/`cursor` query params and the `data.items`/`data.page`
shape. It only reads the database; it never calls the external provider. It is
public: it needs no token.

| Case | Status | Body |
|---|---|---|
| The table has releases | 200 | `ok: true`, `data.items`, `data.page` |
| The table is empty | 200 | `ok: true`, `data.items: []`, `data.page.has_more: false`, `data.page.next_cursor: null`, `data.page.total: 0` |
| Invalid `limit` | 422 | `ok: false`, `reason: "invalid_request"` |
| Invalid or expired `cursor` | 422 | `ok: false`, `reason: "invalid_cursor"` |
| Database failed, or a stored row is invalid | 502 | `ok: false`, `reason: "upstream_error"` |
| Database timed out | 504 | `ok: false`, `reason: "upstream_timeout"` |

### Shape

```json
{
  "ok": true,
  "data": {
    "items": [
      {
        "album_id": "MPREb_...",
        "title": "Album",
        "type": "album",
        "artists": [{"id": "UC...", "name": "Artist"}],
        "thumbnail_url": "https://.../abc=w544-h544-p-l90-rj",
        "image": {"small": "...", "medium": "...", "large": "...", "color": null}
      }
    ],
    "page": {"limit": 50, "next_cursor": "<opaque cursor>", "has_more": true, "total": 109}
  }
}
```

- `album_id` is the id of `/album/{id}`. `type` is `album`, `single` or `ep`.
- `artists` is never empty and every `id` is a string.
- `thumbnail_url` is the stored cover rewritten to 544 pixels with smart crop.
- The order is the one of the provider's page. The stored `position` and the
  `audio_playlist_id` are not exposed.
- The list is replaced as a whole by the ingest (`POST /ingest/releases`). A walk
  across pages that overlaps a replacement can skip or repeat an item; start
  again from the first page.

### Image color

Every `image` carries `color`: a lowercase `"#rrggbb"` or `null`. It is
`null` until the server has computed it (the first time it sees the
picture) and a later request carries it; the computation happens after the
response is sent. See [Image tiers](conventions.md#image-tiers) and
[Color](conventions.md#color). Reading the stored colors never fails the
request: on a database failure `color` is `null` and the rest of the body is
the same.

### Headers

Every response carries `Cache-Control: no-store`.

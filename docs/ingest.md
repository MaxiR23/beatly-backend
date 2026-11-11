# Ingest: genre playlists and new releases

The collector fills two things: the genre playlists (`genres`, most of this
guide) and the list of new releases (`releases`, see the section [New
releases](#new-releases)). How the genre playlists get their songs: it is a collector that runs on the
owner's own device (an Android phone), and a back-end endpoint that validates and writes what
the collector sends.

## End to end

    phone (Termux)                          back                      database
    ┌──────────────────────────┐   POST    ┌──────────────────┐  RPC  ┌────────────────┐
    │ collector.py             │  /ingest  │ validates the    │ ────▶ │ replace_genre_ │
    │ reads the source         │  /genres  │ body strictly,   │       │ playlists      │
    │ playlists, resolves each │ ────────▶ │ checks the slugs │       │ (migration 052)│
    │ video to its audio, CSV, │  token    │ and writes all   │       │ all or nothing │
    │ log, then sends          │           │ or nothing       │       └────────────────┘
    └──────────────────────────┘           └──────────────────┘

- The collector reads `genre_sources.json` (which source playlists feed which
  genre) and `overrides.json` (manual fixes), both versioned in this repo and
  downloaded from the public repo at every run.
- It reads each source playlist from the external provider. Why it runs on
  a device and not on a server is in ADR 028.
- For each video of a playlist it finds the audio version of the official
  artist (the statuses below), and writes a CSV and a summary on the phone.
- With `--write` it sends the result to `POST /ingest/genres` of the back.
  The phone holds no database key: only the URL of the back and the ingest
  token, which works for one thing.
- The back validates the submission with strict models (a mistake anywhere
  rejects everything), checks that the genres exist, and writes with one call
  to `replace_genre_playlists`. Contract: [`api/ingest.md`](api/ingest.md).
- If the collector does not run, nothing is deleted: the app keeps serving the
  last saved playlists.

## Scope and identity

- Only the genres that appear in `genre_sources.json` are touched; the others
  are left as they are.
- Each source playlist has a stable identity: `genre_playlists.source_playlist_id`
  (migration 052), unique inside a genre. A playlist that already exists keeps
  its `id`, so saved links and library entries keep working, and only its
  songs are replaced.
- The playlists of those genres that are not in the file are deleted. The rows
  that existed before the column (the historical seed) have no source id, so
  they count as "not in the file" and are deleted the first time their genre is
  ingested; their ids answer 404 afterwards.
- Every song is written to `tracks` with an upsert on `track_id` that
  overwrites `title`, `artists`, `album`, `album_id`, `duration_seconds` and
  `thumbnail_url` (the same as liking a song does).

## Back configuration

- `INGEST_TOKEN`: the token the collector sends as `Authorization: Bearer`.
  At least 32 characters and different from every `SUPABASE_*` key, or the app
  does not start. Without it the endpoint answers 401 to everything. Generate
  one with `python -c "import secrets; print(secrets.token_urlsafe(48))"` and
  put it only in the back's environment and on the phone.
- `INGEST_RPC_TIMEOUT`: seconds the database write may take, 60 by default,
  above `SUPABASE_READ_TIMEOUT` and at most 300. The platform's request
  timeout must be above it.
- Apply migration `052` in the database **before** the first `--write` and
  before deploying the code that calls it (`db/migrations/README.md`).

## Install the collector in Termux

1. Install Termux and, in it:

       pkg update
       pkg install python curl cronie termux-services

2. Download the collector and its pins (the same versions as the back) from
   a fixed commit of `main`, not from `main` itself, so the phone only runs
   code you chose. `REF` is the full hash of that commit, and `RAW` is the
   base URL of the folder `ingest/device` at that commit: the same URL
   `RAW_BASE_URL` has in `collector.py`, with `main` replaced by `$REF`:

       REF=<full commit hash>
       RAW=<RAW_BASE_URL with main replaced by $REF>
       mkdir -p ~/beatly
       curl -fsSLo ~/beatly/collector.py "$RAW/collector.py"
       curl -fsSLo ~/beatly/requirements.txt "$RAW/requirements.txt"
       pip install -r ~/beatly/requirements.txt

3. Create `~/.beatly-collector.env` with the URL of the back and the token,
   and lock it down. The collector refuses to run if the file is readable by
   anyone else:

       BEATLY_API_URL=https://api.example.com
       BEATLY_INGEST_TOKEN=<the same value as INGEST_TOKEN in the back>

       chmod 600 ~/.beatly-collector.env

   `BEATLY_API_URL` must be `https://`. The only exceptions, to try the back
   on your own machine before deploying it, are plain `http://` to `localhost`,
   `127.0.0.1` and the private network ranges `10.0.0.0/8`, `172.16.0.0/12`
   and `192.168.0.0/16` (for example `http://192.168.1.20:8000`). Anything
   else over `http` is refused with exit code 1. `172.15.x.x` and `172.32.x.x`
   are public addresses and are refused.

4. First run, a dry run (nothing is sent to the back):

       python ~/beatly/collector.py genres

5. Cron. Restart Termux once, then:

       sv-enable crond
       crontab -e

   and add a line with the schedule you choose (`<minute> <hour> <day of
   month> <month> <day of week>`):

       <schedule> python ~/beatly/collector.py genres --write >> ~/.beatly-collector/cron.log 2>&1

6. Keep it alive: run `termux-wake-lock` (Termux notification, "Acquire
   wakelock") and turn off the battery optimization for Termux in the Android
   settings. Without both, Android can stop cron with the screen off.

To update the collector, pick the new commit, set `REF` to its hash and
repeat the two `curl` and the `pip install -r`. Read the diff of
`ingest/device/` between the two commits before you do. After updating, run a
dry run of `genres` and of `releases` (`python ~/beatly/collector.py genres`
and `python ~/beatly/collector.py releases`) before the next cron.

Security notes:

- Pinning `REF` means a change pushed to `main` never runs on the phone until
  you update it by hand.
- `genre_sources.json` and `overrides.json` are still read from `main` at
  every run. They are data, not code: the collector parses them strictly and
  the back validates the whole submission again, so the worst a bad push can
  do is replace the genre playlists of the genres it names with wrong ones.
- Keep two-factor authentication on the GitHub account that can push to
  this repo.
- If the phone is lost, or the token may have leaked, set a new
  `INGEST_TOKEN` in the back and on the phone. The old one stops working at
  once.

## Input files

Both live in `ingest/device/`, are public, and hold no secrets. A change is a
commit to the repo; the collector downloads them at every run.

`genre_sources.json` (starts as `{"genres": []}`, and with it the collector
exits 1 with `nothing to ingest`):

    {"genres": [
      {"slug": "rock", "playlists": [
        {"source": "PLexample0001", "title": "Rock classics",
         "description": "optional", "category": "optional"},
        {"source": "PLexample0002"}
      ]}
    ]}

- `slug` is the slug of an existing genre; `source` is the id of the source
  playlist. Both are required. `title`, `description` and `category` are
  optional; without a title or description the ones of the source playlist are
  used.
- Unknown keys, a repeated slug, a repeated source inside a genre and a genre
  without playlists are errors.
- The order of the playlists inside a genre is their order in the app.
- A slug that does not exist is only detected with `--write` (the back
  answers 404 `genre_not_found` and writes nothing).

`overrides.json` (starts as `{}`): `{"<video id>": "<audio id>"}`. For that
video no search is made: the collector reads the audio from the provider
(its first item must be the audio asked for, or the file is wrong and the run
exits 1 with no report) and uses it, with the status `excepcion manual`. An
override wins over everything else. A 404 for this file is the same as `{}`.

To add a source playlist or a fix: edit the file, commit, and the next run
uses it.

## Statuses

Each video of a source playlist ends in one of eight statuses, written in the
CSV and counted in the summary:

| Status | Meaning | Sent |
|---|---|---|
| `oficial` | A search (songs, 5 results, with the title and up to two artists) found a result with the same normalized title (or contained in the video's) and at least one artist id in common | yes |
| `ya era audio` | The video already is an audio track; used as is | yes |
| `excepcion manual` | The video is in `overrides.json` | yes |
| `solo por nombre` | A result matches the title but shares no artist id | no |
| `sin match` | No result matches the title | no |
| `datos incompletos` | Unavailable, or the song lacks album, album id, duration, cover or an id for its first artist, so it cannot be a valid row | no |
| `repetida` | The same audio appeared earlier in the same playlist; the first keeps its status | no |
| `error del proveedor` | The provider failed after the retries | no |

Titles are compared after lowercasing, removing accents, dropping groups and
suffixes such as official video, lyrics, visualizer, audio, live and en vivo,
and dropping anything after `ft` or `feat`. The duration difference (audio
minus video, seconds) is only written to the CSV; it never decides.

## Reports and files on the phone

In `~/.beatly-collector/` (created with mode 700):

- `reports/genre-ingest-YYYYMMDD-HHMMSS.csv` (UTC), one row per video of every
  source playlist, skipped ones included, with the columns `genero`,
  `playlist` (the source id), `titulo`, `artistas`, `id_video`, `id_audio`,
  `estado`, `diferencia_duracion`. It is written on every run, also on a
  dry run and also when the run aborts.
- `collector.log`, rotating (1 MB, 3 files). It never holds the token.
- `cache.json`: the `oficial` results by video id, so the next run searches
  only for what is new. It has no expiry; to invalidate it, delete the file.
  An unreadable cache is not ignored: the run exits 1 and the log says to
  delete it.
- The summary goes to stdout: one line per playlist and a `total:` line with
  the eight counters and `se guardan=`, the number of songs that will be saved.

## When it does not write

The run aborts with exit code 1, sends nothing, and still writes the CSV and
the summary, when: a source playlist could not be read, any video ended in
`error del proveedor`, or any playlist ends with no saved song. This protects
the app from being emptied by a provider problem.

## Exit codes and retries

- `0`: the run is complete (a dry run ended, or the back accepted the
  submission with the counts sent).
- `1`: any failure (configuration, input files, cache, provider, rejected or
  failed send, aborted run).
- `2`: bad command line arguments.

The collector retries the provider 3 times (waits of 1 s and 2 s) and the
download of the input files the same way. The send is retried up to 4 times,
waiting 5, 15 and 45 s, on a network error, a timeout and a 502, 503 or 504.
It never retries a 4xx. Sending again is safe: the endpoint leaves the same
result for the same body, and two sends at the same time wait for each other.
It asks nothing and never reads stdin, so it is safe under cron.

## First run, in order

1. Apply migration `052` (`db/migrations/README.md` has the checks).
2. Set `INGEST_TOKEN` in the back's environment.
3. Fill `genre_sources.json` and, if needed, `overrides.json`, and commit.
4. On the phone, run the collector in dry run, and read the CSV and the
   summary.
5. Back up `genre_playlists` and `genre_playlist_tracks` of those genres: the
   first write deletes the historical playlists and the rollback of `052` does
   not bring them back.
6. Run it with `--write`, and run the confirmation query of the `052` entry in
   the README.

## New releases

`python ~/beatly/collector.py releases [--write]` reads the provider's page of
new releases and, with `--write`, sends it to `POST /ingest/releases`
([`api/ingest.md`](api/ingest.md)); the app reads it with `GET /releases`
([`api/releases.md`](api/releases.md)). The design is in ADR 029.

- It reads one page, in one response, with the language fixed to `en`. The
  location is not fixed: the list depends on the IP of the phone, on purpose.
- It does not use `genre_sources.json`, `overrides.json` nor `cache.json`, and
  downloads no input file.
- Each card of the grid is checked on its own against the rules below; a card
  that fails is discarded and counted with the first reason that failed.

### Rules and reasons

In this order; the reason of a discarded card is the first that fails.

| Reason (`estado`) | The card is discarded when |
|---|---|
| `tarjeta ilegible` | It is not a card of the expected renderer |
| `id de album invalido` | The album id is not `MPREb_...` (letters, digits, `_`, `-`; up to 128) |
| `playlist de audio invalida` | The audio playlist id is not `OLAK5uy_...` (same characters; up to 128) |
| `tipo no permitido` | The type text is not exactly `Album`, `Single` or `EP` (it is stored as `album`, `single`, `ep`) |
| `titulo invalido` | The title is empty or longer than 300 characters |
| `sin artistas` | No artist with a link |
| `artista sin id valido` | An artist with a link is not an artist page, or its id is not `UC` and 22 characters |
| `demasiados artistas` | More than 20 artists |
| `artista sin link` | Any artist name has no link (the whole card is discarded) |
| `nombre de artista invalido` | A name is empty, longer than 300 characters, or is `null`, `none` or `undefined` (any case) |
| `portada invalida` | The cover is not an `https://` URL up to 2 048 characters |
| `repetido` | The same album appeared earlier on the page; the first one stays |

The page is parsed on the raw response and not with the library's parsers,
because those drop the artist names without a link and the card must be
discarded whole. The separators between the artists are exactly ` • `,
` & ` and `, `; any other run without a link after the type is an artist
without a link.

### Report and summary

`reports/release-ingest-YYYYMMDD-HHMMSS.csv` (UTC) in `~/.beatly-collector/`,
one row per card in the order of the page, with the columns `orden`, `estado`
(`se guarda` or the reason), `tipo`, `titulo`, `artistas`, `id_album` and
`id_playlist_audio`. It is written when the page had cards, on a dry run too
and also when the run aborts; every text cell is protected against formulas.
The summary goes to stdout: a `total:` line with `leidas=`, a counter for each
reason and `se guardan=`; when the run aborts for the cards, an `aborta:` line
with the reason and its figures.

### When it does not write

The run aborts with exit code 1, sends nothing and the app keeps the last list
when: the page could not be read after the retries; the page has another shape
(more than one section, a grid with continuations, no grid); it has no cards;
no card passes; more than 500 pass (the back's limit; it is never truncated);
or fewer than half of the cards read pass (`MIN_KEPT_RATIO = 0.5`, exactly half
does not abort). The two samples measured kept 95%, so a bigger discard means
the format of the page changed. The `aborta:` line has the figures (read, pass
and percentage). The rule lives only in the collector; the back does not know
it.

### Exit codes, retries and schedule

The same as `genres`: `0` complete, `1` any failure, `2` bad arguments; the
page is read with 3 attempts (waits of 1 s and 2 s) and the send is retried as
described above. Sending again is safe.

Cron line, with the schedule you choose:

    <schedule> python ~/beatly/collector.py releases --write >> ~/.beatly-collector/cron.log 2>&1

### First deployment, in order

1. Apply migration `053` and run the confirmation query of its entry in
   `db/migrations/README.md`. It has to be before the code: without it the two
   endpoints answer 502.
2. Deploy the back.
3. On the phone, update the collector (see above), run
   `python ~/beatly/collector.py releases` and read the CSV and the summary.
4. Run it with `--write`, twice if you want to see that the second leaves the
   same list, and read `GET /releases`.
5. Add the cron line.

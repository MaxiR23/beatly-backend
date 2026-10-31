# 026. Recommendations are split into sections without repeats

How the personalized set is divided into the lists of the new home screen
without repeating an artist or an album, where the section of each item is
stored, what happens to a set stored before the sections, and what the
extra provider calls cost (issue #227). It evolves ADR 025: the generation
stays inside the request, in parallel, in one call to
`replace_user_recommendations`.

## Context

The set of #221 was two flat lists of 15 items. The new home screen asks for
four lists: ten albums for you, up to three sections each tied to a seed
artist (three artists and three albums, alternating), ten recommended albums
and ten recommended artists, with no artist and no album twice in the
response. The app's onboarding (#148) validates `artists` and `albums` and
needs both to be present, so they cannot disappear in the same change.

The shape of the response grew; the requirement on time did not: the
generation must still fit the 12 s request budget (ADR 024).

## Decision

**Lists and quantities.** `albums_for_you` 10, `seed_sections` up to 3 of
6 items (3 artists and 3 albums, alternating, artist first),
`recommended_albums` 10 and `recommended_artists` 10. The quantities are
constants of the service, not settings. The sections use only the artist
seeds, the first three whose page exists, in the order of the seeds (favorites
and listened artists alternating). A seed that the provider confirms is gone
is no longer a seed, so the rounds spread as before.

**Order of assignment and one `taken` set.** Each item goes to the first list
that asks for it, in the order of the screen: `albums_for_you` (the 10 best
ranked albums, with the ranking of ADR 025: the reserve of album seeds first,
then the artist seeds by rounds, then more from the album seeds), the three
sections, `recommended_albums` (the next 10 of the same ranking, skipping
what is taken) and `recommended_artists` (the chosen artists that did not
enter a section). One set of taken ids is shared by all the album
assignments, and the uniqueness of the chosen artists does the same for
artists, so no id is repeated. The reserve of album seeds is capped by the
size of the list (`recommendations_history_albums` accepts up to 15). A
section with only part of its content is returned with it and is not filled
with the other type; a section with nothing is left out and the next seed
does not take its place.

The same set also holds the edition keys `(artist, base title)` of every
album taken, and an album is skipped when any of its keys is taken: the first
edition that a list asks for keeps the album and the others enter no list.
The base title drops, repeatedly, the trailing groups in parentheses or
square brackets, lowercases and collapses spaces. The artist is each linked
artist of the album, without the `MPLA` prefix, plus, for an album of an
artist page, the owner of the page, because that shelf may carry no linked
artist. The editions of an album seed are taken from the start, as the seed itself
is, only if its page was found and carries an artist with an id (the key
needs an artist).

**Soundtracks are not recommended.** A title with "Soundtrack" or "OST" as a
whole word, in any case, is removed from both sources before the albums are
given out, so the next candidate takes its place in every list. By whole word,
so "Ghost" or "Lost" stay.

**The `reason` is the real seed.** Each item carries the seed it came from;
the items of a section carry the seed of the section.

**The section is stored in the `payload`, with no migration.** Each row has
`section` and, for the seed sections, `section_index` from 0 to 2. The `rank`
is global per type in the order of the screen (artists 1 to 19, albums 1 to
29, 48 rows at most). The unique index `(user_id, bucket, item_type,
item_id)` is enough because no id repeats, and the function of migration 047
inserts the payload as it comes. The read rebuilds the sections from the
rows: a row with an unknown section, an item of the wrong type for its
section, or a `seed_sections` row without a `section_index` from 0 to 2 is a
502, never an empty list.

**A set without sections is an expired set.** A row of the format of #221 has
no `section` in its payload. `POST` regenerates it even if `valid_until` is in
the future; `GET` returns the four new lists empty and the rest as stored,
without an error. The home screen can call `POST` when it opens: with a valid
set that has sections it does not call the provider.

**`artists` and `albums` stay, deprecated.** They keep their content (every
artist and every album of the set, flattened, by rank), now 19 and 29 items,
until the new home screen uses the new lists; a later issue removes them. The
deprecation is documented in `docs/api/recommendations.md`.

**Calls to the provider.** With 5 artist seeds and 3 album seeds: 5 + 19 +
3 = 27 calls (23 in ADR 025), in 8 batches of 4 (2 + 5 + 1; 7 in ADR 025).
Fewer similar artists are fetched when there are fewer seeds: the limit is 3
per section seed plus 10 (13 with 1 seed, 16 with 2), and that is a ceiling:
when a section seed has fewer than 3 candidates, the artists the rounds gave
to the others that fit neither its section nor the 10 recommended are
dropped before their pages are fetched, so no page is fetched that does not
fit a list.

Measured on 2026-10-30 from the owner's machine, the provider library
directly (no app, no Redis, no database), a new client per run (no cache), 27
calls per run (5 seed pages, 19 similar pages chosen in rounds over the
related artists of the 5 seeds, 3 albums), three sets of five seeds:

| threads | runs | total min / mean / max | p50 per call | max per call |
|--------:|-----:|-----------------------:|-------------:|-------------:|
| 1       | 3    | 7.04 / 7.24 / 7.46     | 0.239        | 0.986        |
| 4       | 6    | 2.43 / 2.52 / 2.66     | 0.258        | 0.811        |
| 6       | 3    | 1.85 / 1.93 / 2.04     | 0.256        | 0.782        |

(seconds; p50 is the mean of the p50 of each run). The worst case with 4
workers, 8 batches each paying the slowest call measured with 4 threads
(0.811 s), is 6.5 s, 1.8 times under the 12 s budget; the typical total is
2.5 s. The bars set for the issue were a total of 4 s or less with 4 threads
and a worst case of 8 s or less: both hold, so `RECOMMENDATIONS_PARALLEL_CALLS`
stays at 4 and no quantity changes.

Supply of content in those runs: a seed page had 10 related artists in every
case; a similar artist's page had between 0 and 10 albums (31 of 228 pages had
fewer than 3), and the albums of the 19 similar artists of a run were 108 to
166 distinct, enough for the 29 albums of the set; 14 of 36 album seeds
recommended nothing. With real users the lists may come out shorter when a
page has little content, which is a documented 200.

This replaces the numbers of ADR 025 that the change made old: "about 23
calls", "15 similar artists" and the worst case of 7 batches (2 + 4 + 1) and
5.4 s. The rest of ADR 025 stays: inside the request, three stages, the
budget that follows the workers, the workers that do not note images, one
executor per generation and the skip of a seed with proof.

## Alternatives rejected

- **A bucket per section.** The function of migration 047 receives a single
  bucket and deletes the user's rows before inserting, so it would need a new
  function and a new migration to keep the replacement in one transaction.
- **A `section` column.** A migration for data that the payload already
  carries, and the index would not change.
- **Removing `artists` and `albums` now.** The app's onboarding validates
  them and would break; they are removed in a separate issue.
- **Promoting the fourth seed to a section when one of the first three has
  nothing.** The sections are the first three seeds; an empty one is left out
  so a section always means the seed the user sees first.
- **Matching editions by title alone.** It would merge "Greatest Hits" of
  different artists.
- **Using the `other_versions` of the album page.** One more call per
  candidate, against the 12 s budget.
- **Removing only a fixed list of edition words ("Deluxe", "Expanded", ...)
  instead of any trailing suffix.** It does not cover "(Epilogue)", which is
  the case seen.
- **Filling a partial section with the other type.** It would hide that the
  seed has little content and break the alternation the screen expects.

## Consequences

- 27 provider calls and 8 batches instead of 23 and 7, with every generation
  of 5 seeds; fewer with fewer seeds.
- A set has up to 48 rows instead of 30, and the response carries every item
  twice (in its list and in the deprecated flat lists) until those are
  removed.
- During a deploy, a `POST` served by the old version can write a set without
  sections after a new one; the next `POST` regenerates it without an error.
- Until a `POST` regenerates it, a user with a valid set of the old format
  sees empty sections in `GET`; the client calls `POST` when it opens the
  screen.
- A section can be shorter than 6 items, or absent, when its seed has little
  content; the lists too, also because editions and soundtracks are skipped.
- A different album of the same artist that differs only in the final suffix
  is treated as an edition and only one is recommended. Suffixes with a dash,
  nested parentheses and different apostrophes are not detected.
- The soundtrack filter is by word: a non-soundtrack with the word in its
  title is excluded; "Original Score", "Music from the Motion Picture",
  "O.S.T." and "B.S.O." and the plural "Soundtracks" are not.

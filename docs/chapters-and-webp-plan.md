# Simplify chapter entry; accept WebP artwork

## Context

Two rough edges in the episode workflow:

**Chapters are tedious to enter.** `prompts._segments` asks for a Start *and* a Stop
for every chapter, then rejects anything out of order
("Entries must be chronological and must not overlap"). The stop time is redundant:
a chapter ends where the next one begins, and **neither spec requires an end time**.
Podlove Simple Chapters has no end attribute at all — `feed.py:280` correctly emits
only `start` on `psc:chapter`. Podcasting 2.0 JSON Chapters requires only
`startTime`; its optional `endTime` exists to end a chapter *early*, creating a gap
where no chapter is active. So the stop time the user types today feeds one optional
JSON field and the validator's overlap check, and nothing else.

**WebP artwork is rejected outright.** `media.IMAGE_EXTENSIONS` is
`{".jpg", ".jpeg", ".png"}`, so `termicast add ep.mp3 cover.webp` dies with
"Unsupported file type"; the import converter (`importer.py:205`) returns WebP
untouched and the strict validator then rejects it. Termicast already normalizes
audio to MP3, transcripts to WebVTT, and PNG artwork to JPEG — WebP should go the
same way. (`serverfix.py` already teaches nginx/Apache `image/webp`, so the product
is inconsistent with itself today.)

Outcome: a chapter is a start time and a title; artwork is whatever image you have,
and Termicast converts it.

## Decisions taken

- Termicast **never produces an end time**. A chapter is a start and a title, from
  the prompt through to the published `chapters/<slug>.json`. Players derive each
  boundary from the next chapter's start, which is what the spec says to do.
- An explicit `endTime` arriving from an imported feed or an external chapters JSON
  is **passed through and still validated**, so a deliberate gap (chapter ends 10:00,
  next starts 12:00) survives a round trip.
- The chapter list **auto-sorts by start**. Overlap becomes impossible by
  construction; only a duplicate start is rejected.
- A `.webp` artwork **URL** is downloaded, converted to JPEG, and hosted in the
  show's `images/` folder, with the public URL repointed at the hosted copy.
- Chapter art accepts a **local file path** as well as an HTTPS URL.

---

## Part 1 — Chapters carry only a start time

### `termicast/validation.py`

Delete `backfill_chapter_ends` (line 283) and replace it with a display-only helper,
the one place an end is still wanted — the editor's list:

```python
def chapter_ends(chapters, duration):
    """Where each chapter appears to end: its own endTime, else the next start,
    else `duration`. For display; never written to a record or a published file."""
```

It yields `None` for a trailing chapter when `duration` is missing or non-positive —
nothing to derive from, and that is not an error.

In `validate_episode`'s chapter branch (lines 240-279), make `endTime` optional:

- `startTime` nonnegative, finite, and within `duration` when a duration is known.
- Starts strictly increasing — a duplicate or out-of-order start is
  `"chapters[i] must be sorted by startTime"`. The `previous_end` overlap check goes
  away; with no ends it cannot fail.
- An **explicit** `endTime` (only ever imported) must still be finite, greater than
  its start, no greater than the next chapter's start, and within `duration`.

Leave the soundbite branch alone — soundbites genuinely need their own `duration`,
and the spec stores it.

`validate_chapter_payload` (line 291) stops calling the backfill: require a nonempty
array of objects each with `startTime`, then delegate to `validate_episode`. This
also fixes an existing trap — a feed with no `itunes:duration` currently backfills
the last chapter to `endTime == 0` and then fails its own validator.

### `termicast/publisher.py:345-348`

No change. It already writes `episode["chapters"]` through verbatim; with no ends in
the record, none reach the file.

### `termicast/importer.py`

Drop the manual backfill loop at lines 173-175 and the `backfill_chapter_ends` call
at line 525. Imported chapters keep whatever `endTime` the source JSON supplied and
nothing more — `psc:chapter` has no end attribute, so chapters imported from RSS now
carry none at all instead of a fabricated one.

### `termicast/prompts.py` — the editor (`_segments`, line 596)

`_segments` has grown into two functions wearing one coat. Keep the shared
Add/Edit/Remove/Done shell and list display; split the per-entry form into
`_soundbite_entry(old)` (today's Start/Stop/Title, unchanged) and
`_chapter_entry(old, show, episode)`:

1. `number("Start (HH:MM:SS, MM:SS, or seconds)", old.get("startTime", 0))`
2. `text("Title", required=True)` — 255-char limit as today
3. Chapter artwork — optional, HTTPS URL **or local file path** (see Part 2)
4. `text("Chapter link (optional HTTPS)")`

No stop prompt. Then:

- Drop `endTime` from the entry just added or edited — the user has retyped its
  timing, so any stored end is stale. (Records saved before this change, and
  imported ones, carry ends; untouched entries keep theirs, so an imported gap
  survives until you edit that chapter.)
- Re-sort the list by `startTime`; reject a start that equals an existing one
  ("Another chapter already starts at 00:05:00").
- After sorting, drop any remaining explicit `endTime` that now exceeds the
  following chapter's start. It can only be a stale derived value, and keeping it
  would strand the user on a validation error they cannot see the cause of.
- Write `img` and `url` **only when nonempty**. Today the editor always writes them,
  so `"img": ""` ends up in the saved record and in the published JSON.

Show computed ends in the list, using `chapter_ends`:

```
Chapters
  1  00:00:00 → 00:01:30   Cold open
  2  00:01:30 → 00:10:20   Interview
  3  00:10:20 → 00:11:40   Outro
```

`edit_field` (line 686) keeps dispatching both fields to the shell.

### Docs

- `termicast/faq.py:189-198` — "Enter chronological, nonoverlapping ranges within the
  episode duration" becomes: enter a start time per chapter; each chapter runs until
  the next one starts, and the last until the end of the episode. Worth stating why
  there is no end time, since it will look like a missing feature: neither Podlove
  Simple Chapters nor Podcasting 2.0 JSON Chapters requires one.
- `USER_GUIDE.md:195-206` and `README.md` chapter sections — same change in wording.

---

## Part 2 — WebP in, JPEG out

### `termicast/media.py`

- `IMAGE_EXTENSIONS` (line 23) += `".webp"` — `identify_files` now routes it to the
  image role, and its error message gains WebP.
- `_OTHER_TYPES` (lines 50-56) += `".webp": "image/webp"`. This flows into
  `STAGEABLE_SUFFIXES`, so an imported `.webp` URL stages under its real suffix
  instead of the `.img` fallback, and into `content_type_for`, matching the
  `image/webp` line `serverfix.py` already emits.
- `_prepare_image` (line 339): the fast path is JPEG-only, so WebP always takes the
  convert branch and lands as `<slug>.jpg` — no change needed there. The `keep`
  branch (line 356) gets an honest message: WebP cannot be kept, because it is not a
  deliverable podcast artwork format; drop `--keep-image` to convert it.
- Add an optional `pad_to=None` parameter to `_prepare_image`. When set, pad/enlarge
  to that size with `ImageOps.pad(..., color=(255, 255, 255))` — the same treatment
  `importer._convert_import_artwork` already applies at lines 224-226.
- New `install_artwork(show, source, *, stem, folder="images", kind="")`:
  - `source` is a local path or an HTTPS URL; a URL is fetched into a temp file with
    `validation._download(url, handle, validation.MAX_ARTWORK_BYTES)`, which already
    enforces HTTPS, guards redirects, and caps the size.
  - Converts through `_prepare_image` with the show's `image_preset`, `pad_to`
    `(3000, 3000)` for `kind` `"show"`/`"episode"` and `None` for `"chapter"` —
    matching the dimension rules `validation.inspect_local_artwork` enforces.
  - Installs to `asset_root(show)/<folder>/<stem>.jpg`, returns
    `(relative, asset_url(show, relative))`.

  `validation` imports no project modules, so importing it from `media` adds no
  cycle; `media` already imports `.publisher` lazily for `atomic_write`.

### `termicast/validation.py` — signalling a convertible problem

`inspect_artwork` (line 422) currently raises a flat `ValueError` for a bad format,
mode, or size. Introduce `class ArtworkNeedsConversion(ValueError)` carrying
`(format, mode, size, required_size)` and raise it for exactly those three cases.
Because it subclasses `ValueError`, every existing `except ValueError` call site
behaves as it does today.

### `termicast/prompts.py` — the artwork prompt (`_artwork`, line 544)

- A **local path** → `install_artwork`, and `artwork_url` becomes the hosted URL.
- An **HTTPS URL** → inspect as today. On `ArtworkNeedsConversion`, offer:

  ```
  Artwork at this URL is WEBP, 1400×1400. Podcast apps require JPEG or PNG.
  Termicast can download it, convert it to JPEG at 3000×3000, and host it at
  https://media.example.com/my-show/images/cover.jpg
  ```

  Accept → `install_artwork`; decline → today's error, and the prompt loops.
- Other failures (not HTTPS, download failed, Pillow missing) are unchanged.

Installed names:

| Artwork | Path |
|---|---|
| Show | `images/cover.jpg`, falling back to `cover-2.jpg`… if an episode already owns that name |
| Episode | `images/episodes/<slug>.jpg`; no slug → the `chapter_filename(guid)` stem |
| Chapter | `images/chapters/<stem>-NN.jpg` via `models.chapter_image_relative` |

No deploy work is needed: `publisher.py:257` and `:352` already fold
`local_relative(show, show["artwork_url"])` into the upload set, and
`hosting.py:211-213` already verifies it, so a locally hosted cover reaches S3 like
any other managed asset.

**Threading `show` through.** `edit_field`/`edit_menu` (lines 658, 718) don't receive
the show, so they gain `show=None` and pass it to `_artwork`. For the show form
`data` *is* the show. Pass `show=show` at the episode call sites (lines 980, 997,
1129). With `show=None` the prompt degrades to today's URL-only behavior, so
`cli.py:309` and `:489` need no change.

### Chapter art from a local file

In `_chapter_entry`, a value that isn't an HTTPS URL is treated as a path and goes
through `install_artwork(..., folder="images/chapters", kind="chapter")`.

`models.chapter_image_relative(slug, index, suffix)` (line 174) gains the no-slug
fallback stem used by `chapters_relative`. The index is the lowest one-based index
not already claimed by another chapter's `img` in this episode — re-sorting can
otherwise make two chapters want `-02`. A stale index is cosmetic (the URL lives in
the chapter record), and `rename.py:115-164` re-indexes them properly on a rename.

### `termicast/importer.py:205`

`if format_name not in ("JPEG", "PNG")` → include `"WEBP"`, with
`to_jpeg = format_name in ("PNG", "WEBP")` at line 209, so imported show, episode,
and chapter art in WebP is converted and renamed to `.jpg` by the existing path at
lines 229-233. `cli.py:36`'s review line ("PNG will be converted to JPEG") should
name the detected format instead of hardcoding PNG.

### Prompt text and help

`prompts.py:1026` ("Artwork file path (JPEG/PNG)"), `prompts.py:645` (chapter artwork
label), `PROMPT_EXAMPLES` entries `"chapter artwork"` and `"artwork file path"`,
`cli.py:693-698` (`add` help), `media.identify_files`'s error, plus the artwork
sections of `README.md` (142-151) and `USER_GUIDE.md` (179-190, 262-270).

---

## Verification

```sh
cd /opt/termicast && .venv/bin/python -m pytest
```

New/updated tests:

- `tests/test_validation.py` — a chapter list with no `endTime` validates; an
  explicit gap end is still checked against the next start and the duration;
  duplicate starts are rejected; a start beyond the duration is rejected; chapters
  validate when `duration` is unknown.
- `tests/test_publisher.py` — a start-only chapter list is written to
  `chapters/<slug>.json` with no `endTime` key; an imported explicit end is written
  through unchanged. Existing fixtures at lines 28 and 114 carry explicit ends, so
  they cover the second case; add the start-only one.
- `tests/test_prompts.py` — the chapter editor never prompts for a stop; entries
  added out of order come back sorted; `img`/`url` are absent rather than `""`.
  Extend the existing `_segments` cancel test at line 97.
- `tests/test_media.py` — `identify_files` accepts `.webp`; `_prepare_image` turns a
  WebP into `<slug>.jpg`; `--keep-image` with WebP raises; `install_artwork` works
  from a local file and from a stubbed HTTPS source.
- `tests/test_importer.py` — WebP show/episode/chapter art is converted to `.jpg` and
  passes `inspect_local_artwork` (mirror the parametrized cases at lines 129-155).
- `tests/test_cli_mime.py` / `tests/test_serverfix.py` — `.webp` maps to `image/webp`.

End-to-end, in the TUI (`.venv/bin/termicast`):

1. `termicast add ep.mp3 cover.webp` → `images/episodes/<slug>.jpg`, review shows the
   conversion note.
2. Episode review → **Add chapters / transcript** → **Chapters manually**: add three
   chapters *out of order*, entering only starts; confirm the list re-sorts and shows
   `00:00:00 → 00:01:30` style ends, and that no stop was ever asked for.
3. Give one chapter a local `.webp` for art; confirm
   `images/chapters/<slug>-NN.jpg` and the public URL in the record.
4. Publish, then check `chapters/<slug>.json` carries `startTime`/`title` and no
   `endTime`, and that `feed.xml` is unchanged in shape (`psc:chapter` never had an
   end attribute). Open the JSON in a Podcasting 2.0 player or validator to confirm
   the chapters still read correctly.
5. Show settings → Artwork URL → paste a `.webp` URL; accept the conversion offer;
   confirm `images/cover.jpg` exists and `artwork_url` points at it.
6. `termicast validate` and `termicast doctor` stay clean.

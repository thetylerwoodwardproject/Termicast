# Termicast

Termicast is a Linux terminal application for managing multiple podcasts and
generating static RSS feeds and chapter files. It uses numbered menus, Rich
messages, SQLite state, and namespace-aware XML handling. Run it interactively
to create or import shows and publish or schedule episodes; use `publish-due`
from cron to publish scheduled episodes.

## Quickstart

1. Follow [Install](#install), then run `termicast`.
2. Create or import a podcast. Creation asks for essentials; use **Edit field**
   at review for additional settings such as funding, podroll, and language.
3. Open the podcast and choose **New episode**. Enter a title, description,
   and hosted MP3 URL. Probe the media; only missing size/duration values need entry.
4. Review, optionally add chapters/transcript or edit more details, then choose
   **Publish now** or **Schedule** and confirm.

The podcast menu is **New episode**, **Episodes**, **Podcast settings**, **Tools**,
and **Switch podcast / Back**. In **Episodes**, select an episode to edit, or filter
by published/scheduled status. **Tools** contains repair, regeneration, CSV, and
migration guidance. Hosting setup is described under [State And Hosting](#state-and-hosting).

## Check And Repair And Import Review

Open a podcast and choose **Tools → Check And Repair** for an offline, read-only scan
of saved episode records, the current feed, and local resources referenced by
the generated feed. Absent optional chapters and transcripts are normal.
The separate `validate` command remains read-only.

Repair displays original/replacement titles, offers all or selected title fixes
or regeneration only, and previews the output paths and public URLs. A final
confirmation creates a private backup of saved state, feeds, and chapters **before**
changes. It then regenerates published content and revalidates, reporting remaining
issues. Scheduled episodes, including overdue ones, are not released. Title fixes
retain GUIDs, publication dates, and scheduling state. A stale scan is rejected;
failed output writes leave durable dirty intent for recovery.

Missing feeds and chapters with saved chapter data can be regenerated. For other
missing local references, supply an existing local source file to copy to the
previewed destination. Repair also allows a corrected **new** output directory;
it does not move the old directory's media. The public base URL stays the same.
Use Podcast settings or the episode editor to correct hosting URLs. External URLs
are not fetched by the scan, and missing audio, artwork, transcripts, or chapter
payloads cannot be reconstructed from URLs alone. Local recovery copies bytes;
it does not transcode or verify that replacement audio matches enclosure metadata.

Episode titles have a hard 60-character limit. Interactive editing offers the
literal first 60 characters with trailing whitespace removed, or editing again.
Saving an existing overlong title also requires this choice. RSS and CSV imports
preview every original/replacement pair and require confirmation before truncating;
there is no ellipsis or automatic word-boundary shortening.

RSS import has one optional asset-review entry point, defaulting to skip. Select
an episode to add chapters from local/HTTPS JSON or enter them manually; add a
transcript from local/HTTPS UTF-8 WebVTT. Missing optional resources produce no
warnings, empty asset files, or import failures. Broken *linked* optional resources
offer retry, a replacement HTTPS URL, or explicit skip, which removes the broken
reference from the generated feed while retaining the original XML in saved state.
Failed partial downloads are removed before retry. VTT checks require a WEBVTT
header and at least one timed cue; they are not a complete WebVTT conformance check.

**Edit episode → Add optional assets** offers the same additions later. Added VTT
content is retained with the record and written on publication, so regeneration
can restore it. Scheduled episode assets are written when released. Chapter JSON
is normalized into saved chapter records. Unknown RSS extensions and imported GUIDs
remain preserved; supported fields explicitly changed or skipped are replaced.

Termicast writes files locally and always downloads supported assets when importing
a podcast. It does not upload media or generated files,
transcode audio, run a web server, configure TLS, or provide server
authentication. Hosting and public access are your responsibility.

## Install

Use Python 3.11 or newer on Linux. Filesystem locking uses Linux/Unix `fcntl`;
these instructions are not for native Windows. On current Debian/Ubuntu releases,
install the interpreter and matching venv support:

```sh
sudo apt update
sudo apt install python3 python3-venv
```

From the project directory:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
termicast --help
```

The editable install includes Rich, lxml, Pillow, and timezone data. The `dev`
extra adds pytest. Run the tests with `python -m pytest`.

Optional media duration inspection uses `ffprobe`, supplied by FFmpeg on
Debian/Ubuntu:

```sh
sudo apt install ffmpeg
ffprobe -version
```

Without `ffprobe`, or when inspection fails, enter the MP3's exact byte length
and duration manually. FFmpeg is not used to transcode or publish audio.

## State And Hosting

By default, state lives in `~/.local/share/termicast/termicast.db`. Set
`TERMICAST_HOME` to a directory to change it, or use the global `--data-dir`
option, which overrides that environment variable for the invocation:

```sh
export TERMICAST_HOME=/srv/termicast/state
termicast
```

Equivalent explicit invocation:

```sh
termicast --data-dir /srv/termicast/state
```

Put `--data-dir` **before** subcommands such as `publish-due`, `validate`, or
`backup`. Use the same absolute
state directory for interactive sessions, validation, cron, and recovery.
The database contains show settings, preserved import XML, scheduled episodes,
and publication status. Its adjacent lock file is `termicast.db.lock`.

Each show has a separate output directory and an absolute HTTPS **base URL**.
The base URL is the public directory URL, not the feed URL. For example:

| Setting | Example |
| --- | --- |
| Output directory | `/srv/www/podcasts/my-show` |
| Base URL | `https://podcasts.example.com/my-show` |
| Public feed | `https://podcasts.example.com/my-show/feed.xml` |
| Public chapter file | `https://podcasts.example.com/my-show/chapters/<safe-id>.json` |

```text
/srv/www/podcasts/my-show/
  feed.xml
  audio/
  chapters/
    <safe-id>.json
  images/
    episodes/
    show/
    chapters/
  transcripts/
  .termicast.lock
```

The media directories are created during import. New episodes may still reference
already-hosted HTTPS assets. Chapter JSON is generated for saved published episodes
with chapters. UUID chapter filenames retain their exact case; arbitrary imported
IDs use SHA-256 filenames, never raw IDs as filesystem paths.
It uses Podcasting 2.0 JSON Chapters version `1.2.0`, with `startTime`,
`endTime`, and `title`, plus optional `img` artwork and `url` links. The RSS references it as
`application/json+chapters` and also includes Podlove PSC start markers.

Configure your static HTTPS host to map the base URL to that output directory,
serve XML and JSON with appropriate content types, and allow public reads of
the feed and chapter files. The Termicast account needs write access; the web
server needs directory traversal and read access. Keep state outside the web
root and do not expose lock or temporary dotfiles. If hosting elsewhere, deploy
chapter files before the referencing feed yourself; Termicast's local atomic
writes do not make an external transfer atomic.

## Numbered Workflows

Every numbered menu offers **B Backup**, **F FAQ**, and **X Exit**, including selectors,
field editors, review screens, and Yes/No confirmations, without changing the
numbered choices. Both utilities return to the same menu and preserve your
in-memory form. Backup includes only saved state, not unsaved edits; neither
utility saves or confirms a form. X cleanly exits the entire application with
status 0, discarding unsaved forms even from deeply nested menus. At free-text
prompts, B, F, and X are ordinary text, not shortcuts.

Read the FAQ without opening or creating a database:

```sh
termicast faq
```

### 1. Create A Podcast

1. Run `termicast --data-dir /srv/termicast/state` and choose **3. Create podcast**.
2. Enter title, description, author, owner email, artwork URL, and explicit flag.
3. Set an IANA timezone such as `America/New_York` or `UTC`, choose an Apple
   primary category, and enter the output directory and HTTPS base URL.
4. At review, **Edit field** exposes all additional settings, including owner name,
   website, copyright, language, podcast type, categories, funding, and podroll.
   A funding label requires a funding URL. Up to eight podroll entries are supported;
   each requires a UUID `feedGuid`. The default language is `en` and type is `episodic`.
   If the website is empty, the base URL supplies the RSS channel link.
5. Review, edit fields if needed, and choose **Save**. This saves the show and
   generates `feed.xml`. Verify its public URL through your host.

At text prompts, Enter retains the current value; `-` clears an optional field.
Supplied URL fields must use absolute HTTPS URLs without embedded credentials.
Artwork must be JPEG or PNG in RGB mode. Show artwork must be square at
1400-3000 pixels (3000 ideal); episode artwork must be exactly 3000x3000.
Optional remote inspection produces a warning when unavailable and requires
manual verification. Import performs mandatory checks on downloaded local files;
chapter artwork is checked for JPEG/PNG RGB, without imposing episode dimensions.

A new show's Podcasting 2.0 GUID is UUIDv5 derived from its initial feed URL.
It stays fixed after creation, including when the base URL changes. The separate
internal show UUID displayed in the podcast selector is used by `validate`.

### 2. Import And Migrate A Podcast

1. Back up the original feed, then choose **2. Import existing podcast**.
2. First choose a **new, nonexistent** media/output directory and its public
   HTTPS directory URL. `feed.xml` is written normally inside that directory.
3. Supply a local XML path, such as `/srv/imports/original.xml`, or the current
   HTTPS feed URL. Imports are limited to 10 MiB; DTD declarations are rejected.
4. Review extracted settings, correct any validation errors, and set the
   timezone explicitly if needed: imports start at `UTC`.
5. Save to download supported assets with per-file byte/speed progress, then
   validate the replacement. Check the publicly hosted feed, chapter
   links, artwork, transcripts, and media before switching listeners.
6. For RSS.com migration, ask RSS.com to configure a permanent HTTP **301
   redirect** from the old feed URL to the new `.../feed.xml`. Follow the
   provider's migration procedure and verify the redirect afterward. Do not
   shut down old hosting before the redirect is active.

Import reads rather than edits the source and stores its exact original XML
bytes in SQLite. Generated output is reserialized XML, not a byte-for-byte
copy. Existing items, episode GUIDs, unknown extension tags, value recipients,
locations, and TXT records are retained unless explicitly managed/replaced.
Unchanged imported settings retain their original elements; edited settings
replace the corresponding managed fields. The generator, self-link, and build
date are updated. An existing show GUID is retained; a missing one is generated
from the import identity source.

Import always downloads enclosures, show/episode artwork, Podcasting 2.0 chapter
JSON, chapter artwork, and transcripts. Identical source URLs are downloaded once;
downloaded audio is probed locally once, never fetched again for probing. Positive
feed duration is retained if ffprobe cannot supply it. Missing or invalid metadata
that cannot be validated aborts import rather than publishing an incomplete show.

Files stream into a private staging directory beside the destination, with size,
time, disk-space, and artwork checks. A failed download or check removes staging.
Only a complete asset set is installed, then the original XML and episode records
are saved and the feed generated. If database saving fails after asset installation,
the installed directory remains for inspection; no existing files are removed.
If feed publication fails after saving, retry `publish-due`.

Rehosted URLs are persisted as exact substitutions in show settings. Original XML
bytes stay in SQLite, not in the public directory. Known resource attributes and
matching URL text/attributes are rewritten, including chapter images inside JSON.
Unrelated unknown XML is retained. Import does not crawl arbitrary URLs embedded
in HTML, unknown extensions, alternate enclosures, or linked websites. Those may
still depend on original hosting; inspect your source before retiring it. Import
URLs/assets must use HTTPS. Unsupported chapter payloads or invalid assets abort
the operation; there is no partial-import or skip-download mode.

Imported episodes are listed, editable, and exportable alongside native episodes.
Older databases with template-only imports expose those episodes too; editing an
episode stores its managed record while retaining untouched XML. They are not
retroactively downloaded on startup: import the source into a new show/output
directory to rehost old link-only imports and load their remote chapter JSON.
Duplicate GUIDs
within a feed are rejected as ambiguous. Episode IDs need not be UUIDs and are scoped
to a show; the database upgrades its old global-GUID key automatically under lock.
Back up state before upgrading. Termicast cannot redirect an external RSS.com URL
from your VPS. The provider controlling that URL must do it.

### 3. Publish An Episode

1. Choose **1. Open podcast**, select a show, then **1. New episode**.
2. Enter a title of at most 60 characters and a description of at most 4,000
   raw characters, including HTML and URLs. Supply an already-hosted HTTPS MP3
   URL and optionally probe it. Detected size/duration appear in review; only missing
   values require manual entry. Duration accepts seconds, `MM:SS`, or `HH:MM:SS`.
3. At review, **Edit details / more options** exposes the optional link, `full`/`trailer`/`bonus` type, positive episode and
   season numbers, explicit flag, artwork, HTTPS VTT transcript URL, and up to
   ten comma-separated keywords.
4. Optionally add soundbites through the details editor, or choose **Add chapters /
   transcript** for local/HTTPS chapter JSON, manual chapters, or a VTT file. Enter
   start and stop times as seconds, `MM:SS`, or `HH:MM:SS`. The menus require
   chronological, nonoverlapping ranges; stops must follow starts and ranges
   must fit within the episode duration. Soundbite titles are limited to 128
   characters; durations outside the recommended 15-120 seconds produce a
   warning. Chapter titles are limited to 255 characters. Chapters also accept
   optional HTTPS artwork (`img`) and destination links (`url`).
5. At the final review choose **1. Edit field**, **2. Publish now**, **3.
   Schedule**, or **4. Cancel**. Confirm publication to write chapters and the
   feed immediately. Each episode gets a permanent UUID; managed published
   items are prepended, newest first, before preserved imported items.

### 4. Schedule And Inspect Episodes

1. Choose **3. Schedule** at episode review and enter a future ISO date/time in
   the show's configured timezone, such as `2027-06-15T09:00:00`.
2. Review both local and UTC times and confirm scheduling once. Timestamps are stored in UTC;
   episode lists display them in the show's current timezone. Changing the
   timezone changes the display, not an already scheduled instant.
3. During a DST fall-back, include an explicit offset to resolve the repeated
   wall time. In `America/New_York`, `2027-11-07T01:30:00-04:00` and
   `2027-11-07T01:30:00-05:00` select different instants. Ambiguous times without
   offsets, nonexistent spring-forward times, and offsets inconsistent with
   the configured zone are rejected. Use **Publish now** for immediate release.
4. Use **2. Episodes** and its **Filter episodes** option to inspect status, local publication time, and
   episode GUID. Scheduled episodes remain absent from the feed until published.

Choose **2. Episodes**, then select an episode to edit any existing native or imported
episode. All modeled content fields are editable, including nested chapters,
chapter artwork, soundbites, enclosure metadata, and transcript URLs. GUIDs remain
permanent. Unchanged imported fields retain their original XML, including extension
elements and multiple original representations. Explicitly editing a managed field
replaces its corresponding elements.

Only unreleased episodes offer **Reschedule** and **Publish now**. Published items
cannot have their publication instant changed or become scheduled again, including
through CSV. Pending edits are validated together before saving. Cancelling or X
discards the form; a failed filesystem publication may already have durable intent
and should be recovered using `publish-due`. There is still no unscheduled-draft or
unschedule state; the scheduled filter shows saved unpublished records.

### Episode CSV

Use **Tools → Import episode CSV** or **Tools → Export episode CSV** in an open show, or:

```sh
termicast --data-dir /srv/termicast/state export-csv <show-id> /srv/private/episodes.csv
termicast --data-dir /srv/termicast/state export-csv <show-id> /srv/private/scheduled.csv --filter scheduled
termicast --data-dir /srv/termicast/state import-csv <show-id> /srv/private/episodes.csv
```

Replace `<show-id>` with the internal show UUID. Export filters are `all` (default),
`published`, and `scheduled`. Exports contain saved records, not unsaved forms, use
UTF-8 and owner-only permissions, and are refused inside configured public roots.
Treat exports as private: they can contain unpublished metadata and schedules.
Spreadsheet applications may interpret user-controlled text as formulas; import
CSV columns as text when reviewing untrusted content in a spreadsheet.

The root `episode_template.csv` is a header-only template. Columns are:

```text
title,description,link,mp3_url,artwork_url,transcript_url,guid,length,duration,episode_type,episode_number,season_number,explicit,keywords,soundbites,chapters,published_at,timezone,status
```

- `guid` is optional for new rows and is generated automatically. Existing imported
  IDs are exported exactly, including non-UUID values.
- Merge uses GUID within the selected show. If GUID is blank, an exact `mp3_url`
  match identifies an existing episode; more than one match is rejected. No match
  creates a new episode. Duplicate target GUIDs anywhere in one batch are rejected.
- Import never deletes episodes absent from the CSV. A supplied GUID not found in
  the show creates a new record. Omitted columns retain existing values; blank
  content cells clear optional fields and fail validation for required fields.
- `length` is positive integer bytes; `duration` is positive seconds. `explicit`
  accepts true/false or 1/0. Optional episode/season numbers are positive integers.
- `keywords`, `chapters`, and `soundbites` are JSON arrays inside CSV-quoted cells.
  Nested chapter metadata, including `img`, `url`, and unknown JSON entry keys, is
  retained. CSV exports all modeled episode fields, not raw unknown XML extensions;
  those remain in the preserved template/database.
- `published_at` is ISO date/time; `timezone` is an IANA zone. Export includes a
  local offset and zone. Import rejects ambiguous/nonexistent wall times or offsets
  inconsistent with that zone. Blank publication time retains an existing instant;
  a new row defaults to now. Blank status retains an existing status; a new row is
  scheduled for a future instant and otherwise published.
- The entire CSV is parsed, matched, validated, and rendered before any database
  changes. Matching and preflight run under the same writer lock as the commit.
  One SQLite transaction saves all intent and marks the show dirty. Chapters and
  feed are then atomically replaced; new published statuses commit after output.
  Filesystem failure does not roll back intent: fix the cause and run `publish-due`
  to recover the complete batch. This is not an all-files atomic transaction.

CSV does not download or probe URLs: it manages metadata for already-hosted assets.
It cannot reschedule or unpublish an already-published episode.

### 5. Run Scheduled Publication

Run manually with the same state directory:

```sh
/opt/termicast/.venv/bin/termicast --data-dir /srv/termicast/state publish-due
```

For an installation at `/opt/termicast`, add this to the publishing user's
crontab using `crontab -e`:

```cron
*/5 * * * * /opt/termicast/.venv/bin/termicast --data-dir /srv/termicast/state publish-due
```

Replace both absolute paths for your installation. Cron does not need venv
activation. Use an account with access to the same state and output directories,
and monitor cron output/errors. All due episodes are processed when the command
runs, so five-minute cron means release at the next run, not an exact-time
background service. It also retries dirty show outputs. Failures return status
1; successful runs return 0 and report the number published. Other shows are
still attempted if one show's publication fails.

### 6. Validate And Maintain

Validate every managed local feed, or one by its **internal show UUID** from
the podcast selector/settings review, not its podcast GUID or an episode GUID:

```sh
termicast --data-dir /srv/termicast/state validate
termicast --data-dir /srv/termicast/state validate <show-uuid>
```

Replace `<show-uuid>` with the actual UUID; do not type the angle brackets.
Validation reports per-feed errors and exits 1 for failures, unreadable feeds,
or an unknown show ID. It exits 0 when all selected feeds pass, including when
there are no managed feeds. Checks cover local XML structure and selected
channel/item fields, GUID duplicates, enclosures, dates, and field limits.
Imported items can trigger errors even though import preserves them.

Validation is offline: it does not fetch the public feed, check chapter JSON,
verify remote media/artwork/transcript content, or guarantee directory-provider
acceptance. Test public HTTPS access separately.

In an open show, **3. Podcast settings** saves changes and regenerates the feed;
**Tools → Regenerate feed** rebuilds already-published content but does not release
due scheduled episodes. Use `publish-due` for those. **Tools → Migration guidance**
shows hosting and redirect instructions.

At startup, **4. Forget podcast** removes the show's database record, preserved
template, and managed episode records after confirmation. It does **not** delete
feed or chapter files or remove them from public hosting.

## Recovery And Backups

Publication uses a database lock and an output-directory lock to coordinate
Termicast writers. Use one consistent state directory for a given output;
separate databases are not a way to safely co-manage the same show.

The write order is deliberate:

1. Persist the episode and intended publication time in SQLite.
2. Write complete chapter files using same-directory temporary files, file
   flush/fsync, atomic replacement, and directory fsync.
3. Replace the complete `feed.xml` the same way, after its chapters exist.
4. Commit published statuses and clear the show's dirty flag in SQLite.

This is **not a multi-file transaction**. Each replaced file is complete, but a
crash may leave new chapters with an old feed, or an updated feed before the
database says "published". After fixing the underlying problem, rerun
`publish-due` with the same state directory. Persisted due intent/dirty state
allows retries; rendering by episode GUID replaces content rather than appending
duplicates. Do not recreate an episode with a new GUID to retry it. Local atomic
replacement does not guarantee atomic remote deployment or simultaneous reader
snapshots across several files.

### Create A Backup

Choose **B Backup** from any numbered menu, then enter a destination directory
or press Enter for the default: `db.path.parent/backups`, the `backups` directory
beside `termicast.db`. From the command line, the destination is optional:

```sh
termicast backup
termicast backup /srv/private/termicast-backups
termicast --data-dir /srv/termicast/state backup
termicast --data-dir /srv/termicast/state backup /srv/private/termicast-backups
termicast --data-dir /srv/termicast/state backup /srv/private/termicast-backups --include-media
```

Backup covers **all saved shows**, not only the open show. It holds the database
filesystem lock while taking a SQLite online backup and copying local outputs,
coordinating with Termicast writers using the same database. Unsaved show or
episode form edits remain in memory and are **not** backed up. This lock does
not coordinate unrelated programs or separate databases modifying the outputs.

The resulting ZIP archive has private owner-only permissions (`0600`) and
contains:

- `manifest.json`, with `created_at`, `database`, `shows` including saved
  settings and original output paths, and `missing_files`.
- `termicast.db`, including show settings, preserved import XML, managed
  episodes, schedules, and publication status.
- `outputs/<internal-show-id>/feed.xml` and each show's available
  `outputs/<internal-show-id>/chapters/*.json` files.

Missing outputs are reported and recorded in `missing_files`; backup still
succeeds because the database is sufficient to regenerate managed outputs
later. This is not a general copy of output directories. By default, MP3s, artwork,
and transcripts are not copied. Add `--include-media` to include local files under
the allowlisted `audio/`, `images/`, and `transcripts/` directories; dotfiles and hidden
directories are excluded, symlinks rejected, and the manifest records this option.
Unrelated root files and other output subdirectories are not swept into the archive.
Backup never downloads remote assets. Keep private state and backups outside public
roots, and back up assets outside these directories separately.

Backup refuses destinations inside any configured public output directory and
refuses symbolic-link outputs. Keep backups and state outside **every** public
web root, including unrelated roots Termicast cannot detect. Archives contain
private management data and unpublished schedules. Mode `0600` is not encryption;
protect copied archives and their storage too. Do not copy a live SQLite file
blindly; if making a separate filesystem backup, pause cron and close the app
before copying matching state and outputs.

### Restore Manually

There is no automatic restore command. **Stop cron and close all Termicast
sessions before restoring**, and keep a safety copy of current state and outputs.

1. Extract a trusted ZIP into a **private staging directory**, not a web root.
   Inspect `manifest.json`, the recorded paths and show IDs, and `missing_files`.
2. Restore `termicast.db` into the intended state directory and map
   `outputs/<internal-show-id>/` back to each original output path from the
   manifest. These internal IDs are not podcast or episode GUIDs.
3. If paths changed, open the app against the restored database and update each
   show's output directory in Podcast settings before regenerating there.
   Change its base URL only if the public URL changed; configure hosting and
   deployment separately.
4. Leave cron stopped. Use **Regenerate feed** to rebuild missing managed feed
   and chapter files without releasing scheduled episodes. Referenced media and
   other external assets still need to be available at their URLs.
5. Check ownership and permissions, run `validate` with the restored state
   directory, and separately verify public feed, chapter, and asset URLs. Keep
   the database, manifest, staging directory, and archive private.
6. Inspect pending schedules and publication status **before** re-enabling cron
   or running `publish-due`. An older backup may restore now-overdue episodes or
   stale publication state; due episodes will publish. Resolve unintended
   pending releases before restarting automation.

Feed files alone cannot restore schedules and management state. Restoring a
backup does not restore remote hosting configuration, redirects, or URL assets.

## Inspection Limits

- Media probing first tries HTTP `Content-Length`, then streams a temporary
  download to disk with per-file progress and runs local `ffprobe`. The default
  media limit is **2 GiB**. The total per-file download deadline defaults to **3600
  seconds**, distinct from the **30-second** socket stall timeout. Disk space is
  checked before and during disk downloads, retaining a 16 MiB safety reserve;
  temporary probe downloads are cleaned up on success, error, or cancellation.
  A known oversized file skips temporary probing; imports reject oversized assets.
  Advertised size mismatches are rejected rather than accepting truncated media.
- Set `TERMICAST_MAX_MEDIA_BYTES`, `TERMICAST_DOWNLOAD_TIMEOUT`, and
  `TERMICAST_STALL_TIMEOUT` before starting the process to override these limits.
  Values are bytes and seconds respectively. The stall setting also bounds local
  ffprobe execution. The overall deadline is checked between reads; a blocking read
  can take up to the socket timeout. Feed loading retains its 10 MiB/15-second
  socket limit; artwork, transcript and chapter-JSON downloads are capped at 20 MiB.
- Manual fallback only supplies metadata. It does not upload a local MP3,
  convert another format to MP3, fix a broken URL, or prove the supplied size,
  duration, or audio format is correct. Enclosures are emitted as `audio/mpeg`;
  verify the hosted file yourself before publication.
- Artwork inspection is optional and capped at 20 MiB. If skipped or unavailable,
  verify dimensions manually. Transcript validation checks HTTPS URL syntax,
  not whether the resource is valid WebVTT. URL syntax checks do not establish
  reachability or public accessibility.
- There is no built-in hosting, deployment, authentication, or credential
  configuration for protected resources. Use publicly accessible HTTPS assets;
  supply trusted import and inspection URLs, since HTTPS validation is not a
  private-network access filter.

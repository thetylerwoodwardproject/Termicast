"""Standalone FAQ text for CLI and menu rendering with Rich Markdown."""

FAQ = """# Termicast FAQ

## How do I get started?

Run `termicast`, then use the numbered menus to create or import a podcast.
Set its local output directory, public HTTPS base URL, and timezone. Save the
show, then open it to publish or schedule episodes. Termicast writes `feed.xml`
and chapter JSON locally; you must arrange hosting and deployment yourself.
The base URL is a directory URL, not the URL ending in `feed.xml`.

## Where can I find Backup and FAQ?

Every numbered menu offers **B Backup**, **F FAQ**, and **X Exit**, including selectors,
field editors, review screens, and Yes/No confirmation menus. Numbered choices
keep their existing numbers. These utilities return to the same menu and
preserve your in-memory form; they do not save or confirm it. At free-text
prompts, B, F, and X are ordinary text, not shortcuts. X exits cleanly from any
numbered menu and discards unsaved forms. Enter keeps the current
value and `-` clears an optional field.

Run `termicast faq` to read this FAQ without creating or opening a database.

## Where is my data stored?

The default database is `~/.local/share/termicast/termicast.db`. Set
`TERMICAST_HOME` to another state directory, or use the global `--data-dir`
option, which takes precedence. Put it before a subcommand:

```sh
termicast --data-dir /srv/termicast/state
termicast --data-dir /srv/termicast/state publish-due
termicast --data-dir /srv/termicast/state validate
termicast --data-dir /srv/termicast/state backup
```

Use the same state directory for the app, cron, validation, and recovery.
Each show has its own output directory. Keep the state directory and backups
outside all public web roots; the database contains private management data,
including owner details, original import XML, and unpublished schedules.

## How do I make a backup, and what does it include?

Choose **B Backup** in any numbered menu. Enter a destination directory, or
press Enter for the default `backups` directory beside the database. The
command-line equivalent accepts an optional destination directory:

```sh
termicast backup
termicast --data-dir /srv/termicast/state backup /srv/private/termicast-backups
```

Backup covers **all saved shows**, not just the open podcast. It uses SQLite's
online backup API while holding the database filesystem lock to coordinate
with Termicast writers, and copies saved local feed and chapter outputs under
that lock. It does not turn unsaved form edits into database records. You can
continue editing afterward, but those in-memory changes are not in the backup.

The result is a ZIP archive with owner-only permissions (`0600`), containing:

- `manifest.json`: `created_at`, `database`, `shows` with saved settings and
  original output paths, and a `missing_files` list.
- `termicast.db`: saved settings, preserved import XML, managed episodes, and
  publication state.
- `outputs/<internal-show-id>/feed.xml` and
  `outputs/<internal-show-id>/chapters/*.json` for each show's available files.

Missing outputs are reported and listed in `missing_files`; they do not make
the backup fail. The database is sufficient to regenerate managed feed and
chapter outputs later. Backup does not fetch linked MP3s, artwork, transcripts,
or other URL assets, and is not a general copy of entire output directories.
Add `backup --include-media` to copy local `audio/`, `images/`, and `transcripts/`
as well, excluding hidden files/directories and rejecting symlinks. Other folders
are not copied. Retain original import files and back up external media separately.

Backup refuses destinations inside any configured public output directory and
refuses symbolic-link outputs. Choose a real, private directory outside every
web root, including unrelated web roots that Termicast cannot know about.
Permissions are not encryption: protect archive copies and storage separately.
The lock coordinates Termicast writers using this database, not unrelated
programs modifying outputs or separate databases managing the same files.

## How do I restore a backup?

Restore is manual; there is no restore command. **Stop cron publication and
close all Termicast sessions first**, and keep them stopped during restoration.
Keep a safety copy of the current state and outputs before replacing anything.

1. Extract a trusted archive into a private staging directory, never directly
   into a public web root. Inspect `manifest.json`, its paths, show IDs, and
   `missing_files` before installing files.
2. Restore `termicast.db` to the intended state directory. Map each
   `outputs/<internal-show-id>/` directory back to the original output path in
   the manifest. Internal show IDs are not podcast or episode GUIDs.
3. If locations changed, reopen the app against the restored state and update
   each show's output directory in Podcast settings before regenerating there.
   Update the base URL only if its public location also changed. Termicast
   does not move files or configure hosting for you.
4. With cron still stopped, use Regenerate feed for missing managed outputs.
   Regeneration rebuilds already-published content without releasing scheduled
   episodes. Referenced external assets still need to exist at their URLs.
5. Check ownership and permissions, run `validate`, and test public feed,
   chapter, media, artwork, and transcript URLs separately. Keep the restored
   database, manifest, and archive private.
6. Inspect pending schedules and publication status **before re-enabling cron
   or running `publish-due`**. An old backup can contain now-overdue episodes
   or stale publication state; `publish-due` releases due episodes. Resolve
   unintended pending releases before restarting automation.

Feed files alone cannot recover schedules and management state. Restoring
local files does not restore remote hosting, redirects, or media assets.

## Is a scheduled episode a saved draft?

Scheduling persists an episode in SQLite but keeps it out of the feed until
publication. Run `publish-due` manually or from cron to release due episodes;
the interactive app is not a background scheduler. A five-minute cron releases
at the next run, not necessarily at the exact scheduled second.

Choose **Episodes**, then select an episode to edit native or imported episodes, including scheduled
ones. Only unreleased episodes can be rescheduled or published now. Published
publication instants cannot change. There is no unscheduled-draft or unschedule
state. Cancelling a new episode discards the in-memory form. Backup preserves saved
schedules, not unsaved forms. CSV import/export is available in the show menu and
through `import-csv`/`export-csv`; see README and `episode_template.csv` for columns.

## How do timezones and daylight saving time work?

Set an IANA timezone such as `America/New_York` or `UTC` for each show. Imports
start at UTC; review this explicitly. Enter a future ISO date/time in that
zone. Termicast shows local and UTC times before confirmation and stores UTC.
Changing the show's timezone changes the display, not a saved release instant.
For repeated fall-back times, include the correct explicit offset, such as
`2027-11-07T01:30:00-04:00` in New York. Ambiguous times without an offset,
nonexistent spring-forward times, and offsets inconsistent with the zone are
rejected. Use Publish now for immediate release.

## Does import preserve identity and metadata? Can I migrate from RSS.com?

Import reads a local XML file or HTTPS feed without editing the source. It
stores the exact original XML bytes; generated feeds are reserialized, not
byte-for-byte copies. Existing episode GUIDs, unknown extension metadata,
value recipients, locations, and TXT records are retained unless explicitly
managed or replaced. Unchanged imported settings retain their elements;
editing settings replaces corresponding managed fields. The generator,
self-link, and build date are updated.

An existing show GUID is retained; a missing one is generated from the import
identity source. New shows get a UUIDv5 GUID based on their initial feed URL,
which remains fixed after creation. Internal show IDs are separate identifiers.
Imported episodes are listed, editable, and exportable. Import always downloads
supported audio, artwork, chapter JSON, chapter images, and transcripts into a new
media directory selected before the feed source. Original XML stays in SQLite;
exact resource URL substitutions and managed field edits preserve unrelated XML.
Arbitrary HTML/extension URLs and alternate enclosures are not crawled.

Choose a new output directory and HTTPS base URL, validate and publicly test
the replacement, then ask RSS.com or the current provider to configure a
permanent HTTP 301 redirect from the old feed URL. Follow that provider's
procedure and verify the redirect before retiring old hosting. Termicast
cannot redirect an external provider's URL or configure your web server.

## What hosting do I need?

Arrange publicly readable HTTPS hosting for feeds, chapters, MP3s, artwork,
and transcripts. Configure the server's directory mapping, TLS, content types,
and permissions yourself. Termicast does not upload, deploy, transcode, run a
web server, or configure authentication. URL fields require absolute HTTPS
URLs without embedded credentials, but syntax checks do not prove access.
When deploying generated files elsewhere, transfer chapters before the feed
that references them. Local atomic writes do not make remote deployment atomic.

## What if media probing or artwork inspection fails?

Install FFmpeg for optional `ffprobe` duration inspection. Probing tries HTTP
Content-Length and then a temporary download capped at 2 GiB with time limits;
known larger files skip duration probing. Missing tools or slow/unavailable
metadata can require manual entry of exact positive MP3 byte length and
duration (seconds, `MM:SS`, or `HH:MM:SS`). Manual values do not fix a URL or
verify the audio format; enclosures are emitted as `audio/mpeg`.

Downloads stream to disk with progress, free-space checks, and cleanup. Configure
`TERMICAST_MAX_MEDIA_BYTES`, `TERMICAST_DOWNLOAD_TIMEOUT` (default 3600 seconds),
and `TERMICAST_STALL_TIMEOUT` (default 30 seconds) before starting Termicast.
Imported audio is probed locally once, not downloaded again for probing.

Optional artwork inspection is capped at 20 MiB. Artwork must be JPEG/PNG RGB;
show art must be square at 1400-3000 pixels (3000 ideal), episode art exactly
3000x3000. Import validates downloaded artwork locally and aborts on invalid
assets. Supply trusted import and inspection URLs;
HTTPS validation is not a private-network access filter.

## How do chapters and transcripts work?

Managed published chapters generate Podcasting 2.0 JSON Chapters version
`1.2.0` in `chapters/<safe-id>.json`, linked as `application/json+chapters`.
UUIDs keep their filenames; opaque imported GUIDs use SHA-256 filenames. Chapter
artwork (`img`) and links (`url`) are editable and included in chapter JSON.
RSS also includes Podlove PSC start markers. Enter chronological, nonoverlapping
chapter and soundbite ranges within the episode duration, with stops after
starts. Chapter titles allow 255 characters; soundbite titles allow 128, with
a warning outside the recommended 15-120 second soundbite duration.

New transcripts are links to already-hosted HTTPS WebVTT files. Feed import
downloads linked transcripts, and `backup --include-media` includes local copies.
Termicast does not generate or inspect transcript content. Verify the file and
its public URL yourself.

## What does validate check, and what are its limits?

Run `termicast validate` for all local feeds, or `termicast validate <show-uuid>`
with an internal show ID from the selector/settings review. Put `--data-dir`
before `validate` if needed. Checks cover local XML structure and selected
channel/item fields, duplicate GUIDs, enclosures, dates, and field limits.
Episode titles allow 60 characters and descriptions 4,000 raw characters,
including HTML and URLs; episodes allow up to ten keywords.

Validation exits 1 on failures, unreadable feeds, or unknown show IDs, and 0
when all selected feeds pass, including when there are none. Preserved imported
items can still fail checks. Validation is offline: it does not check chapter
JSON, fetch public feeds or assets, verify transcript content, or guarantee
podcast-directory acceptance. Test public HTTPS access separately.
    """

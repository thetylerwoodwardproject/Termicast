"""Standalone FAQ text for CLI and menu rendering with Rich Markdown."""

FAQ = """# Termicast FAQ

## How do I get started?

Run `termicast`, then use the numbered menus to create or import a podcast.
Set its local output directory, public HTTPS base URL, and timezone. Save the
show, then open it to add episodes from local media files. Termicast writes
`feed.xml` locally and can deploy assets and the feed to S3-compatible storage.
The base URL is the public root of the show's files, not a URL ending in
`feed.xml`.

## How do I add an episode?

From the show menu choose **New episode**, or use the command line:

```sh
termicast add <show-id> episode.mp3 artwork.jpg transcript.vtt --slug s02ep042
```

Audio is required; artwork and transcript are optional and identified by their
file type. Termicast prepares the files (audio presets, image optimization),
prompts for title and details, and publishes immediately or schedules the
episode. Media files must be on the machine running Termicast; running on a VPS
does not give it access to files on your desktop.

Smaller files help listeners on slower connections and reduce hosting
bandwidth. Standard audio uses MP3 at 128 kbps and 44.1 kHz. Compact artwork
targets JPEG below 500 KB. You can change these presets or keep compatible
original files.

## Where can I find Backup and FAQ?

Every numbered menu offers **B Backup**, **F FAQ**, and **X Exit**. Numbered
choices keep their existing numbers. These utilities return to the same menu
and preserve your in-memory form. X exits cleanly and discards unsaved forms.
Enter keeps the current value and `-` clears an optional field. Run
`termicast faq` to read this FAQ without creating or opening a database.

## Where is my data stored?

The default database is `~/.local/share/termicast/termicast.db`. Set
`TERMICAST_HOME` to another state directory, or use `--data-dir`. Use the same
state directory for the app, cron, validation, and recovery. Each show has its
own output directory. Keep state and backups outside all public web roots.

## How do I make a backup, and what does it include?

Choose **B Backup** or run:

```sh
termicast backup
termicast --data-dir /srv/termicast/state backup /srv/private/backups
termicast --data-dir /srv/termicast/state backup --include-media
```

Backup covers **all saved shows**. It holds the database lock while taking a
SQLite online backup and copies saved feed and chapter files. The result is a
private (`0600`) ZIP with `manifest.json`, `termicast.db`, and
`outputs/<internal-show-id>/...`. Missing outputs are reported, not fatal.
Add `--include-media` to include prepared `audio/`, `images/`, and
`transcripts/` (including chapter images and scheduled assets). Backups are not
encryption; protect copied archives too.

## How do I restore a backup?

Restore is manual. Stop cron and close all Termicast sessions first. Extract a
trusted archive into a private staging directory, restore `termicast.db`, map
output directories back from `manifest.json`, regenerate missing outputs with
**Regenerate feed**, then verify with `validate` and `doctor`. Inspect pending
schedules before re-enabling cron, since an old backup can contain now-overdue
episodes. After restore, use the retained local assets to redeploy with
`termicast deploy <show-id>`.

## Is a scheduled episode a saved draft?

Scheduling persists an episode in SQLite but keeps it out of the feed until
publication. Run `publish-due` manually or from cron to release due episodes:

```cron
*/5 * * * * /opt/termicast/.venv/bin/termicast --data-dir /srv/termicast/state publish-due
```

A five-minute cron releases at the next run, not at the exact scheduled second.

## How do timezones and daylight saving time work?

Set an IANA timezone per show. Enter future ISO date/time in that zone; Termicast
shows local and UTC times before confirmation and stores UTC. Include an explicit
offset to resolve repeated fall-back times. Ambiguous or nonexistent times are
rejected.

## How do audio and artwork presets work?

Audio presets: **Standard** (MP3 128 kbps, 44.1 kHz) and **Music** (MP3 192 kbps,
44.1 kHz). Mono/stereo are preserved; multichannel input is rejected. Suitable MP3s
at or below the target bitrate are passed through, never uprated. Image presets:
**Compact** (JPEG below 500 KB) and **Detail** (JPEG below 1 MB). Already-suitable
JPEGs are preserved; transparency is flattened onto white; oversize square covers
are reduced to 3000x3000; undersized art is never upscaled. Per-episode overrides
and keep-original options are available in review and via CLI flags.

## What hosting do I need?

Arrange publicly readable HTTPS hosting for the feed, chapters, MP3s, artwork, and
transcripts. Termicast supports **local web server** (files served directly from
the output directory) or **S3-compatible storage** (files uploaded with `s4cmd`).
For S3, configure credentials outside Termicast in `~/.s3cfg` and set the endpoint,
bucket, and optional prefix in the **Hosting** submenu. `feed.xml` is uploaded last,
after its assets, and is never referenced until its assets are in place. Deploy with
`termicast deploy <show-id>` and verify with `termicast doctor <show-id>`.

## What does doctor check?

`termicast doctor [show-id]` is read-only. It checks that required tools (FFmpeg,
ffprobe, s4cmd when S3 is used) are installed, that the public feed and media are
reachable with correct MIME types, and that the public feed matches the local feed.
It does not probe bucket writes or deletes.

## Does import preserve identity and metadata? Can I migrate?

Import reads a local XML file or HTTPS feed without editing the source, preserving
GUIDs, unknown extension metadata, value recipients, locations, and TXT records.
Assets are downloaded once. Run hosting verification (`doctor`) before switching
listeners, then ask the current host to configure a permanent HTTP 301 redirect from
the old feed URL to the new `.../feed.xml`. Termicast cannot redirect an external
provider's URL or configure your web server, and it never deletes remote objects.

## How do chapters and transcripts work?

Managed chapters generate Podcasting 2.0 JSON Chapters in `chapters/<slug>.json`,
linked as `application/json+chapters`. Transcripts are WebVTT files in
`transcripts/<slug>.vtt`. Enter chronological, nonoverlapping ranges within the
episode duration. Chapter titles allow 255 characters; soundbite titles allow 128.

## What does validate check, and what are its limits?

`termicast validate [show-id]` checks local XML structure and selected field
limits, duplicate GUIDs, enclosures, and dates. It is offline: it does not fetch
public resources. Use `doctor` for public accessibility and MIME checks.
"""

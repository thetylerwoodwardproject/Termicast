# Termicast

Self-hosted Podcast 2.0 RSS manager. No server process needed -- generates a
static feed.xml that nginx serves directly.

## How it works

- `cli.py` -- prompt-based management CLI. Writes feed.xml to disk after every change.
- `generate.py` -- tiny script cron runs daily to publish scheduled episodes automatically.
- nginx serves feed.xml and the media folder as static files. Nothing is ever running.

## Requirements

- Python 3.8+
- nginx
- certbot
- `pip3 install -r requirements.txt` (requests for feed transfers; Rich for terminal presentation)

## Setup

### 1. Get the files onto your server

Clone from GitHub:

```bash
git clone https://github.com/thetylerwoodwardproject/termicast /opt/termicast
```

Or copy manually:

```bash
scp -r termicast/ user@yourserver:/opt/termicast
```

### 2. Install dependencies

```bash
pip3 install -r requirements.txt
apt install nginx certbot python3-certbot-nginx
```

### 3. Create the media directory

```bash
mkdir -p /opt/termicast/media
```

### 4. First run

The first time you run the script it walks you through a setup wizard asking
for your domain, show title, description, and author name.

```bash
cd /opt/termicast
python3 cli.py
```

### 5. Symlink feed.xml and media into /var/www/html

nginx serves files from `/var/www/html` by default. Symlinking keeps your
files in one place while letting nginx find them without any path gymnastics.

```bash
ln -s /opt/termicast/feed.xml /var/www/html/feed.xml
ln -s /opt/termicast/media /var/www/html/media
```

### 6. Configure nginx

```bash
cp /opt/termicast/nginx.conf.example /etc/nginx/sites-available/audio.example.com
```

Edit the file and replace `audio.example.com` with your actual domain. Then enable it:

```bash
ln -s /etc/nginx/sites-available/audio.example.com /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
```

> **Important:** Remove the default nginx site (`default`) or it will intercept
> requests before your config and you'll get 404s on the ACME challenge.

Test that it's working before running certbot:

```bash
curl http://audio.example.com/feed.xml
```

### 7. Get your SSL certificate

```bash
certbot --nginx -d audio.example.com
```

Certbot will automatically update your nginx config with HTTPS settings and
schedule automatic certificate renewal. Don't run this until the curl test
in the previous step returns your feed.

### 8. Set up the cron job

This is what publishes scheduled episodes automatically. Without it, scheduled
episodes only go live the next time you manually run `cli.py`.

```bash
crontab -e
```

Add this line (runs daily at 8am -- adjust to match when your episodes go live):

```
0 8 * * * cd /opt/termicast && python3 generate.py >> /var/log/termicast.log 2>&1
```

Your feed is now live at `https://audio.example.com/feed.xml`.

---

## Usage

```bash
cd /opt/termicast
python3 cli.py
```

feed.xml is regenerated automatically after every action in the CLI.

The CLI uses a Rich-powered broadcast-console theme: grouped numbered menus,
a local-status dashboard, responsive episode tables, and animated fetch spinners.
Downloads show progress, transfer speed, and estimated time remaining. Menu
numbers are unchanged; type a number and press Enter. Enter accepts a marked
default, and Ctrl+C exits. This is a prompt-based interface, not an arrow-key TUI.

The dashboard reports local episode counts, scheduled episodes, mirror source,
and whether `feed.xml` exists. It does not claim the public feed is reachable.
Narrow terminals wrap episode titles and omit the filename/ID columns.

Colors are automatically disabled when output is redirected, `NO_COLOR` is
set, or `TERM=dumb`. Redirected output and custom sync loggers stay plain and
do not animate. To keep the styled menus but turn off motion:

```bash
TERMICAST_REDUCED_MOTION=1 python3 cli.py
```

Any value (including an empty value) enables this setting. Reduced motion
prints final progress only; `NO_COLOR` alone does not disable motion.

Primary feeds list episodes newest-first by publication timestamp normalized
to UTC, rather than by the date string's spelling or timezone offset.

### Adding an episode

1. Drop your MP3 (and optional transcript) into `./media/`
2. Run `python3 cli.py` and select "Add episode"
3. Fill in the prompts
4. Set a future pub date to schedule, or a past/current date to publish immediately

### Scheduling an episode

1. Add the episode with a future pub date (e.g. `2026-06-02 08:00`)
2. The episode is saved to the database but filtered out of feed.xml
3. At 8am on June 2nd, cron runs `generate.py` and the episode appears in the feed

### Importing from an existing feed

Select "Import from RSS feed" and paste your current feed URL. The script will:

- Import all show metadata
- Download all audio files into `./media/`
- Download episode artwork
- Download transcripts if available in the feed

Existing files are skipped on re-import so it is safe to run more than once.

Imported episodes are stored newest-first by UTC publication timestamp. Episodes with
missing or invalid publication dates are skipped with a warning.

### Manually regenerating the feed

```bash
python3 generate.py
```

---

## Chapters

Chapters follow the [Podcast Index JSON format](https://github.com/Podcast-Index-org/podcast-namespace/blob/master/chapters/jsonChapters.md).
Each chapter supports a start time, title, optional link URL, and optional image URL.
Chapter JSON files are written to `./media/` automatically when the feed regenerates.

## Mirroring an external feed

"Mirror external feed" in the CLI keeps a raw-XML, newest-first copy of
another host's feed on this server -- a failover if anything happens to your
primary host.

Unlike "Import from RSS feed" (a one-way *migration* that flattens the feed
into this tool's data model), the mirror never re-generates the XML. The
source XML is preserved except for URL rewrites and item order, so every tag
survives exactly as published: `podcast:guid`, `podcast:value` splits, `podcast:podroll`,
`podcast:funding`, `psc:chapters`, multiple categories, and any future
Podcasting 2.0 tags. The only changes are:

- Asset URLs (enclosures, transcripts, chapters JSON and its images,
  artwork, chapter images, the XSL stylesheet) are rewritten to local copies
  downloaded into `./mirror/media/`.
- The `atom:link rel="self"` is pointed at the mirror's own URL.
- Direct channel items are ordered newest first by their `pubDate` UTC instant.
  Equal dates retain source order; missing or invalid dates come last in source order.

Failed downloads keep their original source URL (a working remote link beats
a broken local one). Downloads retry three times with backoff; if an OP3 URL
still fails, the downloader tries its direct origin. Downloads use temporary
files, check nonempty content and declared transfer length, and only publish
completed files. The same downloader and colored progress bars are used by
RSS import. Repeat imports skip known episode GUIDs; failed audio downloads
skip the episode so the next import can retry.

If RSS.com's Triton dynamic-audio stream fails and advertises a signed,
same-host static-audio fallback, the downloader tries that fallback first.
This can omit dynamically inserted ads; it does not discard length checks
or accept a truncated stream as a completed download.

Syncs are incremental: already-downloaded assets are skipped, and failed
assets are retried on the next sync. Standalone sync exits nonzero on partial
failure, including chapter-image failures. Run sync as a user with write
access to both `mirror/` and `mirror/media/`. Colors are suppressed for
redirected output, `NO_COLOR`, and `TERM=dumb`.

Ordering moves exact raw item blocks without reserializing them, preserving
namespaces, unknown tags, CDATA, and formatting.

Set it up in the CLI, then serve and schedule it:

```bash
ln -s /opt/termicast/mirror /var/www/html/mirror
crontab -e   # add:
0 9 * * * cd /opt/termicast && python3 mirror.py >> /var/log/termicast-mirror.log 2>&1
```

The mirror is then live at `https://audio.example.com/mirror/feed.xml`,
with all its media under `https://audio.example.com/mirror/media/`.

### Promoting the mirror to primary

If the mirrored host goes down for good (or you're leaving it), "Promote
mirror to primary" in the mirror menu adopts the mirror into *your* feed so
this server becomes the real host and you can keep publishing new episodes.
It works entirely from the local mirror -- the old host does not need to be
online. Promotion:

- preserves `podcast:guid` and every episode GUID **exactly**, so the show
  keeps its identity in the Podcast Index ecosystem and apps don't
  re-download episodes
- imports value/valueRecipient splits, funding, license, medium, podroll,
  copyright, locations, and all categories into your show settings, and
  feed.xml re-emits them
- hardlinks audio, transcripts, chapters, and artwork from `./mirror/media/`
  into `./media/` (no extra disk space)
- merges episodes by GUID -- anything already in your database is untouched,
  and re-running promote is a no-op
- sets `podcast:locked` to `no` so directories will accept the feed URL
  change, and regenerates feed.xml immediately

### What a mirror can NOT do: the feed URL problem

Be honest with yourself about what happens on the day the primary host
dies. Every listener's app is subscribed to the **old host's feed URL** --
a domain you do not control. There is no redirection: nothing on this
server can make `media.rss.com/yourshow/feed.xml` (or any other host's URL)
point here. After promoting, you still have to move the audience:

- **Apple Podcasts Connect, Spotify, YouTube Music, etc.** -- log in to each
  dashboard and change the show's feed URL to yours. Apps that subscribe
  through those directories will follow automatically on their next refresh.
- **podcastindex.org** -- update the feed URL there too. Because promotion
  preserves `podcast:guid`, Podcasting 2.0 apps can verify the new URL is
  the same show.
- **Anyone subscribed directly to the raw RSS URL** (old-school podcatchers,
  RSS readers) will **not** follow automatically. They silently stop getting
  new episodes until they manually resubscribe to your URL. There is nothing
  any backup tool can do about this -- only the owner of the old domain
  could redirect, and if they're gone, they can't.

The real fix is to do this *before* the disaster: put your canonical feed
URL on a domain you own (most hosts support a feed redirect while they're
still alive), so listeners are subscribed to *your* URL and a failover
becomes a server-side switch instead of a directory-by-directory scramble.

## Episode links ("From This Episode")

Each episode can carry a webpage link, emitted as the item's `<link>` element.
Apple Podcasts reads links from your episode data (the `<link>` element and any
`<a href>` in the show notes), fetches OpenGraph metadata from the destination,
and surfaces them as "From This Episode" cards. It is not a `podcast:` namespace
tag -- it is the standard RSS `<link>` plus whatever links live in your notes.

- **Add episode** prompts for an optional "Episode webpage link".
- **Edit episode -> Basic info** shows the current link; press Enter to keep it,
  type a new URL to replace it, or type `-` to clear it.

Note: importing from another feed carries over that feed's `<link>` (and the
links baked into its show notes). If you migrated from a host like rss.com and
still see its page under "From This Episode," clear or replace the episode link
here and strip any leftover links from the show notes.

## Transcripts

Place SRT, VTT, or TXT files in `./media/` and enter the filename when adding
or editing an episode. The correct MIME type is set automatically based on the
file extension.

## Running the tests

```bash
pip3 install -r requirements.txt pytest
pytest
```

Tests using the `data_dir` fixture run against an isolated scratch directory.
The fixture monkeypatches storage, feed, mirror, promotion, and CLI media paths,
so those tests never write to your real database, media, or generated feeds.

## OP3 Tracking

Enclosure URLs are automatically prefixed with `https://op3.dev/e/` when you
enable it during the setup wizard. Stats appear at
`https://op3.dev/show/{your-podcast-guid}` once downloads start coming in.

You can disable OP3 by clearing the `op3Prefix` field in "Edit show settings".

---

## File structure

```
termicast/
  cli.py              # Prompt-based management CLI
  generate.py         # Cron script -- regenerates feed.xml
  feed.py             # RSS XML generator
  mirror.py           # Cron script -- raw XML mirror, newest first
  promote.py          # Adopt the mirror as the primary feed (failover)
  store.py            # JSON data layer
  requirements.txt    # requests, Rich
  tests/              # pytest tests and isolated data directory fixture
  mirror/             # Mirrored feed.xml + downloaded assets (auto-created)
  podcast.json        # Episode database (auto-created on first run)
  feed.xml            # Generated RSS feed (symlinked into /var/www/html)
  media/              # Audio files, transcripts, chapter JSON
  nginx.conf.example  # Reference nginx config
  README.md
```

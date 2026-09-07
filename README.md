# Termicast

Self-hosted Podcast 2.0 RSS manager. No server process needed -- generates a
static feed.xml that nginx serves directly.

## How it works

- `cli.py` -- run this to manage episodes. Writes feed.xml to disk after every change.
- `generate.py` -- tiny script cron runs daily to publish scheduled episodes automatically.
- nginx serves feed.xml and the media folder as static files. Nothing is ever running.

## Requirements

- Python 3.8+
- nginx
- certbot
- `pip3 install -r requirements.txt` (`requests` for RSS import/mirroring;
  `Pillow` for the AI pipeline's soundbite waveform videos)
- `ffmpeg` + `ffprobe` (only needed for the AI pipeline: audio conversion,
  soundbite clip extraction, and waveform video rendering)
- A local Whisper install (e.g. `pip3 install openai-whisper`, or
  whisper.cpp) if you use local transcription instead of the OpenAI cloud API

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
pip3 install requests
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

### Manually regenerating the feed

```bash
python3 generate.py
```

---

## AI Episode Pipeline

There are three ways to get an episode into your feed, and none of them
replace each other -- pick whichever fits a given episode:

1. **Add episode** -- fully manual, described above.
2. **Process New Episode (w/ AI)** -- described in this section:
   drop raw audio + artwork in a folder, and Whisper + Claude/OpenAI generate
   everything else.
3. **Mirror / Promote** -- adopting another host's feed verbatim, described
   below.

### How it works

1. Drop your episode's WAV/FLAC/MP3 and a PNG artwork file into a folder
   (nothing else needs to be in there).
2. Run `python3 cli.py` -> "Process New Episode (w/ AI)" and point it
   at that folder. It will:
   - Convert the audio to MP3 (the only output format Termicast publishes).
   - Transcribe the full episode to VTT with Whisper (cloud or local,
     depending on your config).
   - Ask Claude or OpenAI to generate 3 titles (you pick the best one), an
     SEO-ready HTML description, 10 keywords, chapter names with start/end
     times, and up to 5 soundbite-worthy moments.
   - Cut each soundbite to its own MP3, and render a matching MP4: a
     1080x1920 (9:16) video with a solid RED/GREEN/BLUE background (for
     chroma-keying over other footage in Canva for Shorts/Reels/TikTok) and
     a thin, centered, hand-drawn-looking waveform line that reacts to the
     clip's audio -- flat during silence, pulsing organically with speech.
   - Generate 3 social media posts (280 chars + hashtags).
   - Schedule the episode -- either at a date/time you enter, or
     automatically on your configured recurring weekly slot (see below).
   - Add the episode to podcast.json and regenerate feed.xml.
   - Write an editorial markdown file with everything generated, named
     `YYMMDD_Episode-Title.md`, into `./editorial/`.

### Configuring the pipeline

Run "Configure AI pipeline" in the CLI, or edit `pipeline.json` directly.
It controls:

- **Transcription**: `cloud` (OpenAI's hosted Whisper API) or `local` (a
  command template you provide for whisper.cpp / openai-whisper / etc, run
  on this server).
- **LLM provider**: `claude` or `openai`, plus which model to use.
- **Prompts**: the tone/style instructions sent to the LLM for titles,
  description, keywords, chapters, soundbites, and social posts -- edit
  these to match your show's voice and any house rules.
- **Soundbite rules**: how many to generate, min/max clip length, and the
  maximum *combined* length across all of them (default 5 clips, 180s total).
- **Waveform video**: RED/GREEN/BLUE background, the waveform's hex color,
  resolution/fps, or turn video generation off entirely (MP3-only soundbites).
- **Recurring publish schedule**: e.g. "every Tuesday at 05:00 UTC" -- when
  enabled, newly processed episodes are automatically queued onto the next
  open weekly slot instead of asking for a date each time. Batch-processing
  several episodes in a row spreads them one-per-week automatically.

API keys are **never** stored in pipeline.json -- only the *name* of the
environment variable to read them from (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY` by default). Export them in your shell/systemd unit/crontab
before running the pipeline.

### Recurring schedule + cron

The recurring slot feature decides *which* future date/time a processed
episode gets; the existing `generate.py` cron job (see step 8 of Setup)
is what actually flips it live once that time arrives. If you're using a
tight recurring slot (e.g. "Tuesday at 05:00"), run cron hourly rather than
daily so episodes go live promptly instead of waiting for the next daily run:

```
0 * * * * cd /opt/termicast && python3 generate.py >> /var/log/termicast.log 2>&1
```

### Editing a processed episode

"Edit episode" works on any episode regardless of how it was created. For
episodes that went through the AI pipeline, it also offers AI-pipeline
options so you can redo any single piece -- pick a different generated
title, regenerate the description/keywords/chapters/soundbites/social posts,
or just refresh the editorial markdown file after a manual tweak -- alongside
the regular manual fields.

## Chapters

Chapters follow the [Podcast Index JSON format](https://github.com/Podcast-Index-org/podcast-namespace/blob/master/chapters/jsonChapters.md).
Each chapter supports a start time, title, optional link URL, and optional image URL.
Chapter JSON files are written to `./media/` automatically when the feed regenerates.

## Mirroring an external feed

"Mirror external feed" in the CLI keeps a verbatim, self-contained copy of
another host's feed on this server -- a failover if anything happens to your
primary host.

Unlike "Import from RSS feed" (a one-way *migration* that flattens the feed
into this tool's data model), the mirror never re-generates the XML. The
source feed is kept byte-for-byte, so every tag survives exactly as
published: `podcast:guid`, `podcast:value` splits, `podcast:podroll`,
`podcast:funding`, `psc:chapters`, multiple categories, and any future
Podcasting 2.0 tags. The only changes are:

- Asset URLs (enclosures, transcripts, chapters JSON and its images,
  artwork, chapter images, the XSL stylesheet) are rewritten to local copies
  downloaded into `./mirror/media/`.
- The `atom:link rel="self"` is pointed at the mirror's own URL.

Failed downloads keep their original source URL (a working remote link beats
a broken local one) and are retried on the next sync. Syncs are incremental:
already-downloaded assets are skipped.

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

## OP3 Tracking

Enclosure URLs are automatically prefixed with `https://op3.dev/e/` when you
enable it during the setup wizard. Stats appear at
`https://op3.dev/show/{your-podcast-guid}` once downloads start coming in.

You can disable OP3 by clearing the `op3Prefix` field in "Edit show settings".

## Advanced Podcast 2.0 settings

"Advanced Podcast 2.0 settings" in the CLI covers the show-level tags that
aren't part of the basic setup wizard:

- **Lock/Unlock** (`podcast:locked`) -- lock your feed once you're settled
  on this host to prevent another platform from importing/claiming it;
  unlock temporarily if you're migrating hosts.
- **TXT records** (`podcast:txt`) -- arbitrary domain-verification or other
  strings, with an optional `purpose` attribute.
- **Funding URL** (`podcast:funding`) -- a support/funding link and button text.
- **Podroll** (`podcast:podroll`) -- recommend other shows by their Podcast
  Index `feedGuid` (look it up at podcastindex.org) and/or `feedUrl`. The
  CLI walks you through it; 8 entries is a practical sane limit.
- **Creator location** (`podcast:location`) -- where the show is produced.

Standard iTunes/Apple Podcasts ("podcast 1.0") tags -- title, author,
subtitle, summary, explicit, artwork, categories, owner, type
(episodic/serial, set in "Edit show settings"), duration, episode/season
numbers, and episode type -- are all emitted automatically and don't need
separate configuration.

---

## File structure

```
termicast/
  cli.py              # Management CLI -- run this to manage episodes
  generate.py         # Cron script -- regenerates feed.xml
  feed.py             # RSS XML generator
  mirror.py           # Cron script -- verbatim mirror of an external feed
  promote.py          # Adopt the mirror as the primary feed (failover)
  store.py            # JSON data layer
  pipeline_config.py  # AI pipeline config (pipeline.json) load/save/defaults
  transcribe.py       # Whisper transcription (cloud API or local shell-out)
  llm_client.py       # Claude/OpenAI text generation for episode metadata
  audio_tools.py      # ffmpeg conversion/clipping + waveform video rendering
  pipeline.py         # AI pipeline helpers: scheduling, slugs, editorial .md
  vtt_utils.py        # Minimal WebVTT parsing shared by the pipeline
  mirror/             # Mirrored feed.xml + downloaded assets (auto-created)
  editorial/          # Per-episode .md files from the AI pipeline (auto-created)
  pipeline_work/      # Scratch dir for in-progress AI pipeline runs (auto-created)
  podcast.json        # Episode database (auto-created on first run)
  pipeline.json       # AI pipeline config (auto-created on first use)
  feed.xml            # Generated RSS feed (symlinked into /var/www/html)
  media/              # Audio files, transcripts, chapter JSON, soundbites
  requirements.txt    # Python dependencies (requests, Pillow)
  nginx.conf.example  # Reference nginx config
  README.md
```

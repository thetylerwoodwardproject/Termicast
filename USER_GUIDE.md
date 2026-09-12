# Termicast — User Guide

Termicast is a simple program for running a podcast from your own server. It
turns your audio files into a ready-to-publish podcast feed, handles your
episode artwork and transcripts, and keeps everything organized. You don't need
to edit XML by hand.

This guide explains how to use it, step by step, in plain language.

---

## 1. The basics, in one minute

Termicast works like a series of numbered menus. You pick a number, press
Enter, and follow the prompts.

**Three things to remember:**

- Every menu has **B**, **F**, and **X** at the bottom.
  - **B** = back up your saved data
  - **F** = read the FAQ
  - **X** = exit (unsaved changes are thrown away)
- At a text box, **press Enter** to keep the current value, and type **`-`**
  to clear an optional field.
- Nothing is published until you explicitly choose **Publish now** or
  **Schedule** and then confirm.

A "podcast" in Termicast is one show with its own folder and public web
address. You can manage several shows at once.

---

## 2. Installing

You need a Linux server with Python 3.11+ and FFmpeg.

```sh
sudo apt install python3 python3-venv ffmpeg
cd /opt/termicast
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

Then start it any time with:

```sh
termicast
```

---

## 3. The main menu

When you run `termicast`, you see:

```
1. Open podcast
2. Import existing podcast
3. Create podcast
4. Forget podcast
5. Quit
```

| Option | What it does |
| --- | --- |
| **1. Open podcast** | Open a show you've already set up |
| **2. Import existing podcast** | Bring in a podcast you already host somewhere else |
| **3. Create podcast** | Start a brand-new show |
| **4. Forget podcast** | Remove a show from Termicast (keeps your files) |
| **5. Quit** | Exit |

---

## 4. Creating your first podcast

Choose **3. Create podcast**. Termicast asks for the essentials:

- **Title**, **description**, **author** — what listeners see.
- **Owner email** — the contact shown in the feed.
- **Artwork URL** — a square image (1400–3000 px is ideal).
- **Explicit** — Yes or No.
- **Timezone** — e.g. `America/New_York` or `UTC`.
- **Category** — pick from the Apple podcast list.
- **Output directory** — a folder on your server, e.g. `/srv/www/my-show`.
- **Base URL** — the public web address of that folder, e.g.
  `https://podcasts.example.com/my-show`.

At the review screen you can choose **Edit field** to fill in optional extras
(website, language, funding, OP3 metrics, etc.). When it all looks right, choose **Save**.

That's it — your show is created and its feed file (`feed.xml`) is written.

---

## 5. Adding an episode (the main thing you'll do)

Open your show, then choose **1. New episode**.

Termicast will ask for your audio file. You'll type the file path:

```
Audio file path (mp3, wav, flac, m4a, ogg, opus):
```

Then it asks (optional):

- **Cover artwork** — a JPEG or PNG image for the episode.
- **Transcript** — a `.vtt` subtitles file, if you have one.

Next you may enter a **season number** and **episode number** (both optional).
Termicast suggests a name like `s02ep042` for the episode's files — you can
accept it or type your own.

Termicast then **prepares your files automatically**:

- Audio is converted to a clean MP3 (128 kbps by default).
- Artwork is optimized to a small JPEG.
- Everything is named and placed in the right folders.

You'll see a summary of the results, then you enter the **title** and
**description**. On the review screen:

```
1. Edit details / more options
2. Publish now
3. Schedule
4. Cancel
5. Add chapters / transcript
```

- **2. Publish now** — makes the episode live immediately.
- **3. Schedule** — choose a future date and time to release it.
- **5. Add chapters / transcript** — add chapter markers or a transcript.

That's the whole episode flow.

> **Tip:** your audio and image files must be on the *same machine* as
> Termicast. If Termicast runs on a VPS, it can't see files on your laptop —
> upload them to the server first.

---

## 6. Scheduling an episode

Choose **3. Schedule** at the review screen and enter a future date/time in
your show's timezone, for example:

```
2027-06-15T09:00:00
```

Termicast shows both the local and UTC time for you to confirm. The episode is
saved but stays out of the public feed until it's due.

Scheduled episodes go live when `publish-due` runs. This is usually set up as a
cron job that runs every few minutes:

```cron
*/5 * * * * /opt/termicast/.venv/bin/termicast --data-dir /srv/termicast/state publish-due
```

(Ask whoever set up your server to add that if it isn't there.)

---

## 7. Importing an existing podcast

If you already publish a podcast somewhere else, choose
**2. Import existing podcast**. Termicast:

1. Reads your current feed (a URL or an XML file you've downloaded).
2. Downloads your episodes, artwork, and transcripts.
3. Keeps your episode IDs and details intact.
4. Generates a new feed at your chosen location.

If your current feed uses OP3 metrics (enclosure URLs prefixed with
`https://op3.dev/e/`), Termicast detects that and enables **OP3 podcast metrics**
automatically. You can turn it off later under **Edit field** in the settings
review.

If JPEG or PNG artwork needs RGB conversion or resizing, Termicast shows its format, color
mode, and dimensions. Choose **Convert this image**, **Automatically convert
remaining artwork in this import**, or **Cancel import**. Auto mode lasts only
for the current import and covers both color conversion and resizing. Transparency
is flattened onto white. Episode images with incorrect dimensions and show images
outside the required square 1400–3000 pixel range are resized to 3000×3000.
Proportions are preserved, with white padding for non-square images; smaller
images are enlarged. JPEG/PNG format is preserved. This
also works for archive imports and converts only the staged copy.

After importing, check that the new feed is publicly reachable, then ask your
old host to set up a **301 redirect** from the old feed URL to the new one.
Termicast can't do that redirect for you — your old host must.

---

## 8. Hosting (where your files live)

One thing stays the same no matter what: **your feed file (`feed.xml`) always
stays on your own web server.** It's written to the show's output folder and
served from your base URL. Only the big media files (audio, artwork,
transcripts, chapters) can optionally move to cloud storage.

Under **4. Hosting** in your show's menu:

| Option | What it does |
| --- | --- |
| **1. Configure hosting** | Choose local hosting or S3-compatible storage |
| **2. Deploy** | Push your media files to your hosting |
| **3. Deploy (dry run)** | Show what would be uploaded, without doing it |
| **4. Hosting checks (doctor)** | Check your feed and files are publicly reachable |
| **5/6. Nginx/Apache snippet** | A paste-in MIME-type config for your web server |
| **7. Migration guidance** | Instructions for switching from another host |

**Local hosting** is simplest: your web server serves everything — feed and
media — from the output folder.

**S3 storage** (Amazon S3, Linode, etc.) keeps your feed on your web server but
moves the audio and artwork into a bucket, which is usually cheaper for big
files. You'll set two addresses:

- **Base URL** — where your web server serves `feed.xml`.
- **Asset base URL** — the public address of your bucket (e.g.
  `https://my-bucket.us-east-1.linodeobjects.com/my-show`).

Put your S3 credentials in a file called `~/.s3cfg` on the server (never in
Termicast). Termicast only stores the bucket name and addresses.

When setting up S3, you'll also be asked: **"Keep a local copy of media after
it's uploaded to S3?"**

- **No** (default): once your audio and artwork are in the bucket, the local
  copies are removed. Only `feed.xml` stays on your server.
- **Yes**: Termicast keeps a copy on your server too, for redundancy and
  backup purposes.

---

## 9. Backing up your data

Press **B** at any menu to make a backup. It saves your settings, episodes, and
schedules into a private ZIP file.

To include your audio and artwork too, use the command line:

```sh
termicast backup --include-media
```

Keep backups somewhere private, outside your public web folders.

---

## 10. Getting help

Press **F** at any menu for the FAQ, or run:

```sh
termicast faq
```

---

## 11. Command cheat sheet

You can also skip the menus for common tasks:

```sh
termicast                                   # the interactive menus
termicast add <show-id> episode.mp3         # add an episode from files
termicast add <show-id> audio.mp3 art.jpg transcript.vtt --slug s02ep042
termicast deploy <show-id>                  # push files to hosting
termicast doctor <show-id>                  # check hosting
termicast validate <show-id>                # check the feed file
termicast publish-due                       # release due episodes
termicast backup                            # make a backup
```

> **What's `<show-id>`?** It's the internal ID shown next to your show's name
> in the "Open podcast" list. It looks like a long number-letter code.

---

That's everything you need to get started. Create your show, add an episode,
and hit **Publish now**.

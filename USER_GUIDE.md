# 📻 Termicast — User Guide

Termicast is a simple program for running a podcast from your own server. It
turns your audio files into a ready-to-publish podcast feed, handles your
episode artwork and transcripts, and keeps everything organized. You don't need
to edit XML by hand.

This guide explains how to use it, step by step, in plain language.

## 📑 Contents

- [🧩 1. The basics, in one minute](#-1-the-basics-in-one-minute)
- [🔧 2. Installing](#-2-installing)
- [🧭 3. The main menu](#-3-the-main-menu)
- [🎬 4. Creating your first podcast](#-4-creating-your-first-podcast)
- [🎧 5. Adding an episode](#-5-adding-an-episode-the-main-thing-youll-do)
- [⏰ 6. Scheduling an episode](#-6-scheduling-an-episode)
- [📦 7. Importing an existing podcast](#-7-importing-an-existing-podcast)
- [🌐 8. Hosting (where your files live)](#-8-hosting-where-your-files-live)
- [💾 9. Backing up your data](#-9-backing-up-your-data)
- [❓ 10. Getting help](#-10-getting-help)
- [📋 11. Command cheat sheet](#-11-command-cheat-sheet)

---

## 🧩 1. The basics, in one minute

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

## 🔧 2. Installing

You need a Linux server (Debian or Ubuntu) with Python 3.11+ and FFmpeg. The
easiest way to get everything set up is one command. Paste this into a terminal
and press Enter:

```sh
curl -fsSL https://raw.githubusercontent.com/thetylerwoodwardproject/Termicast/main/install.sh | bash
```

It asks for your password when it needs to, then installs the tools Termicast
needs, copies the program to `/opt/termicast`, and sets it up so you can just
type `termicast`. You can run the same command again later to update to the
latest version.

If you'd rather do it by hand, here are the individual steps:

<details>
<summary><strong>Manual install</strong></summary>

First, copy the project onto your computer. If you don't have `git`, install it
with `sudo apt install git`, then run:

```sh
git clone https://github.com/thetylerwoodwardproject/Termicast.git termicast
cd termicast
```

This creates a folder called `termicast` and downloads the program into it.
Then install everything it needs:

```sh
sudo apt install python3 python3-venv ffmpeg
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'

# Make `termicast` work in any terminal (no activation needed)
mkdir -p ~/.local/bin
ln -s "$PWD/.venv/bin/termicast" ~/.local/bin/termicast
```

> [!NOTE]
> Installing into `/opt` (or any folder owned by root) by hand? Take ownership
> first so `pip` can write its files:
>
> ```sh
> sudo chown -R "$USER:$USER" /opt/termicast
> ```

</details>

Then start it any time with:

```sh
termicast
```

If it isn't found, log out and back in so `~/.local/bin` gets added to your
`PATH`. For a shortcut that only works in an interactive terminal, you can
instead add `alias termicast='/opt/termicast/.venv/bin/termicast'` to
`~/.bashrc`.

---

## 🧭 3. The main menu

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

## 🎬 4. Creating your first podcast

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

## 🎧 5. Adding an episode (the main thing you'll do)

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

> [!TIP]
> Your audio and image files must be on the *same machine* as Termicast. If
> Termicast runs on a VPS, it can't see files on your laptop — upload them to
> the server first.

---

## ⏰ 6. Scheduling an episode

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

## 📦 7. Importing an existing podcast

If you already publish a podcast somewhere else, choose
**2. Import existing podcast**. Termicast:

1. Reads your current feed (a URL or an XML file you've downloaded).
2. Downloads your episodes, artwork, and transcripts.
3. Keeps your episode IDs and details intact.
4. Generates a new feed at your chosen location.

> [!TIP]
> The output directory, base URL, and hosting/S3 settings you enter at the
> start are saved as you go. If the import fails partway through — a bad S3
> permission, a naming collision, a network hiccup — starting **Import
> existing podcast** again offers to resume right where you left off instead
> of re-asking all of those questions from scratch.

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

> [!IMPORTANT]
> After importing, check that the new feed is publicly reachable, then ask
> your old host to set up a **301 redirect** from the old feed URL to the new
> one. Termicast can't do that redirect for you — your old host must.

---

## 🌐 8. Hosting (where your files live)

One thing stays the same no matter what: **your feed file (`feed.xml`) always
stays on your own web server.** It's written to the show's output folder and
served from your base URL — the local copy is the canonical one. Only the big
media files (audio, artwork, transcripts, chapters) can optionally move to
cloud storage. If you'd like, S3 hosting can also mirror a copy of `feed.xml`
into the bucket, but the web-server copy remains the one listeners are pointed
at.

Under **4. Hosting** in your show's menu:

| Option | What it does |
| --- | --- |
| **1. Configure hosting** | Choose local hosting or S3-compatible storage |
| **2. Deploy** | Push your media files to your hosting |
| **3. Deploy (dry run)** | Show what would be uploaded, without doing it |
| **4. Hosting checks (doctor)** | Check your feed and files are publicly reachable |
| **5/6. Nginx/Apache snippet** | A paste-in MIME-type config for your web server |
| **7. S3 write-access policy** | A paste-in AWS IAM policy granting exactly the S3 access Termicast needs |
| **8. Migration guidance** | Instructions for switching from another host |
| **10. Correct host MIME types** | Detect local Nginx/Apache tools, edit a site configuration, validate, and optionally reload and recheck |

When the interactive Hosting checks or Deploy action reports incorrect MIME types,
Termicast offers **Correct host MIME types** directly. This is a guided repair:
select the installed server and the active site configuration serving your podcast.
Termicast displays the MIME mappings and opens `$VISUAL`, `$EDITOR`, or `vi` so you
can apply them in the correct directory/location block. Scope chapter JSON mappings
to `chapters/` when the site also serves ordinary JSON; preserve existing Nginx MIME
mappings instead of adding a duplicate `types` block.

The selected server's default configuration is tested before and after the edit.
Termicast keeps a private backup outside server include directories, restores the
selected file if editing or validation fails, and asks before reloading. After a
reload it runs public hosting checks again. The displayed backup is in a temporary
directory; copy it elsewhere if you need long-term retention. Only the selected
file is backed up. Use an account with the required file and server-control
permissions; Termicast does not invoke sudo. Custom server configurations or
container-managed servers should be corrected through their deployment tooling.

Finding a server executable does not prove it serves the public URL. Confirm the
site before editing. S3/CDN media headers may require object metadata or CDN fixes;
the canonical feed on the local web server can still use this repair flow.

**Local hosting** is simplest: your web server serves everything — feed and
media — from the output folder.

> [!TIP]
> Termicast will create your output folder for you if it doesn't already
> exist — but that needs your account to have write access to its *parent*
> folder too (e.g. `/var/www`), which is often locked down on a shared
> server. If you hit a permission error, ask whoever manages the server to
> create the folder and hand it to you instead: `sudo mkdir -p
> /var/www/mypodcast && sudo chown yourusername /var/www/mypodcast`.
> Termicast only needs write access to that one folder from then on — never
> to the shared server directory around it.

**S3 storage** (Amazon S3, Linode, etc.) keeps your feed on your web server but
moves the audio and artwork into a bucket, which is usually cheaper for big
files. You'll set two addresses:

- **Base URL** — where your web server serves `feed.xml`.
- **Asset base URL** — the public address of your bucket (e.g.
  `https://my-bucket.us-east-1.linodeobjects.com/my-show`).

> [!IMPORTANT]
> Your S3 credentials live in a file called `~/.s3cfg` on the server — never
> in Termicast's own database, settings, or backups. If that file doesn't
> exist yet, Termicast offers to create it for you right here: it asks for
> your access key and secret key with masked input (never echoed to the
> screen), writes them straight to `~/.s3cfg` with owner-only permissions,
> and never keeps a copy anywhere else. Say no and set it up yourself if
> you'd rather.

> [!TIP]
> Termicast checks that your credentials can both list *and write to* the
> bucket before it lets you continue — a key that can only list or read is a
> common source of confusing "Access Denied" errors partway through an
> import. It checks the destination is empty, then uploads and removes a
> small, uniquely named test object; a bucket that can't even be listed (wrong
> endpoint, missing or revoked credentials) is reported right away rather than
> treated as an empty destination. If a check fails, or an upload is denied
> later, Termicast prints a ready-to-paste fix: an AWS IAM policy scoped to
> exactly the access it needs, or a checklist for other providers. You can also
> pull that policy up any time from **Hosting → S3 write-access policy**.

For an existing podcast, `deploy` and automatic publishing run a lighter
preflight first: missing `s4cmd` or credentials are caught before any upload
starts, without requiring an empty prefix or delete permission. The actual
upload remains the final test of write access.

When setting up S3, you'll also be asked **"Automatically deploy on
publish/schedule?"**, **"Keep a local copy of media after it's uploaded to
S3?"**, and **"Also upload a copy of feed.xml to the bucket?"**

- **Automatically deploy** controls *future* episodes — say **No** if you'd
  rather review and run `deploy` yourself each time. It has no effect on an
  import already in progress: importing into S3 hosting always uploads once
  at the end, regardless of this setting, so you never end up with an
  "imported" podcast whose media never actually reached the bucket.
- **Keep a local copy**:
  - **No** (default): once your audio and artwork are in the bucket, the
    local copies are removed. Only `feed.xml` stays on your server.
  - **Yes**: Termicast keeps a copy on your server too, for redundancy and
    backup purposes.
- **Mirror feed.xml**: say **Yes** to also upload a copy of the freshly
  written `feed.xml` to the bucket (uploaded after the media). `feed.xml`
  still stays on your web server either way — turning this off later simply
  stops updating the mirrored copy; it never deletes the existing one.

> [!TIP]
> If `doctor` or a deploy prints a flood of near-identical "Content-Type"
> errors, Termicast collapses them into a few examples and tells you the fix.
> For URLs served by your web server, use **Hosting → Nginx/Apache MIME
> snippet** and reload the server. For S3/CDN URLs the upload already set the
> Content-Type, so check the object metadata and any CDN/proxy header
> overrides or cached headers instead.

---

## 💾 9. Backing up your data

Press **B** at any menu to make a backup. It saves your settings, episodes, and
schedules into a private ZIP file.

To include your audio and artwork too, use the command line:

```sh
termicast backup --include-media
```

> [!WARNING]
> Keep backups somewhere private, outside your public web folders.

---

## ❓ 10. Getting help

Press **F** at any menu for the FAQ, or run:

```sh
termicast faq
```

---

## 📋 11. Command cheat sheet

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

> [!NOTE]
> **What's `<show-id>`?** It's the internal ID shown next to your show's name
> in the "Open podcast" list. It looks like a long number-letter code.

---

That's everything you need to get started. Create your show, add an episode,
and hit **Publish now**.

---

Developed with the help of machines by **Tyler Woodward** of **The Tyler
Woodward Project**.

[![Website](https://img.shields.io/badge/Website-tylerwoodward.me-14b8a6?style=for-the-badge)](https://tylerwoodward.me)
[![Threads](https://img.shields.io/badge/Threads-%40tylerwoodward.me-000000?style=for-the-badge&logo=threads&logoColor=white)](https://www.threads.net/@tylerwoodward.me)
[![Instagram](https://img.shields.io/badge/Instagram-%40tylerwoodward.me-E4405F?style=for-the-badge&logo=instagram&logoColor=white)](https://www.instagram.com/tylerwoodward.me)
[![Bluesky](https://img.shields.io/badge/Bluesky-tylerwoodward.me-0285FF?style=for-the-badge&logo=bluesky&logoColor=white)](https://bsky.app/profile/tylerwoodward.me)
[![YouTube](https://img.shields.io/badge/YouTube-%40thetylerwoodwardproject-FF0000?style=for-the-badge&logo=youtube&logoColor=white)](https://www.youtube.com/@thetylerwoodwardproject)
[![Facebook](https://img.shields.io/badge/Facebook-%2Fthetylerwoodwardproject-1877F2?style=for-the-badge&logo=facebook&logoColor=white)](https://www.facebook.com/thetylerwoodwardproject)

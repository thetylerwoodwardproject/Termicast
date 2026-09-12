"""Termicast command-line entry point and podcast menus."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo
from zipfile import ZipFile

from rich.table import Table
from rich.text import Text

from .database import Database
from .backup import create_backup
from .feed import validate_feed
from .importer import import_feed, download_import
from .csvio import export_csv, import_csv
from .migration import migration_guidance
from .models import new_show
from .prompts import (
    ACCENT, confirm, console, edit_field, error, menu, show_banner, show_form, show_summary, text,
    warning, menu_utilities, show_faq, Cancelled, ExitRequested, edit_episode_form,
    optional_assets, add_episode, episode_form, hosting_menu,
)
from .publisher import Publisher
from .repair import scan_show, repair_show
from .hosting import doctor


def _describe_action_error(exc):
    """Render an exception for the "Action failed" banner.

    A bare PermissionError just names the path it couldn't write to (e.g. a
    hidden staging directory next to the output folder), which reads as
    baffling. Point at the directory that actually needs write access instead.
    """
    if isinstance(exc, PermissionError) and exc.filename:
        path = Path(exc.filename)
        directory = path if path.is_dir() else path.parent
        return (f"Action failed: no write permission for {directory} "
                f"(needed to create {path.name} there). Grant your account write "
                f"access to that directory, e.g. `sudo chown \"$USER\" {directory}`, "
                "then try again.")
    return f"Action failed: {exc}"


def _artwork_reviewer():
    """Create conversion approval state scoped to a single import."""
    automatic = False

    def review(url, format_name, mode, size, target_size=None):
        nonlocal automatic
        console.print(f"Artwork needs conversion\nSource: {url}\n"
                      f"Detected: {format_name}, {mode}, {size[0]}×{size[1]}\n"
                      "Required: RGB. Transparency, if present, will be flattened onto white.",
                      markup=False)
        if target_size:
            console.print(f"Resize to {target_size[0]}×{target_size[1]}, preserving proportions. "
                          "Non-square images will be padded with white; smaller images will be enlarged.")
        if automatic:
            console.print("Automatically converting artwork to meet import requirements.")
            return True
        action = menu("Artwork conversion", ["Convert this image",
                      "Automatically convert remaining artwork in this import", "Cancel import"], 1)
        automatic = action == 2
        return action != 3

    return review


def _choose_naming(episodes, show):
    """Return (scheme, fallback); (None, ...) keeps the imported file names."""
    from .models import plan_slugs
    console.print("Imported files are named after a hash of their original URL, such as "
                  "audio/a3f9c2…e81.mp3. They can instead be named in sequence.", markup=False)
    action = menu("Episode file naming", ["Keep the imported file names",
                                          "ep001, ep002, … (episode number)",
                                          "s01ep001, s01ep002, … (season and episode number)"], 2)
    if action == 1:
        return None, "position"
    scheme = "ep" if action == 2 else "sep"
    fallback = "position"
    unnumbered = [e for e in episodes if e.get("episode_number") is None]
    if unnumbered:
        console.print(f"{len(unnumbered)} of {len(episodes)} episodes declare no episode "
                      "number in the feed.", markup=False)
        fallback = ("position", "keep")[menu("Episodes without an episode number",
                    ["Number them by publication order, oldest first",
                     "Keep their imported file names"], 1) - 1]
    try:
        planned = plan_slugs(episodes, scheme, fallback)
    except ValueError as exc:
        error(exc)
        warning("Feed episode numbers cannot name these files uniquely.")
        if not confirm("Number every episode by publication order instead?", True):
            return None, fallback
        try:
            planned = plan_slugs(episodes, scheme, "renumber")
        except ValueError as retry:
            error(retry)
            return None, fallback
        fallback = "renumber"
    if not planned:
        warning("No episode can be named under this scheme; keeping the imported names.")
        return None, fallback
    _preview_names(planned, len(episodes))
    if show.get("hosting") == "s3" and not show.get("keep_local_media"):
        warning("This podcast uploads media to S3 and does not keep a local copy. "
                "Termicast can still rename these files after they're deployed by "
                "renaming the object directly in S3, without a local copy. Consider "
                "enabling 'Keep a local copy of media' under Hosting for redundancy "
                "and media-inclusive backups.")
    if not confirm("Use these names?", True):
        return None, fallback
    return scheme, fallback


def _preview_names(planned, total):
    """Show the first few planned names and the last, rather than all of them."""
    names = list(planned.values())
    shown = names[:5] + (["…"] if len(names) > 6 else []) + (names[-1:] if len(names) > 5 else [])
    console.print(f"Planned names ({len(names)} of {total} episodes):", style=ACCENT)
    for name in shown:
        console.print(f"  {name}", markup=False)


def _resolve_optional(episode, kind, url, exc):
    warning(f"{episode['title']}: linked {kind} failed ({url}): {exc}")
    action = menu("Broken optional resource", ["Retry", "Replacement HTTPS URL", "Skip and remove reference"], 1)
    if action == 3:
        return None
    return url if action == 1 else text("Replacement HTTPS URL", required=True)


def _review_optional(episodes, root, show):
    if not confirm("Import review: add missing optional chapters or transcripts?", False):
        return
    while True:
        labels = [f"{e['title']} — chapters: {'yes' if e['chapters'] else 'absent'}, transcript: {'yes' if e['transcript_url'] else 'absent'}"
                  for e in episodes]
        index = menu("Select episode for optional assets", labels + ["Finish review"], len(labels) + 1)
        if index > len(episodes):
            return
        optional_assets(episodes[index - 1], show)


def _check_repair(db, show):
    scan = scan_show(db, show["id"])
    console.print("Scan is offline. Absent optional chapters/transcripts are not errors.", markup=False)
    for issue in scan["issues"]:
        warning(issue)
    action = menu("Check And Repair", ["Regenerate", "Cancel"], 2)
    if action == 2:
        return
    output_dir = None
    if confirm("Correct output directory to a NEW path? Existing media will need copying separately.", False):
        output_dir = str(Path(text("New output directory", required=True)).expanduser().absolute())
    recoveries = {}
    for missing in scan["missing"]:
        if missing["regenerable"]:
            continue
        if confirm(f"Supply a local recovery file for {missing['url']}?", False):
            recoveries[missing["path"]] = text("Existing local source file", required=True)
    destination = output_dir or show["output_dir"]
    console.print(f"Regenerate: {destination}/feed.xml and saved chapter JSON\nPublic base URL: {show['base_url']}", markup=False)
    for target, source in recoveries.items():
        from .storage import asset_root, asset_base
        relative = Path(target).relative_to(asset_root(show))
        console.print(f"Copy {source} -> {asset_root(show) / relative}\nURL: {asset_base(show)}/{relative.as_posix()}", markup=False)
    if not confirm("Back up saved state/feeds/chapters and regenerate WITHOUT releasing scheduled episodes?", False):
        return
    backup, result = repair_show(db, scan, recoveries, output_dir)
    console.print(f"Backup: {backup}", markup=False)
    for issue in result["issues"]:
        warning(issue)
    console.print(f"Repair complete: {len(recoveries)} local files recovered; {len(result['issues'])} remaining issues.", markup=False)


def _backup(db, destination=None, include_media=False):
    path = create_backup(db, destination, include_media=include_media)
    console.print(f"Backup saved: {path}", style=ACCENT, markup=False)
    with ZipFile(path) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    for missing in manifest["missing_files"]:
        warning(f"Not present in backup: {missing}")
    return path


def _interactive_backup(db):
    console.print("Backs up saved data, feeds, and chapter JSON for ALL podcasts. "
                  "Unsaved edits stay open but are not included. For local media too, use backup --include-media.", markup=False)
    destination = text("Private backup directory (outside public output directories)",
                       str(db.path.parent / "backups"), required=True)
    _backup(db, destination)


def _select_show(db, title="Open podcast"):
    shows = db.list_shows()
    if not shows:
        warning("No podcasts configured. Create or import one first.")
        return None
    selected = menu(title, [f"{show['title']} ({show['id']})" for show in shows] + ["Back"])
    return shows[selected - 1] if selected <= len(shows) else None


def _list_episodes(db, show, selection="all"):
    episodes = db.list_episodes(show["id"])
    if selection != "all":
        episodes = [episode for episode in episodes if episode.get("status") == selection]
    zone = show.get("timezone", "UTC")
    table = Table(title=f"{selection.title()} episodes ({zone})",
                  border_style=ACCENT)
    for name in ("Title", "Status", "Publication time", "GUID"):
        table.add_column(name, overflow="fold")
    for episode in episodes:
        value = episode.get("publish_at") or episode.get("published_at")
        display = "Not scheduled"
        if value:
            try:
                date = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
                if date.tzinfo is None:
                    raise ValueError("Timestamp has no UTC offset")
                display = date.astimezone(ZoneInfo(zone)).isoformat()
            except (ValueError, TypeError, KeyError, AttributeError):
                display = f"{value} (invalid time or timezone)"
        table.add_row(Text(str(episode.get("title", ""))), Text(str(episode.get("status", ""))),
                      Text(display), Text(str(episode.get("guid", ""))))
    console.print(table)
    if not episodes:
        warning("No episodes to display.")
    return episodes


def _episodes(db, publisher, show):
    selection = "all"
    while True:
        episodes = _list_episodes(db, show, selection)
        options = [f"{e['title']} ({e['status']})" for e in episodes]
        action = menu("Select episode to edit", options + ["Filter episodes", "Back"])
        if action == len(episodes) + 2:
            return
        if action == len(episodes) + 1:
            selection = ("all", "published", "scheduled")[menu(
                "Filter episodes", ["All", "Published", "Scheduled"]) - 1]
        else:
            edit_episode_form(db, show, publisher, episodes[action - 1])


def _tools(db, publisher, show):
    while True:
        action = menu("Tools", ["Check And Repair", "Regenerate feed", "Import episode CSV",
                                "Export episode CSV", "Migration guidance", "Back"])
        if action == 6:
            return
        try:
            _tool_action(db, publisher, show, action)
        except Cancelled:
            console.print("Cancelled. Nothing was changed.")
        if action == 1:
            show = db.get_show(show["id"])


def _tool_action(db, publisher, show, action):
    """Run one Tools menu action; the caller reports a cancelled prompt."""
    if action == 1:
        _check_repair(db, show)
    elif action == 2:
        publisher.regenerate(show["id"])
        console.print("Feed regenerated.", style=ACCENT)
    elif action == 3:
        path = text("CSV path (merge; no episodes deleted)", required=True)
        if confirm("Preflight and merge this CSV?", False):
            count = import_csv(db, show["id"], path)
            console.print(f"Merged {count} episodes.", style=ACCENT)
    elif action == 4:
        selection = ("all", "published", "scheduled")[menu("Export episodes", ["All", "Published", "Scheduled"]) - 1]
        path = text("Private CSV destination", required=True)
        console.print(str(export_csv(db, show["id"], path, selection)), markup=False)
    elif action == 5:
        console.print(migration_guidance(show), markup=False)


def _open_show(db, publisher, show):
    while True:
        show = db.get_show(show["id"])
        show_summary(db, show)
        action = menu("CONTROL ROOM / Choose a number",
                      ["New episode", "Episodes", "Podcast settings",
                       "Hosting", "Tools", "Switch podcast / Back"],
                      headers={0: "PUBLISH & MANAGE", 2: "CONFIGURATION",
                               4: "TOOLS", 5: "SESSION"})
        try:
            if action == 6:
                return
            if action == 1:
                episode_form(show, publisher, db)
            elif action == 2:
                _episodes(db, publisher, show)
            elif action == 3:
                updated = show_form(show)
                if updated is not None:
                    db.save_show(updated)
                    publisher.regenerate(updated["id"])
                    console.print("Settings saved and feed regenerated.", style=ACCENT)
            elif action == 4:
                hosting_menu(db, publisher, show)
            elif action == 5:
                _tools(db, publisher, show)
        except EOFError:
            raise
        except Cancelled:
            console.print("Cancelled. Nothing was changed.")
        except Exception as exc:
            error(_describe_action_error(exc))


def _pick_output_dir(destination):
    """Ask for the output directory; offer to overwrite an existing feed.xml there.

    Returns True if the user chose to overwrite a previous import.
    """
    while True:
        edit_field(destination, "output_dir")
        feed_path = Path(destination["output_dir"]).expanduser().absolute() / "feed.xml"
        if not feed_path.exists() and not feed_path.is_symlink():
            return False
        action = menu(f"{feed_path} already exists",
                      ["Overwrite it (replaces the existing feed.xml; any asset "
                       "filename that collides with a new one is reported, not "
                       "deleted)",
                       "Choose a different directory", "Cancel import"], 2)
        if action == 1:
            return True
        if action == 3:
            raise Cancelled


def _configure_hosting(destination):
    """Collect hosting settings, verifying an S3 destination is reachable and
    writable now.

    Failing fast here means a bad bucket/endpoint/credential is caught before
    the rest of the import wizard (feed fetch, metadata review, naming) runs.
    """
    from .prompts import hosting_form
    while True:
        hosting_form(destination)
        if destination.get("hosting") != "s3":
            return
        console.print("Verifying the S3 destination is reachable and writable...", markup=False)
        try:
            from .s3deploy import check_s3_destination
            check_s3_destination(destination)
        except (ValueError, RuntimeError) as exc:
            error(f"S3 check failed: {exc}")
            if confirm("Edit hosting settings and try again?", True):
                continue
            raise Cancelled
        console.print("S3 destination looks good.", style=ACCENT)
        return


def _create_or_import(db, publisher, importing=False):
    source = ""
    template = None
    episodes = None
    if importing:
        destination = new_show()
        console.print("Choose the feed directory and its public HTTPS directory URL. Existing website directories are allowed. Hosting is configured next.", markup=False)
        overwrite = _pick_output_dir(destination)
        edit_field(destination, "base_url")
        _configure_hosting(destination)
        kind = menu("Import source", ["Feed (HTTPS URL or local XML)", "Archive manifest (local files)"], 1)
        if kind == 2:
            manifest = text("Archive manifest path (manifest.json)", required=True)
            from .archive import archive_identity, import_archive
            settings = archive_identity(manifest)
            show = new_show(**settings)
            show.update(settings)
            show["output_dir"] = destination["output_dir"]
            show["base_url"] = destination["base_url"]
            show.update({key: value for key, value in destination.items()
                         if key in ("hosting", "endpoint_url", "bucket", "prefix", "asset_base_url", "enabled", "keep_local_media")})
            show = show_form(show)
            if show is None:
                return
            from .archive import load_manifest, merged_template
            from .importer import extract_episodes
            scheme, fallback = _choose_naming(
                extract_episodes(merged_template(*load_manifest(manifest))), show)
            show, episodes, template = import_archive(show, manifest, review_artwork=_artwork_reviewer(),
                                                      naming=scheme, naming_fallback=fallback,
                                                      overwrite=overwrite)
        else:
            source = text("Existing feed (HTTPS URL or local XML path)", required=True)
            settings, template = import_feed(source)
            show = new_show(**settings)
            show.update(settings)
            console.print("Choose the NEW hosting location; imported identity and XML are retained.",
                          style=ACCENT)
            show["output_dir"] = destination["output_dir"]
            show["base_url"] = destination["base_url"]
            show.update({key: value for key, value in destination.items()
                         if key in ("hosting", "endpoint_url", "bucket", "prefix", "asset_base_url", "enabled", "keep_local_media")})
            show = show_form(show)
            if show is None:
                return
            from .importer import extract_episodes
            scheme, fallback = _choose_naming(extract_episodes(template), show)
            show, episodes = download_import(show, template,
                                             review_optional=lambda episodes, root: _review_optional(episodes, root, show),
                                             resolve_optional=_resolve_optional,
                                             review_artwork=_artwork_reviewer(),
                                             naming=scheme, naming_fallback=fallback,
                                             overwrite=overwrite)
    else:
        show = show_form(new_show(), collect=True)
        if show is None:
            return
    show = db.save_show(show, template=template, episodes=episodes)
    publisher.regenerate(show["id"])
    console.print("Podcast saved and feed generated.", style=ACCENT)
    if importing:
        console.print(migration_guidance(show, source), markup=False)


def _interactive(db, publisher):
    show_banner()
    while True:
        action = menu("Termicast", ["Open podcast", "Import existing podcast", "Create podcast",
                                    "Forget podcast", "Quit"])
        try:
            if action == 5:
                return 0
            if action == 1:
                show = _select_show(db)
                if show:
                    _open_show(db, publisher, show)
            elif action in (2, 3):
                _create_or_import(db, publisher, importing=action == 2)
            elif action == 4:
                show = _select_show(db, "Forget podcast")
                if show and confirm(f"Forget {show['title']} and its managed drafts? "
                                    "Feed and chapter files will NOT be deleted."):
                    db.forget_show(show["id"])
                    console.print("Podcast forgotten. Output files were not deleted.", style=ACCENT)
        except EOFError:
            raise
        except Cancelled:
            console.print("Cancelled. Nothing was changed.")
        except Exception as exc:
            error(_describe_action_error(exc))


def _validate(db, show_id):
    if show_id:
        show = db.get_show(show_id)
        if not show:
            error(f"Unknown podcast ID: {show_id}")
            return 1
        shows = [show]
    else:
        shows = db.list_shows()
    failures = 0
    for show in shows:
        label = f"{show.get('title', show['id'])} ({show['id']})"
        try:
            path = Path(show["output_dir"]).expanduser() / "feed.xml"
            errors = validate_feed(path.read_bytes())
        except Exception as exc:
            errors = [f"Cannot validate managed feed: {exc}"]
        if errors:
            failures += 1
            error(f"{label}: FAILED")
            for message in errors:
                error(f"  {message}")
        else:
            console.print(f"{label}: valid ({path})", style=ACCENT, markup=False)
    if not shows:
        console.print("No managed feeds to validate.")
    return 1 if failures else 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="termicast", description="Manage and publish podcast feeds.")
    parser.add_argument("--data-dir", help="Store the database here (sets TERMICAST_HOME).")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("publish-due", help="Publish scheduled episodes that are due; suitable for cron.")
    backup = commands.add_parser("backup", help="Back up all saved state, feeds, and chapter JSON to a private ZIP.")
    backup.add_argument("destination", nargs="?", help="Backup directory; defaults to <data-dir>/backups.")
    backup.add_argument("--include-media", action="store_true", help="Include local audio, artwork, and transcripts.")
    for name in ("import-csv", "export-csv"):
        command = commands.add_parser(name, help="Merge or export episode CSV.")
        command.add_argument("show_id")
        command.add_argument("path")
        if name == "export-csv":
            command.add_argument("--filter", choices=("all", "published", "scheduled"), default="all")
    commands.add_parser("faq", help="Read frequently asked questions, including backup and recovery.")
    validate = commands.add_parser("validate", help="Validate one or all managed output feeds.")
    validate.add_argument("show_id", nargs="?", help="Podcast ID; omit to validate all feeds.")
    add = commands.add_parser("add", help="Create an episode from local media files.")
    add.add_argument("show_id")
    add.add_argument("files", nargs="+", help="Audio (required), then optional artwork and transcript.")
    add.add_argument("--slug", help="Editorial slug such as s02ep042.")
    add.add_argument("--audio-preset", choices=("standard", "music"), help="Override the show's audio preset.")
    add.add_argument("--image-preset", choices=("compact", "detail"), help="Override the show's image preset.")
    add.add_argument("--keep-audio", action="store_true", help="Keep the original audio instead of optimizing.")
    add.add_argument("--keep-image", action="store_true", help="Keep the original artwork instead of optimizing.")
    deploy = commands.add_parser("deploy", help="Upload saved media assets to S3 (feed.xml stays on the web server); verify by default.")
    deploy.add_argument("show_id")
    deploy.add_argument("--dry-run", action="store_true", help="No uploads or bucket probes.")
    deploy.add_argument("--no-verify", action="store_true", help="Skip post-deploy public verification.")
    doctor_cmd = commands.add_parser("doctor", help="Read-only hosting and media checks.")
    doctor_cmd.add_argument("show_id", nargs="?", help="Podcast ID; omit to check all shows.")
    archive = commands.add_parser("archive", help="Download a feed and assets into a persistent archive.")
    archive.add_argument("source", help="Feed HTTPS URL or local XML path.")
    archive.add_argument("destination", help="Archive directory.")
    restage_cmd = commands.add_parser("restage", help="Preview URL restaging from an archive manifest.")
    restage_cmd.add_argument("manifest", help="Path to the archive manifest.json.")
    restage_cmd.add_argument("base_url", help="Public base URL for the restaged feed.")

    args = parser.parse_args(argv)
    if args.data_dir is not None:
        os.environ["TERMICAST_HOME"] = str(Path(args.data_dir).expanduser())
    try:
        if args.command == "faq":
            show_faq()
            return 0
        if args.command == "archive":
            from .archive import archive_feed
            console.print(f"Archive written: {archive_feed(args.source, args.destination)}",
                          style=ACCENT, markup=False)
            return 0
        if args.command == "restage":
            from .archive import restage
            mapping, errors = restage(args.manifest, args.base_url)
            for original, new in sorted(mapping.items()):
                console.print(f"{original} -> {new}", markup=False)
            for message in errors:
                error(message)
            return 1 if errors else 0
        db = Database()
        if args.command == "backup":
            _backup(db, args.destination, args.include_media)
            return 0
        if args.command == "validate":
            return _validate(db, args.show_id)
        if args.command == "doctor":
            problems = doctor(db, args.show_id)
            if problems:
                for problem in problems:
                    warning(problem)
                return 1
            console.print("No hosting problems found.", style=ACCENT)
            return 0
        publisher = Publisher(db)
        if args.command == "add":
            show = db.get_show(args.show_id)
            if show is None:
                error(f"Unknown podcast ID: {args.show_id}")
                return 1
            add_episode(db, publisher, show, args.files, slug=args.slug,
                        audio_preset=args.audio_preset, image_preset=args.image_preset,
                        keep_audio=args.keep_audio, keep_image=args.keep_image)
            return 0
        if args.command == "deploy":
            publisher.deploy(args.show_id, dry_run=args.dry_run, verify=not args.no_verify)
            console.print("Deployed." if not args.dry_run else "Dry run complete: no uploads or bucket probes were made.",
                          style=ACCENT)
            return 0
        if args.command == "import-csv":
            console.print(f"Merged {import_csv(db, args.show_id, args.path)} episodes.")
            return 0
        if args.command == "export-csv":
            console.print(str(export_csv(db, args.show_id, args.path, args.filter)), markup=False)
            return 0
        if args.command == "publish-due":
            count = publisher.publish_due()
            console.print(f"Published {count} due episode(s).", style=ACCENT)
            return 0
        with menu_utilities(lambda: _interactive_backup(db)):
            return _interactive(db, publisher)
    except ExitRequested:
        console.print("Exiting. Unsaved form edits were discarded.")
        return 0
    except Cancelled:
        console.print("Cancelled.")
        return 0
    except EOFError:
        console.print("\nInput closed. Exiting.")
        return 0
    except KeyboardInterrupt:
        console.print("\nCancelled. Exiting.")
        return 130
    except Exception as exc:
        error(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

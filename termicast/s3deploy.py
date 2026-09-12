"""Ordered, verified S3-compatible deployment using the external `s4cmd` tool.

Credentials stay in `~/.s3cfg`; Termicast only supplies nonsecret destination
settings. Uploads are serialized (one subprocess at a time) and use s4cmd's
`--sync-check` md5 metadata so unchanged objects are skipped and multipart ETags
are never treated as ordinary MD5 hashes.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from .media import content_type_for
from .storage import asset_root

# Upload controls from the rev1 plan.
SINGLEPART_LIMIT = 64 * 1024 * 1024
MULTIPART_SPLIT = 16 * 1024 * 1024
NUM_THREADS = 2


def s4cmd_path():
    """Resolve the s4cmd executable, preferring the active venv, then PATH."""
    candidate = Path(sys.executable).parent / "s4cmd"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("s4cmd")
    if found:
        return found
    raise RuntimeError(
        "s4cmd is required for S3 deployment. Install it (pip install s4cmd "
        "into the Termicast environment) and retry.") from None


def object_key(show, relative):
    prefix = show.get("prefix", "")
    return f"{prefix}/{relative}" if prefix else relative


def s4cmd_args(show, content_type):
    args = [s4cmd_path(), "put"]
    if show.get("endpoint_url"):
        args += ["--endpoint-url", show["endpoint_url"]]
    args += ["--num-threads", str(NUM_THREADS),
             "--multipart-split-size", str(MULTIPART_SPLIT),
             "--max-singlepart-upload-size", str(SINGLEPART_LIMIT),
             "--force", "--sync-check",
             "--API-ContentType", content_type]
    return args


def upload_file(show, local_path, remote_relative, dry_run=False):
    """Upload one file with an explicit Content-Type; skip unchanged objects.

    A dry run performs no upload and no bucket probe.
    """
    content_type = content_type_for(remote_relative)
    if content_type is None:
        raise ValueError(
            f"No Content-Type mapping for {remote_relative}; use a supported "
            "audio, image, transcript, chapter, or feed format")
    local_path = Path(local_path)
    if not local_path.is_file():
        raise ValueError(f"Upload source does not exist: {local_path}")
    if dry_run:
        return
    args = s4cmd_args(show, content_type) + [
        str(local_path), f"s3://{show['bucket']}/{object_key(show, remote_relative)}"]
    process = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if process.returncode != 0:
        raise RuntimeError(
            f"s4cmd upload failed for {remote_relative}: "
            f"{process.stderr.strip() or process.stdout.strip()}") from None


def deploy_paths(show, relative_paths, source_root=None, *, dry_run=False, verify=True):
    """Upload and verify a batch of managed assets in the given order.

    `relative_paths` is the explicit set of managed asset files to deploy
    (never `feed.xml`, which stays on the web server). A failed upload stops
    the batch and raises. Returns the list of uploaded relative paths.
    """
    if not relative_paths:
        return []
    root = Path(source_root) if source_root else asset_root(show)
    uploaded = []
    problems = []
    for relative in relative_paths:
        try:
            upload_file(show, root / relative, relative, dry_run=dry_run)
            uploaded.append(relative)
        except (ValueError, RuntimeError) as exc:
            problems.append(f"{relative}: {exc}")
            break
    if verify and not dry_run:
        from .hosting import check_url
        from .storage import asset_base
        base = asset_base(show)
        for relative in uploaded:
            problems.extend(check_url(f"{base}/{relative}", content_type_for(relative)))
    if problems:
        raise RuntimeError("; ".join(problems))
    return uploaded


def remote_rename(show, old_relative, new_relative):
    """Rename one S3 object in place via a server-side copy, then delete the old key.

    Used when an asset has no local copy (S3 hosting without 'keep a local
    copy of media'): the bytes never leave S3, so nothing needs downloading
    and re-uploading. Metadata, including Content-Type, is preserved by s4cmd.
    """
    args = [s4cmd_path(), "mv"]
    if show.get("endpoint_url"):
        args += ["--endpoint-url", show["endpoint_url"]]
    args += ["--force",
             f"s3://{show['bucket']}/{object_key(show, old_relative)}",
             f"s3://{show['bucket']}/{object_key(show, new_relative)}"]
    process = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if process.returncode != 0:
        raise RuntimeError(
            f"s4cmd rename failed for {old_relative} -> {new_relative}: "
            f"{process.stderr.strip() or process.stdout.strip()}") from None


def check_s3_destination(show):
    """Reject importing into a prefix that already contains objects."""
    key = object_key(show, "")
    args = [s4cmd_path(), "ls"]
    if show.get("endpoint_url"):
        args += ["--endpoint-url", show["endpoint_url"]]
    args.append(f"s3://{show['bucket']}/{key}")
    try:
        process = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        raise RuntimeError("s4cmd is required to check the S3 destination; install it and retry") from None
    if process.returncode == 0 and process.stdout.strip():
        raise ValueError("S3 destination already contains objects; choose an unused show prefix")

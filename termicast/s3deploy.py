"""Ordered, verified S3-compatible deployment using the external `s4cmd` tool.

Credentials stay in `~/.s3cfg`; Termicast only supplies nonsecret destination
settings. Uploads are batched one `s4cmd put` per managed folder and run one
subprocess at a time, and use s4cmd's `--sync-check` md5 metadata so unchanged
objects are skipped and multipart ETags are never treated as ordinary MD5
hashes. See `batches()` for why a folder is the unit.
"""

import configparser
from functools import lru_cache
import io
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

from .media import content_type_for
from .storage import asset_root

# The exact env vars s4cmd itself checks (S3Handler.s3_keys_from_env), ahead
# of ~/.s3cfg -- checked here too so Termicast never nags about a missing
# file when credentials are already supplied this way.
S3_ACCESS_KEY_ENV = "S3_ACCESS_KEY"
S3_SECRET_KEY_ENV = "S3_SECRET_KEY"

# Upload controls from the rev1 plan.
SINGLEPART_LIMIT = 64 * 1024 * 1024
MULTIPART_SPLIT = 16 * 1024 * 1024
NUM_THREADS = 2

# Bounded timeout for preflight subprocesses (list, probe, delete). Real
# uploads of large media keep no timeout.
S3_CMD_TIMEOUT = 60


# Recent botocore (which s4cmd uses internally) defaults to adding S3 request
# checksums that Linode Object Storage's backend rejects with a generic
# AccessDenied on PutObject. These env vars restore the older opt-in
# behavior; setdefault so an operator's own explicit setting always wins.
def _s4cmd_env():
    env = os.environ.copy()
    env.setdefault("AWS_REQUEST_CHECKSUM_CALCULATION", "when_required")
    env.setdefault("AWS_RESPONSE_CHECKSUM_VALIDATION", "when_required")
    return env


def _run(args, timeout=None):
    """Run s4cmd, converting a hang into an actionable RuntimeError."""
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=timeout, env=_s4cmd_env())
    except subprocess.TimeoutExpired as exc:
        command = args[1] if len(args) > 1 else args[0]
        raise RuntimeError(f"s4cmd {command} timed out after {timeout} seconds") from exc


def s3cfg_path():
    return Path.home() / ".s3cfg"


def s3_credentials_present(path=None):
    """True if s4cmd can find S3 credentials, via env vars or ~/.s3cfg.

    Mirrors s4cmd's own lookup order closely enough to avoid nagging: it
    checks S3_ACCESS_KEY/S3_SECRET_KEY, then `[default] access_key` /
    `secret_key` in the s3cfg file (s4cmd also accepts command-line flags,
    which Termicast never passes, so those aren't checked here).
    """
    if os.environ.get(S3_ACCESS_KEY_ENV) and os.environ.get(S3_SECRET_KEY_ENV):
        return True
    cfg_path = Path(path) if path else s3cfg_path()
    if not cfg_path.is_file():
        return False
    config = configparser.ConfigParser()
    try:
        config.read(cfg_path)
        return bool(config.get("default", "access_key", fallback="")) and \
            bool(config.get("default", "secret_key", fallback=""))
    except configparser.Error:
        return False


def write_s3cfg(access_key, secret_key, path=None):
    """Write a minimal ~/.s3cfg that s4cmd can read, owner-only permissions.

    s4cmd only ever reads `[default] access_key` / `secret_key` from this
    file (its `S3Handler.s3_keys_from_s3cfg`) -- host_base/host_bucket are
    never consulted, since Termicast passes `--endpoint-url` explicitly per
    show. Written at mode 0600: this file holds a live secret key.
    """
    cfg_path = Path(path) if path else s3cfg_path()
    config = configparser.ConfigParser()
    config["default"] = {"access_key": access_key, "secret_key": secret_key}
    buffer = io.StringIO()
    config.write(buffer)
    from .publisher import atomic_write
    atomic_write(cfg_path, buffer.getvalue().encode(), mode=0o600)


@lru_cache(maxsize=1)
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


def upload_file(show, local_path, remote_relative, dry_run=False, timeout=None):
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
    process = _run(args, timeout=timeout)
    if process.returncode != 0:
        message = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(
            f"s4cmd upload failed for {remote_relative}: {message}"
            f"{_permission_help(show, message)}") from None


def batches(relative_paths):
    """Group managed paths into runs that one `s4cmd put` can carry.

    s4cmd's multi-source put joins each source's BASENAME onto the target
    directory (S3Handler.put_files), so a batch is only well defined when
    every file in it lands in the same folder. --API-ContentType likewise
    applies to the whole invocation, so the type has to be uniform too.
    Grouping on both gives roughly one run per managed folder -- audio,
    images/episodes, images/chapters, transcripts, chapters -- instead of
    one process per file. Input order is preserved within and across groups.
    """
    groups = {}
    for relative in relative_paths:
        content_type = content_type_for(relative)
        if content_type is None:
            raise ValueError(
                f"No Content-Type mapping for {relative}; use a supported "
                "audio, image, transcript, chapter, or feed format")
        key = (PurePosixPath(relative).parent.as_posix(), content_type)
        groups.setdefault(key, []).append(relative)
    for (folder, content_type), group in groups.items():
        names = [PurePosixPath(relative).name for relative in group]
        # Guaranteed by paths being unique within one folder, but the whole
        # batch silently overwrites itself if it ever stops holding.
        assert len(set(names)) == len(names), f"duplicate basenames in {folder}"
    return groups


def upload_batch(show, root, relatives, content_type, dry_run=False, timeout=None):
    """Upload one batch of files in a single s4cmd process.

    Spawning s4cmd per file meant importing boto3 before a byte moved; a
    150-episode show republishing three assets each paid that ~450 times.
    One process per folder also lets --num-threads do real work, since
    put_files spreads a multi-source batch over its own thread pool.
    """
    root = Path(root)
    for relative in relatives:
        if not (root / relative).is_file():
            raise ValueError(f"Upload source does not exist: {root / relative}")
    if dry_run:
        return
    if len(relatives) == 1:
        upload_file(show, root / relatives[0], relatives[0], timeout=timeout)
        return
    folder = PurePosixPath(relatives[0]).parent.as_posix()
    prefix = object_key(show, folder if folder != "." else "")
    target = f"s3://{show['bucket']}/{prefix.rstrip('/')}/" if prefix else f"s3://{show['bucket']}/"
    args = s4cmd_args(show, content_type) + [str(root / relative) for relative in relatives] + [target]
    process = _run(args, timeout=timeout)
    if process.returncode != 0:
        message = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(
            f"s4cmd upload failed for {len(relatives)} file(s) under {folder}: {message}"
            f"{_permission_help(show, message)}") from None


def _permission_help(show, message):
    """Append actionable guidance when an S3 error looks permission-related,
    so the fix is a paste-and-go step instead of a reverse-engineering exercise.
    """
    if "denied" not in message.lower() and "forbidden" not in message.lower():
        return ""
    if show.get("endpoint_url"):
        prefix = show.get("prefix", "")
        scope = f"'{show['bucket']}' (prefix '{prefix}/')" if prefix else f"'{show['bucket']}' (bucket root, no prefix)"
        return (
            "\nThis looks like a permissions problem with your storage provider's access key, "
            "not a Termicast bug. In your provider's dashboard, make sure the key in ~/.s3cfg "
            f"has read, write, AND delete access to bucket {scope}, not just list/read -- a key "
            "scoped to read-only or list-only permissions is the most common cause.")
    from .hosting import s3_write_policy_snippet
    return (
        "\nThis looks like a permissions problem with your AWS credentials, not a Termicast "
        "bug. Attach this policy to the IAM user or role whose access key is in ~/.s3cfg:\n"
        + s3_write_policy_snippet(show))


def deploy_paths(show, relative_paths, source_root=None, *, dry_run=False, verify=True):
    """Upload and verify a batch of managed assets in the given order.

    `relative_paths` is the explicit set of managed asset files to deploy.
    `feed.xml` is normally excluded (it stays on the web server) and is only
    uploaded when explicitly requested as a mirror. A failed upload stops the
    batch and raises. Returns the list of uploaded relative paths.
    """
    if not relative_paths:
        return []
    root = Path(source_root) if source_root else asset_root(show)
    uploaded = []
    problems = []
    for (folder, content_type), group in batches(relative_paths).items():
        try:
            upload_batch(show, root, group, content_type, dry_run=dry_run)
            uploaded.extend(group)
        except (ValueError, RuntimeError) as exc:
            # No partial credit: a failed batch reports nothing uploaded, so
            # upload_existing_assets cannot delete a local file whose object
            # may never have landed. The raise below stops it regardless.
            problems.append(f"{', '.join(group)}: {exc}")
            break
    if verify and not dry_run:
        from .hosting import check_url
        from .storage import asset_base
        base = asset_base(show)
        for relative in uploaded:
            problems.extend(check_url(f"{base}/{relative}", content_type_for(relative)))
    if problems:
        from .hosting import summarize_verification_problems
        raise RuntimeError("\n".join(summarize_verification_problems(problems, target="s3")))
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
    process = _run(args)
    if process.returncode != 0:
        raise RuntimeError(
            f"s4cmd rename failed for {old_relative} -> {new_relative}: "
            f"{process.stderr.strip() or process.stdout.strip()}") from None


WRITE_PROBE_PREFIX = ".termicast-write-check-"


def _list_destination(show):
    """Return the raw `s4cmd ls` result for the show's bucket/prefix."""
    key = object_key(show, "")
    args = [s4cmd_path(), "ls"]
    if show.get("endpoint_url"):
        args += ["--endpoint-url", show["endpoint_url"]]
    args.append(f"s3://{show['bucket']}/{key}")
    try:
        return _run(args, timeout=S3_CMD_TIMEOUT)
    except FileNotFoundError:
        raise RuntimeError("s4cmd is required to check the S3 destination; install it and retry") from None


def _ensure_empty_destination(show):
    """Reject a destination whose listing fails or that already holds objects.

    A nonempty listing means the prefix is in use; a failed listing is reported
    as a connection/authentication/permission problem rather than silently
    treated as an empty destination.
    """
    process = _list_destination(show)
    if process.returncode != 0:
        message = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(
            f"Could not list the S3 destination to confirm it is empty: {message}"
            f"{_permission_help(show, message)}") from None
    if process.stdout.strip():
        raise ValueError("S3 destination already contains objects; choose an unused show prefix")


def _delete_probe(show, probe_relative):
    """Remove the exact probe object created by this invocation, if any."""
    remote = f"s3://{show['bucket']}/{object_key(show, probe_relative)}"
    args = [s4cmd_path(), "del"]
    if show.get("endpoint_url"):
        args += ["--endpoint-url", show["endpoint_url"]]
    args.append(remote)
    process = _run(args, timeout=S3_CMD_TIMEOUT)
    if process.returncode != 0:
        raise RuntimeError(
            f"Uploaded a write-access probe object but could not remove it ({remote}); "
            f"delete it manually: {process.stderr.strip() or process.stdout.strip()}")


def _check_write_access(show):
    """Probe write and delete access using a unique, prefix-scoped object.

    The probe object name is unique per attempt so concurrent or repeated
    checks never collide with one another, and cleanup targets only the exact
    object this invocation created.
    """
    probe_relative = f"{WRITE_PROBE_PREFIX}{secrets.token_hex(8)}.txt"
    with tempfile.TemporaryDirectory() as tmp:
        probe_path = Path(tmp) / "probe.txt"
        probe_path.write_bytes(b"termicast write check\n")
        try:
            upload_file(show, probe_path, probe_relative, timeout=S3_CMD_TIMEOUT)
        except (ValueError, RuntimeError) as exc:
            raise RuntimeError(f"S3 destination is listable but not writable: {exc}") from None
    _delete_probe(show, probe_relative)


def check_s3_destination(show):
    """Reject importing into a prefix that already contains objects, and confirm
    the credentials can write to (and delete from) it.

    Listing can succeed with read-only credentials while every later
    PutObject is denied, so a probe upload+delete catches that mismatch here
    instead of partway through a deploy. A failed listing is reported as a
    connection/authentication/permission error rather than ignored.
    """
    _ensure_empty_destination(show)
    _check_write_access(show)


def check_s3_access(show):
    """Non-mutating preflight: fail fast on missing tooling or credentials.

    Unlike an import (`check_s3_destination`), an existing show may already
    hold objects and its upload path needs only PutObject, so no listing,
    write probe, or delete probe is attempted here. The actual upload remains
    the definitive write test; this only catches the most common hard failures
    (no s4cmd, no credentials) before asset generation or upload begins.
    """
    s4cmd_path()
    if not s3_credentials_present():
        raise RuntimeError(
            f"S3 credentials are not configured; expected them in {s3cfg_path()} "
            "(or via S3_ACCESS_KEY/S3_SECRET_KEY). Configure hosting first.")


def delete_prefix(show, dry_run=False, allow_empty_prefix=False):
    """Delete every object under this show's S3 prefix; returns the object count.

    Refuses by default when the show has no prefix: an empty prefix means the
    show is hosted at the bucket root, and a recursive delete there destroys
    the whole bucket instead of just this show's content. Pass
    `allow_empty_prefix=True` once the caller has separately warned about and
    confirmed that risk with the user.
    """
    if not show.get("prefix") and not allow_empty_prefix:
        raise ValueError(
            "Refusing to delete S3 objects: this show has no prefix, so a "
            "recursive delete would target the entire bucket. Clean up the "
            "bucket manually, or confirm deleting the whole bucket.")
    check_s3_access(show)
    process = _list_destination(show)
    if process.returncode != 0:
        message = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(
            f"Could not list the S3 destination to delete it: {message}"
            f"{_permission_help(show, message)}") from None
    count = len([line for line in process.stdout.splitlines() if line.strip()])
    if count == 0 or dry_run:
        return count
    remote = f"s3://{show['bucket']}/{object_key(show, '')}"
    args = [s4cmd_path(), "del", "--recursive"]
    if show.get("endpoint_url"):
        args += ["--endpoint-url", show["endpoint_url"]]
    args.append(remote)
    process = _run(args)
    if process.returncode != 0:
        message = process.stderr.strip() or process.stdout.strip()
        raise RuntimeError(
            f"s4cmd delete failed for {remote}: {message}"
            f"{_permission_help(show, message)}") from None
    return count

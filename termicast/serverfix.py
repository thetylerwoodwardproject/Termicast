"""Explicit, interactive site-configuration repair support."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile

# Generous timeout: a sudo password prompt can take a moment, and the actual
# check/reload commands are fast.
CONTROL_TIMEOUT = 120

# Marker Termicast inserts (and searches for) so the MIME block is idempotent.
MIME_MARKER = "Termicast podcast MIME types"

NGINX_MIME_BLOCK = """\
# Termicast podcast MIME types
types {
    application/rss+xml       xml;
    application/json+chapters json;
    text/vtt                  vtt;
    application/x-subrip      srt;
    audio/mp4                 m4a;
    audio/mpeg                mp3;
    image/jpeg                jpg jpeg;
    image/png                 png;
    image/webp                webp;
}
"""

APACHE_MIME_BLOCK = """\
# Termicast podcast MIME types
AddType application/rss+xml .xml
AddType application/json+chapters .json
AddType text/vtt .vtt
AddType application/x-subrip .srt
AddType audio/mp4 .m4a
AddType audio/mpeg .mp3
AddType image/jpeg .jpg .jpeg
AddType image/png .png
AddType image/webp .webp
"""


def _is_root():
    return os.geteuid() == 0


def _sudo_prefix():
    """Prefix a command with sudo when running unprivileged.

    Validation and reload must run as root: they read TLS private keys that
    Let's Encrypt keeps root-only and signal a root-owned master process.
    """
    if _is_root():
        return []
    sudo = shutil.which("sudo")
    if sudo is None:
        raise RuntimeError(
            "Correcting the web server needs root. Install sudo or run Termicast as "
            "root (for example: sudo termicast fix-host-mime <id>).")
    return [sudo]


def detect_servers():
    """Return installed control tools, not a claim about who serves a URL."""
    found = {}
    for name, candidates in (("Nginx", ("nginx",)),
                             ("Apache", ("apache2ctl", "apachectl", "httpd"))):
        for candidate in candidates:
            executable = shutil.which(candidate)
            if executable is None:
                for directory in ("/usr/sbin", "/usr/local/sbin", "/sbin"):
                    path = Path(directory) / candidate
                    if path.is_file() and os.access(path, os.X_OK):
                        executable = str(path)
                        break
            if executable:
                found[name] = executable
                break
    return found


def run_control(executable, *args):
    """Run a server control command (test/reload), elevating when needed."""
    command = [*_sudo_prefix(), executable, *args]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=CONTROL_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Web-server command timed out after {CONTROL_TIMEOUT} seconds") from exc
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or
                           "Web-server command failed")


def _read_bytes(path):
    """Read a config file, falling back to sudo for root-only files."""
    try:
        return path.read_bytes()
    except PermissionError:
        result = subprocess.run([*_sudo_prefix(), "cat", str(path)], capture_output=True)
        if result.returncode:
            detail = (result.stderr or b"").decode().strip() or "permission denied"
            raise RuntimeError(f"Cannot read {path}: {detail}")
        return result.stdout


def _write_bytes(path, data):
    """Write a config file, using sudo when the current account cannot write."""
    if os.access(path, os.W_OK):
        path.write_bytes(data)
        return
    handle, temporary = tempfile.mkstemp(prefix="termicast-restore-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        result = subprocess.run([*_sudo_prefix(), "cp", temporary, str(path)],
                                capture_output=True, text=True)
        if result.returncode:
            raise OSError(result.stderr.strip() or "restore failed")
    finally:
        Path(temporary).unlink(missing_ok=True)


def _backup(path, data):
    """Snapshot one config file to a private, owner-only temp location."""
    backup = Path(tempfile.mkdtemp(prefix="termicast-server-backup-")) / path.name
    with backup.open("xb") as stream:
        os.chmod(backup, 0o600)
        stream.write(data)
    return backup


def mime_snippet(name):
    """The clean MIME block to add, for display or copy-paste."""
    return NGINX_MIME_BLOCK if name == "Nginx" else APACHE_MIME_BLOCK


def _join_after(lines, index, block):
    return lines[:index + 1] + [""] + block + lines[index + 1:]


def _insert_block(name, content):
    """Return the config with the MIME block inserted, or None if no anchor.

    Nginx: after the first `server_name`/`root` directive, else after `server {`.
    Apache: after the first `<VirtualHost>`/`<Directory>` opening.
    """
    lines = content.splitlines()
    block = mime_snippet(name).rstrip("\n").split("\n")
    if name == "Nginx":
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("server_name "):
                return _join_after(lines, index, block)
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("root "):
                return _join_after(lines, index, block)
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("server") and stripped.endswith("{"):
                return _join_after(lines, index, block)
        return None
    for index, line in enumerate(lines):
        if "<VirtualHost" in line or "<Directory" in line:
            return _join_after(lines, index, block)
    return None


def _find_existing_block(name, lines):
    """Locate an existing Termicast MIME block, or None if absent.

    Returns `(start, end)` inclusive line indices. Nginx blocks run from the
    marker comment to their closing brace; Apache blocks are the run of
    `AddType` lines immediately after the marker.
    """
    start = None
    for index, line in enumerate(lines):
        if MIME_MARKER in line:
            start = index
            break
    if start is None:
        return None
    if name == "Nginx":
        for index in range(start, len(lines)):
            if lines[index].strip() == "}":
                return start, index
        return None
    end = start
    while end + 1 < len(lines) and lines[end + 1].strip().startswith("AddType"):
        end += 1
    return start, end


def apply_mime_patch(name, path, executable):
    """Insert or upgrade the MIME block, validate, and restore on failure.

    Returns `(backup, changed)` where `changed` is False only when the current
    block is already present and up to date. Does not reload; the caller offers
    that separately.
    """
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("Choose an existing site configuration file")
    original = _read_bytes(path)
    content = original.decode("utf-8", errors="replace")
    lines = content.splitlines()
    block = mime_snippet(name).rstrip("\n").split("\n")
    bounds = _find_existing_block(name, lines)
    if bounds is not None and lines[bounds[0]:bounds[1] + 1] == block:
        return None, False
    if bounds is not None:
        start, end = bounds
        new_lines = lines[:start] + block + lines[end + 1:]
    else:
        new_lines = _insert_block(name, content)
        if new_lines is None:
            raise ValueError(
                "Could not find a server block to patch in the configuration. Add the "
                "MIME mappings manually inside the site's server/Directory block.")
    backup = _backup(path, original)
    _write_bytes(path, ("\n".join(new_lines) + "\n").encode("utf-8"))
    try:
        run_control(executable, "-t")
    except BaseException as exc:
        _write_bytes(path, original)
        if isinstance(exc, Exception):
            raise RuntimeError(
                f"Configuration restored after failed validation: {exc}. "
                f"Backup: {backup}") from exc
        raise
    return backup, True



def reload_server(name, executable):
    run_control(executable, "-t")
    args = ("-s", "reload") if name == "Nginx" else ("-k", "graceful")
    run_control(executable, *args)

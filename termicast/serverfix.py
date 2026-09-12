"""Explicit, interactive site-configuration repair support."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile


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
    try:
        result = subprocess.run([executable, *args], capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Web-server command timed out after 30 seconds") from exc
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or
                           "Web-server command failed")


def edit_site_config(path, executable):
    """Back up one explicit file, edit, validate, and restore on failure.

    The caller confirms this file belongs to the selected server's default
    configuration. Reload is a separate explicit action.
    """
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("Choose an existing site configuration file")
    editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
    if not editor or shutil.which(editor[0]) is None:
        raise ValueError("Set VISUAL or EDITOR to an installed terminal editor")
    run_control(executable, "-t")
    original = path.read_bytes()
    # Keep backups outside include directories (sites-enabled/* may load any name).
    backup = Path(tempfile.mkdtemp(prefix="termicast-server-backup-")) / path.name
    # Exclusive creation and owner-only permissions; server configs may hold secrets.
    with backup.open("xb") as stream:
        os.chmod(backup, 0o600)
        stream.write(original)
    try:
        result = subprocess.run([*editor, str(path)])
        if result.returncode:
            raise RuntimeError("Editor exited unsuccessfully")
        changed = path.read_bytes() != original
        if changed:
            run_control(executable, "-t")
        return backup, changed
    except BaseException as exc:
        try:
            path.write_bytes(original)
        except OSError as restore_exc:
            raise RuntimeError(f"Could not restore configuration; restore {backup} to {path}: "
                               f"{restore_exc}. Original failure: {exc}") from exc
        if isinstance(exc, Exception):
            raise RuntimeError(f"Configuration restored after failed edit/check: {exc}. "
                               f"Backup: {backup}") from exc
        raise


def reload_server(name, executable):
    run_control(executable, "-t")
    args = ("-s", "reload") if name == "Nginx" else ("-k", "graceful")
    run_control(executable, *args)

"""Public asset locations and hosting-settings validation.

`base_url` is the public root of the show's output directory; `feed.xml` always
lives there and is served by the web server. For S3 hosting, media assets are
uploaded to a bucket/prefix whose public root is `asset_base_url`; the feed
stays on the web server and references those S3 URLs. `output_dir` is the local
directory that holds `feed.xml` and the working copy of the assets.
"""

import shutil
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .validation import validate_https


def asset_root(show):
    return Path(show["output_dir"]).expanduser().absolute()


def local_delete_targets(show):
    """Existing local paths that "Delete podcast" would remove.

    Only paths Termicast itself is known to create -- the managed asset
    folders, the feed, and its lock files (`publisher.operation_lock`/
    `output_lock`) -- are ever considered here, so an output_dir shared with
    unrelated files is left otherwise untouched.
    """
    from .rename import MANAGED_FOLDERS
    root = asset_root(show)
    names = list(MANAGED_FOLDERS) + ["feed.xml", ".termicast.oplock", ".termicast.lock"]
    return [path for path in (root / name for name in names) if path.exists()]


def delete_local_assets(show, dry_run=False):
    """Remove the paths from `local_delete_targets`, then the directory if now empty.

    A directory that still holds unrelated files (explicitly supported --
    Termicast never assumes it owns the whole output_dir) is left in place.
    """
    targets = local_delete_targets(show)
    if dry_run:
        return targets
    for path in targets:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    try:
        asset_root(show).rmdir()
    except OSError:
        pass
    return targets


def feed_url(show):
    return show["base_url"].rstrip("/") + "/feed.xml"


def asset_base(show):
    """Public root for media assets (S3 bucket/prefix, or the local web root)."""
    if show.get("hosting") == "s3":
        return (show.get("asset_base_url") or show.get("base_url", "")).rstrip("/")
    return show["base_url"].rstrip("/")


def local_relative(show, url):
    """Relative asset path for a public URL of this show, or None if foreign.

    The inverse of `asset_base(show) + "/" + relative`. Used to find the file
    behind a feed URL when the episode record carries no stored path, as
    imported episodes do.
    """
    if not url:
        return None
    base = urlsplit(asset_base(show) + "/")
    parts = urlsplit(url)
    if (parts.scheme, parts.netloc) != (base.scheme, base.netloc):
        return None
    if not parts.path.startswith(base.path):
        return None
    relative = unquote(parts.path[len(base.path):])
    return Path(relative) if relative else None


def validate_storage(show):
    errors = []
    if show.get("hosting", "local") not in ("local", "s3"):
        errors.append("Hosting must be local or s3")
    if show.get("hosting") == "s3":
        if not show.get("bucket") or "/" in show["bucket"]:
            errors.append("S3 bucket name is required and must not include a path")
        if show.get("endpoint_url") and (not validate_https(show["endpoint_url"])
                                         or urlsplit(show["endpoint_url"]).query
                                         or urlsplit(show["endpoint_url"]).fragment):
            errors.append("S3 endpoint must be an absolute HTTPS URL without query or fragment")
        if not show.get("asset_base_url"):
            errors.append("S3 hosting requires a public asset base URL")
        elif (not validate_https(show["asset_base_url"])
              or urlsplit(show["asset_base_url"]).query
              or urlsplit(show["asset_base_url"]).fragment):
            errors.append("asset_base_url must be an absolute HTTPS URL without query or fragment")
        prefix = show.get("prefix", "")
        if prefix and (prefix.startswith("/") or prefix.endswith("/")
                       or any(part in ("", ".", "..") for part in prefix.split("/"))):
            errors.append("S3 prefix must have no leading/trailing slash or empty/dot components")
    if "enabled" in show and not isinstance(show["enabled"], bool):
        errors.append("enabled must be a boolean")
    if "keep_local_media" in show and not isinstance(show["keep_local_media"], bool):
        errors.append("keep_local_media must be a boolean")
    if "mirror_feed" in show and not isinstance(show["mirror_feed"], bool):
        errors.append("mirror_feed must be a boolean")
    return errors


def validate_conflicting_prefixes(shows):
    """Reject two S3 shows whose normalized bucket/prefix ranges overlap."""
    from urllib.parse import urlsplit
    seen = {}
    for show in shows:
        if show.get("hosting") != "s3" or not show.get("bucket"):
            continue
        endpoint = urlsplit(show.get("endpoint_url") or "").netloc.lower()
        bucket = show["bucket"].lower()
        prefix = tuple(p for p in show.get("prefix", "").split("/") if p)
        key = (endpoint, bucket)
        for other_endpoint, other_bucket, other_prefix in seen.get(key, ()):
            shorter, longer = sorted((prefix, other_prefix), key=len)
            if list(longer[:len(shorter)]) == list(shorter):
                raise ValueError(
                    f"S3 target prefix {show.get('prefix') or '(root)'} conflicts with an existing show "
                    f"using the same bucket {show['bucket']}")
        seen.setdefault(key, []).append((endpoint, bucket, prefix))

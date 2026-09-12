"""Public asset locations and hosting-settings validation.

`base_url` is the public root of the show's output directory; `feed.xml` always
lives there and is served by the web server. For S3 hosting, media assets are
uploaded to a bucket/prefix whose public root is `asset_base_url`; the feed
stays on the web server and references those S3 URLs. `output_dir` is the local
directory that holds `feed.xml` and the working copy of the assets.
"""

from pathlib import Path
from urllib.parse import urlsplit

from .validation import validate_https


FOLDERS = ("audio", "images", "transcripts", "chapters")


def asset_root(show):
    return Path(show["output_dir"]).expanduser().absolute()


def feed_url(show):
    return show["base_url"].rstrip("/") + "/feed.xml"


def asset_base(show):
    """Public root for media assets (S3 bucket/prefix, or the local web root)."""
    if show.get("hosting") == "s3":
        return (show.get("asset_base_url") or show.get("base_url", "")).rstrip("/")
    return show["base_url"].rstrip("/")


def hosting_kind(show):
    return "s3" if show.get("hosting") == "s3" else "local"


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
    return None

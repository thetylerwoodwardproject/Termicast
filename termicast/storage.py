"""Public asset locations and hosting-settings validation.

Termicast uses one public namespace per show. `base_url` is the public root of
that namespace; `output_dir` is the local directory that mirrors it (served
directly for local hosting, or used as the working copy for S3 deployment).
"""

from pathlib import Path
from urllib.parse import urlsplit

from .validation import validate_https


FOLDERS = ("audio", "images", "transcripts", "chapters")


def asset_root(show):
    return Path(show["output_dir"]).expanduser().absolute()


def asset_base(show):
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
        prefix = show.get("prefix", "")
        if prefix and (prefix.startswith("/") or prefix.endswith("/")
                       or any(part in ("", ".", "..") for part in prefix.split("/"))):
            errors.append("S3 prefix must have no leading/trailing slash or empty/dot components")
    if "enabled" in show and not isinstance(show["enabled"], bool):
        errors.append("enabled must be a boolean")
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

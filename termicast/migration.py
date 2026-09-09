"""Guidance for moving an externally hosted feed to Termicast."""


def migration_guidance(show: dict, source: str = "") -> str:
    destination = show["base_url"].rstrip("/") + "/feed.xml"
    return (
        f"Replacement feed: {destination}\n"
        f"Local feed: {show['output_dir']}/feed.xml\n"
        "Serve the output directory over HTTPS and verify the replacement feed and "
        "chapter files are publicly accessible before switching listeners.\n"
        "Imported episode GUIDs and existing feed extensions are preserved.\n"
        f"Ask RSS.com (or the current host) to configure a permanent HTTP 301 redirect "
        f"from {source or 'the old feed URL'} to {destination}. Follow the host's "
        "migration procedure and verify the redirect afterward.\n"
        "Termicast cannot redirect an external RSS.com URL from this VPS. "
        "Do not shut down the old hosting before the redirect is active."
    )

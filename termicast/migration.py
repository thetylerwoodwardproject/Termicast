"""Guidance for moving an externally hosted feed to Termicast."""


def migration_guidance(show: dict, source: str = "") -> str:
    from .storage import asset_base, asset_root
    destination = show["base_url"].rstrip("/") + "/feed.xml"
    return (
        f"Replacement feed: {destination}\n"
        f"Local feed: {show['output_dir']}/feed.xml\n"
        f"Public assets: {asset_base(show)}/ (audio/, images/, transcripts/, chapters/)\n"
        f"Local assets / working copies: {asset_root(show)}\n"
        f"Deploy and verify: termicast deploy {show['id']}  and  termicast doctor {show['id']}\n"
        "Run hosting verification (doctor) and confirm the replacement feed and its "
        "chapters/media are publicly accessible before switching listeners.\n"
        "Imported episode GUIDs and existing feed extensions are preserved.\n"
        f"Ask the current host (for example RSS.com) to configure a permanent HTTP 301 "
        f"redirect from {source or 'the old feed URL'} to {destination}. Follow that "
        "host's migration procedure and verify the redirect afterward.\n"
        "Termicast cannot redirect an external provider's URL from this server, and it "
        "never deletes remote objects. Do not shut down the old hosting before the "
        "redirect is active."
    )

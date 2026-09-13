from termicast.storage import asset_root, local_delete_targets, delete_local_assets


def test_local_delete_targets_only_lists_existing_managed_paths(show):
    root = asset_root(show)
    root.mkdir(parents=True)
    (root / "audio").mkdir()
    (root / "feed.xml").write_text("<rss></rss>")
    (root / "notes.txt").write_text("unrelated")

    targets = local_delete_targets(show)

    assert root / "audio" in targets
    assert root / "feed.xml" in targets
    assert root / "chapters" not in targets
    assert root / "notes.txt" not in targets


def test_delete_local_assets_removes_managed_paths_and_empty_dir(show):
    root = asset_root(show)
    (root / "audio").mkdir(parents=True)
    (root / "audio" / "ep1.mp3").write_bytes(b"data")
    (root / "feed.xml").write_text("<rss></rss>")
    (root / ".termicast.lock").write_text("")

    delete_local_assets(show)

    assert not root.exists()


def test_delete_local_assets_keeps_directory_with_unrelated_files(show):
    root = asset_root(show)
    (root / "audio").mkdir(parents=True)
    (root / "audio" / "ep1.mp3").write_bytes(b"data")
    (root / "notes.txt").write_text("unrelated")

    delete_local_assets(show)

    assert root.is_dir()
    assert not (root / "audio").exists()
    assert (root / "notes.txt").exists()


def test_delete_local_assets_dry_run_leaves_files_in_place(show):
    root = asset_root(show)
    (root / "images").mkdir(parents=True)
    (root / "images" / "cover.jpg").write_bytes(b"data")

    targets = delete_local_assets(show, dry_run=True)

    assert root / "images" in targets
    assert (root / "images" / "cover.jpg").exists()


def test_delete_local_assets_is_idempotent(show):
    root = asset_root(show)
    (root / "audio").mkdir(parents=True)

    delete_local_assets(show)
    delete_local_assets(show)

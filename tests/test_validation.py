from termicast.models import new_show, new_episode
from termicast.validation import validate_show, validate_episode
from termicast.storage import validate_storage, validate_conflicting_prefixes


def test_show_defaults_validate():
    show = new_show(title="S", description="d", base_url="https://e.org/show", output_dir="/tmp/x")
    assert validate_show(show) == []


def test_invalid_audio_preset():
    show = new_show(title="S", description="d", base_url="https://e.org/show",
                    output_dir="/tmp/x", audio_preset="loud")
    assert any("audio_preset" in e for e in validate_show(show))


def test_invalid_image_preset():
    show = new_show(title="S", description="d", base_url="https://e.org/show",
                    output_dir="/tmp/x", image_preset="tiny")
    assert any("image_preset" in e for e in validate_show(show))


def test_s3_storage_requires_bucket():
    show = new_show(title="S", description="d", base_url="https://e.org/show",
                    output_dir="/tmp/x", hosting="s3")
    assert any("bucket" in e for e in validate_storage(show))


def test_s3_storage_requires_asset_base_url():
    show = new_show(title="S", description="d", base_url="https://e.org/show",
                    output_dir="/tmp/x", hosting="s3", bucket="b")
    assert any("asset base URL" in e for e in validate_storage(show))
    show["asset_base_url"] = "https://cdn.e.org/show"
    assert validate_storage(show) == []


def test_asset_base_uses_asset_base_url_for_s3():
    from termicast.storage import asset_base
    show = new_show(base_url="https://e.org/show", asset_base_url="https://cdn.e.org/show", hosting="s3")
    assert asset_base(show) == "https://cdn.e.org/show"
    show["hosting"] = "local"
    assert asset_base(show) == "https://e.org/show"


def test_s3_prefix_rules():
    show = new_show(title="S", description="d", base_url="https://e.org/show",
                    output_dir="/tmp/x", hosting="s3", bucket="b", prefix="/bad")
    assert any("prefix" in e for e in validate_storage(show))
    show["prefix"] = "good/prefix"
    assert not any("prefix" in e for e in validate_storage(show))


def test_conflicting_prefixes_rejected():
    a = new_show(hosting="s3", bucket="b", prefix="show", endpoint_url="https://s3.e.org")
    b = new_show(hosting="s3", bucket="b", prefix="show/extra", endpoint_url="https://s3.e.org")
    try:
        validate_conflicting_prefixes([a, b])
        assert False, "expected conflict"
    except ValueError as exc:
        assert "conflict" in str(exc)


def test_save_show_rejects_conflicting_s3_prefix(db, tmp_path):
    def s3_show(prefix, output_dir):
        return new_show(title="S", description="d", base_url="https://e.org/show",
                        output_dir=str(output_dir), hosting="s3", bucket="b", prefix=prefix,
                        endpoint_url="https://s3.e.org", asset_base_url="https://s3.e.org/b")

    db.save_show(s3_show("show", tmp_path / "a"))
    try:
        db.save_show(s3_show("show/extra", tmp_path / "b"))
        assert False, "expected conflict"
    except ValueError as exc:
        assert "conflict" in str(exc)


def test_episode_slug_validation():
    episode = new_episode(title="E", description="d", mp3_url="https://e.org/x.mp3",
                          length=1, duration=1.0, slug="s01e001")
    assert validate_episode(episode) == []
    episode["slug"] = "bad/slug"
    assert any("slug" in e for e in validate_episode(episode))

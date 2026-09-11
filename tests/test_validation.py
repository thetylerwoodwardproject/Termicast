from termicast.models import new_show, new_episode
from termicast.validation import validate_show, validate_episode, validate_presets
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


def test_episode_slug_validation():
    episode = new_episode(title="E", description="d", mp3_url="https://e.org/x.mp3",
                          length=1, duration=1.0, slug="s01e001")
    assert validate_episode(episode) == []
    episode["slug"] = "bad/slug"
    assert any("slug" in e for e in validate_episode(episode))

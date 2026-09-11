import shutil
from pathlib import Path

import pytest

from termicast.media import (
    identify_files, prepare_media, content_type_for, ENCLOSURE_TYPES,
    AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, TRANSCRIPT_EXTENSIONS,
    _prepare_image, _prepare_audio,
)
from termicast.models import new_show
from conftest import make_wav, make_png, make_jpg, make_vtt


FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def test_identify_files():
    roles = identify_files(["a.mp3", "b.jpg", "c.vtt"])
    assert roles == {"audio": "a.mp3", "image": "b.jpg", "transcript": "c.vtt"}


def test_identify_files_requires_audio():
    with pytest.raises(ValueError, match="Audio is required"):
        identify_files(["b.jpg"])


def test_identify_files_rejects_two_audio():
    with pytest.raises(ValueError, match="Exactly one audio"):
        identify_files(["a.mp3", "b.wav"])


def test_identify_files_rejects_two_images():
    with pytest.raises(ValueError, match="Exactly one cover image"):
        identify_files(["a.mp3", "b.jpg", "c.png"])


def test_identify_files_rejects_unknown_type():
    with pytest.raises(ValueError, match="Unsupported file type"):
        identify_files(["a.mp3", "b.txt"])


def test_content_type_for():
    assert content_type_for("feed.xml") == "application/rss+xml"
    assert content_type_for("chapters/x.json") == "application/json+chapters"
    assert content_type_for("transcripts/x.vtt") == "text/vtt"
    assert content_type_for("audio/x.mp3") == "audio/mpeg"
    assert content_type_for("audio/x.m4a") == "audio/mp4"
    assert content_type_for("images/x.jpg") == "image/jpeg"
    assert content_type_for("audio/x.unknown") is None


def test_enclosure_types():
    assert ENCLOSURE_TYPES[".mp3"] == "audio/mpeg"
    assert ENCLOSURE_TYPES[".flac"] == "audio/flac"
    assert set(AUDIO_EXTENSIONS) >= {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus"}
    assert set(IMAGE_EXTENSIONS) == {".jpg", ".jpeg", ".png"}
    assert TRANSCRIPT_EXTENSIONS == {".vtt"}


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_prepare_media_end_to_end(tmp_path):
    show = new_show(base_url="https://e.org/show", output_dir=str(tmp_path / "out"))
    wav = make_wav(tmp_path / "tone.wav")
    png = make_png(tmp_path / "art.png")
    vtt = make_vtt(tmp_path / "sub.vtt")
    prepared = prepare_media(show, identify_files([str(wav), str(png), str(vtt)]),
                             slug="s01e001")
    assert prepared.audio_relative == "audio/s01e001.mp3"
    assert prepared.audio_meta["codec"] == "mp3"
    assert prepared.audio_meta["sample_rate"] == 44100
    assert prepared.audio_meta["duration"] > 0
    assert prepared.image_relative == "images/s01e001.jpg"
    assert prepared.transcript_relative == "transcripts/s01e001.vtt"
    for relative in (prepared.audio_relative, prepared.image_relative, prepared.transcript_relative):
        assert (tmp_path / "out" / relative).is_file()


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")
def test_keep_audio_preserves_deliverable_format(tmp_path):
    source = make_wav(tmp_path / "tone.wav")
    dest = tmp_path / "out"
    dest.mkdir()
    _, relative, _, _, meta, note = _prepare_audio(source, dest / "audio", "s01e001", "standard", True)
    assert relative == "audio/s01e001.wav"
    assert note == "kept original audio"
    assert (dest / relative).is_file()


def test_image_optimizes_png_to_jpeg(tmp_path):
    source = make_png(tmp_path / "art.png")
    dest = tmp_path / "out"
    dest.mkdir()
    _, relative, _, _, dims, note = _prepare_image(source, dest / "images", "s01e001", "compact", False)
    assert relative == "images/s01e001.jpg"
    assert dims == (1400, 1400)
    assert (dest / relative).is_file()


def test_image_preserves_small_jpeg(tmp_path):
    source = make_jpg(tmp_path / "art.jpg")
    dest = tmp_path / "out"
    dest.mkdir()
    original = source.read_bytes()
    _, relative, _, _, _, note = _prepare_image(source, dest / "images", "s01e001", "compact", False)
    assert note == "preserved suitable JPEG"
    assert (dest / relative).read_bytes() == original


def test_keep_image_preserves_png(tmp_path):
    source = make_png(tmp_path / "art.png")
    dest = tmp_path / "out"
    dest.mkdir()
    _, relative, _, _, dims, note = _prepare_image(source, dest / "images", "s01e001", "compact", True)
    assert relative == "images/s01e001.png"
    assert dims == (1400, 1400)

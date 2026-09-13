import shutil

import pytest

from termicast.media import (
    identify_files, prepare_media, content_type_for, ENCLOSURE_TYPES,
    AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, TRANSCRIPT_EXTENSIONS,
    _prepare_image, _prepare_audio, _prepare_transcript, transcript_type,
)
from termicast.models import new_show
from conftest import make_wav, make_png, make_jpg, make_vtt


FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def test_identify_files():
    roles = identify_files(["a.mp3", "b.jpg", "c.vtt"])
    assert roles == {"audio": "a.mp3", "image": "b.jpg", "transcript": "c.vtt"}


def test_identify_files_accepts_srt_transcript():
    roles = identify_files(["a.mp3", "c.srt"])
    assert roles["transcript"] == "c.srt"


def test_prepare_transcript_converts_srt(tmp_path):
    source = tmp_path / "sub.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    dest, relative, _, _ = _prepare_transcript(str(source), tmp_path, "s01e001")
    assert relative == "transcripts/s01e001.vtt"
    assert dest.read_text(encoding="utf-8").startswith("WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\n")


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
    assert content_type_for("transcripts/x.srt") == "application/x-subrip"
    assert content_type_for("audio/x.mp3") == "audio/mpeg"
    assert content_type_for("audio/x.m4a") == "audio/mp4"
    assert content_type_for("images/x.jpg") == "image/jpeg"
    assert content_type_for("audio/x.unknown") is None


def test_transcript_type_by_suffix():
    assert transcript_type("https://e.org/t.vtt") == "text/vtt"
    assert transcript_type("https://e.org/t.srt") == "application/x-subrip"
    assert transcript_type("https://e.org/t.json") == "application/json"
    assert transcript_type("https://e.org/t") == "text/vtt"


def test_enclosure_types():
    assert ENCLOSURE_TYPES[".mp3"] == "audio/mpeg"
    assert ENCLOSURE_TYPES[".flac"] == "audio/flac"
    assert set(AUDIO_EXTENSIONS) >= {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus"}
    assert set(IMAGE_EXTENSIONS) == {".jpg", ".jpeg", ".png", ".webp"}
    assert TRANSCRIPT_EXTENSIONS == {".vtt", ".srt"}


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg/ffprobe not installed")
@pytest.mark.parametrize("suffix,keep_image,expected_suffix", [
    (".png", False, ".jpg"), (".jpg", False, ".jpg"),
    (".png", True, ".png"), (".jpg", True, ".jpg"),
])
def test_prepare_media_end_to_end(tmp_path, suffix, keep_image, expected_suffix):
    show = new_show(base_url="https://e.org/show", output_dir=str(tmp_path / "out"))
    wav = make_wav(tmp_path / "tone.wav")
    image = (make_png if suffix == ".png" else make_jpg)(tmp_path / ("art" + suffix))
    vtt = make_vtt(tmp_path / "sub.vtt")
    prepared = prepare_media(show, identify_files([str(wav), str(image), str(vtt)]),
                             slug="s01e001", keep_image=keep_image)
    assert prepared.audio_relative == "audio/s01e001.mp3"
    assert prepared.audio_meta["codec"] == "mp3"
    assert prepared.audio_meta["sample_rate"] == 44100
    assert prepared.audio_meta["duration"] > 0
    assert prepared.image_relative == "images/episodes/s01e001" + expected_suffix
    assert prepared.image_path == tmp_path / "out" / prepared.image_relative
    assert not (tmp_path / "out/images" / ("s01e001" + expected_suffix)).exists()
    if keep_image or suffix == ".jpg":
        assert prepared.image_path.read_bytes() == image.read_bytes()
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


def test_prepared_review_reports_how_media_was_handled(tmp_path):
    """The audio/image notes are computed during preparation, so show them.

    Without these the review panel says what the output is but not what was
    done to get there -- converted, passed through, or kept as the original.
    """
    from termicast.media import Prepared

    prepared = Prepared(
        audio_path=tmp_path / "a.mp3", audio_relative="audio/a.mp3",
        audio_before=200, audio_after=100,
        audio_meta={"codec": "mp3", "sample_rate": 44100, "channels": 2, "duration": 1.0},
        audio_note="converted to MP3 128 kbps / 44100 Hz",
        image_path=tmp_path / "a.jpg", image_relative="images/episodes/a.jpg",
        image_before=300, image_after=150, image_note="kept original artwork",
        image_dims=(1400, 1400),
    )
    review = prepared.review()
    assert review["audio_handling"] == "converted to MP3 128 kbps / 44100 Hz"
    assert review["image_handling"] == "kept original artwork"


def test_webp_identification_and_conversion(tmp_path):
    from PIL import Image
    from termicast.validation import inspect_local_artwork
    source = tmp_path / "art.WEBP"
    Image.new("RGBA", (1400, 1400), (255, 0, 0, 0)).save(source)
    assert identify_files(["e.mp3", source])["image"] == str(source)
    dest, relative, _, _, dims, note = _prepare_image(source, tmp_path / "out", "ep001", "compact", False)
    assert relative == "images/ep001.jpg"
    assert dims == (1400, 1400)
    assert "WEBP to JPEG" in note
    assert inspect_local_artwork(dest) == []
    with Image.open(dest) as image:
        assert image.getpixel((0, 0)) == (255, 255, 255)
    with pytest.raises(ValueError, match="Drop --keep-image"):
        _prepare_image(source, tmp_path / "out", "ep002", "compact", True)


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("kind", ["show", "episode", "chapter"])
def test_install_artwork(tmp_path, monkeypatch, remote, kind):
    from PIL import Image
    from termicast import validation
    from termicast.media import install_artwork
    show = new_show(output_dir=str(tmp_path / "out"), base_url="https://e.org/show",
                    hosting="s3", asset_base_url="https://cdn.e.org/show")
    source = tmp_path / "wide.webp"
    Image.new("RGB", (800, 400), (255, 0, 0)).save(source)
    original = source.read_bytes()
    downloads = []

    def download(url, handle, limit):
        downloads.append((url, limit))
        handle.write(original)

    monkeypatch.setattr(validation, "_download", download)
    url = "https://source.e.org/wide.webp"
    folder = "images/chapters" if kind == "chapter" else "images"
    relative, public = install_artwork(show, url if remote else source, stem="ep001", folder=folder, kind=kind)
    assert relative == f"{folder}/ep001.jpg"
    assert public == f"https://cdn.e.org/show/{relative}"
    assert downloads == ([(url, validation.MAX_ARTWORK_BYTES)] if remote else [])
    dest = tmp_path / "out" / relative
    assert validation.inspect_local_artwork(dest, episode=kind == "episode", chapter=kind == "chapter") == []
    with Image.open(dest) as image:
        assert image.size == ((800, 400) if kind == "chapter" else (3000, 3000))
        if kind != "chapter":
            assert all(c >= 220 for c in image.getpixel((1500, 0)))
    assert source.read_bytes() == original


def test_install_artwork_pads_small_jpeg_instead_of_fast_copy(tmp_path):
    from termicast.media import install_artwork
    from termicast.validation import inspect_local_artwork
    show = new_show(output_dir=str(tmp_path / "out"), base_url="https://e.org/show")
    source = make_jpg(tmp_path / "small.jpg")
    relative, _ = install_artwork(show, source, stem="cover", kind="episode")
    assert inspect_local_artwork(tmp_path / "out" / relative, episode=True) == []

"""Shared pytest fixtures for Termicast v2."""

import wave
from pathlib import Path

import pytest

from termicast.database import Database
from termicast.models import new_show


@pytest.fixture(autouse=True)
def plain_console(monkeypatch):
    """Assert on prompt text, not on ANSI codes a developer's FORCE_COLOR adds.

    Rich resolves the color system when the Console is built at import time, so
    the settings have to be turned off on the live object rather than the
    environment.
    """
    from termicast import prompts
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setattr(prompts.console, "_color_system", None, raising=False)
    monkeypatch.setattr(prompts.console, "_force_terminal", False, raising=False)
    monkeypatch.setattr(prompts.console, "no_color", True, raising=False)


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    home = tmp_path / "state"
    monkeypatch.setenv("TERMICAST_HOME", str(home))
    return home


@pytest.fixture
def db():
    return Database()


@pytest.fixture
def show(db, tmp_path):
    record = new_show(
        title="Test podcast", description="A test podcast", author="Test author",
        owner_name="Test owner", owner_email="owner@example.org",
        website="https://example.org/show", category="Technology",
        base_url="https://example.org/show", output_dir=str(tmp_path / "output"),
        timezone="UTC", explicit=False,
    )
    return db.save_show(record)


def make_wav(path, seconds=0.2, channels=2, rate=44100):
    frames = int(seconds * rate)
    silence = b"\x00" * (frames * channels * 2)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(silence)
    return Path(path)


def make_png(path, size=(1400, 1400)):
    from PIL import Image
    Image.new("RGB", size, (200, 30, 30)).save(path)
    return Path(path)


def make_jpg(path, size=(1400, 1400)):
    from PIL import Image
    Image.new("RGB", size, (30, 200, 30)).save(path, "JPEG", quality=90)
    return Path(path)


def make_vtt(path):
    Path(path).write_text("WEBVTT\n\n00:00.000 --> 00:00.200\nHello\n", encoding="utf-8")
    return Path(path)

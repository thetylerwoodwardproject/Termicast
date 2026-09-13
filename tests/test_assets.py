import pytest

from termicast.assets import check_srt, check_vtt


SRT = ("1\r\n"
       "00:00:01,000 --> 00:00:04,000\r\n"
       "Hello <i>there</i>\r\n"
       "\r\n"
       "2\r\n"
       "00:00:04,500 --> 00:00:06,250\r\n"
       "Goodbye\r\n")


def test_webvtt_passes_through():
    text = "WEBVTT\n\n00:00.000 --> 00:00.200\nHello\n"
    assert check_vtt(text) == text


def test_webvtt_without_cues_rejected():
    with pytest.raises(ValueError, match="at least one timed cue"):
        check_vtt("WEBVTT\n\nNOTE nothing here\n")


def test_srt_converts_to_webvtt():
    assert check_vtt(SRT) == ("WEBVTT\n\n"
                              "1\n00:00:01.000 --> 00:00:04.000\nHello <i>there</i>\n\n"
                              "2\n00:00:04.500 --> 00:00:06.250\nGoodbye\n")


def test_srt_with_bom_and_short_fields_converts():
    text = check_vtt("﻿1\n1:2:3,4 --> 1:2:5,25\nLate cue\n")
    assert text.startswith("WEBVTT\n\n1\n01:02:03.400 --> 01:02:05.250\n")


def test_srt_coordinates_dropped_and_cue_settings_kept():
    text = check_vtt("1\n00:00:01,000 --> 00:00:02,000 X1:40 X2:600 Y1:20 Y2:50 align:start\nHi\n")
    assert "00:00:01.000 --> 00:00:02.000 align:start\n" in text
    assert "X1" not in text


def test_headerless_webvtt_gains_a_header():
    assert check_vtt("00:00:01.000 --> 00:00:02.000\nHi\n") == (
        "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n")


def test_untimed_text_rejected():
    with pytest.raises(ValueError, match="WebVTT or SubRip"):
        check_vtt("Just some prose about the episode.\n")


def test_check_srt_returns_subrip_unchanged():
    assert check_srt(SRT) == SRT


def test_check_srt_rejects_untimed_text():
    with pytest.raises(ValueError, match="SubRip"):
        check_srt("<html>not a transcript</html>")

"""Low-resolution notice: only when the longest side is below the threshold (default 960 px, configurable)."""

from swingguru.app_api import DEFAULT_LOW_RES_LONG_SIDE, LOW_RES_NOTICE, quality_notice


def test_low_resolution_gets_the_notice():
    assert quality_notice({"width": 304, "height": 540}) == LOW_RES_NOTICE        # 540 px tall (degraded in the study)
    assert quality_notice({"width": 540, "height": 960 - 1}) == LOW_RES_NOTICE


def test_whatsapp_and_tiktok_sized_portrait_clips_do_not():
    assert quality_notice({"width": 608, "height": 1080}) is None
    assert quality_notice({"width": 576, "height": 1024}) is None
    assert quality_notice({"width": 1080, "height": 1920}) is None
    assert quality_notice({"width": 1280, "height": 720}) is None                 # landscape 720p: long side 1280


def test_threshold_is_configurable(monkeypatch):
    assert DEFAULT_LOW_RES_LONG_SIDE == 960
    monkeypatch.setenv("SG_LOW_RES_LONG_SIDE", "1100")
    assert quality_notice({"width": 608, "height": 1080}) == LOW_RES_NOTICE
    monkeypatch.setenv("SG_LOW_RES_LONG_SIDE", "not a number")
    assert quality_notice({"width": 608, "height": 1080}) is None                 # falls back to 960


def test_unknown_size_says_nothing():
    assert quality_notice({}) is None

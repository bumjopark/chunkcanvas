"""OCR'd Korean gets its word spacing back; other text is left alone."""
import pytest

from app.parsers.spacing import restore_spacing

pytest.importorskip("kiwipiepy")


def test_unspaced_korean_ocr_lines_get_spaces_back():
    fixed = restore_spacing("기존기술현황\n최근10년간자연재해는총186건으로인명피해가발생함")
    first, second = fixed.split("\n")
    assert first.replace(" ", "") == "기존기술현황" and " " in first
    assert second.replace(" ", "") == "최근10년간자연재해는총186건으로인명피해가발생함" and second.count(" ") >= 4


def test_spaces_already_read_are_kept_and_other_text_is_untouched():
    assert restore_spacing("Attention Is All You Need") == "Attention Is All You Need"
    assert " 예측함으로써 " in restore_spacing("위험상황을 예측함으로써 로봇과 작업자가")
    assert restore_spacing("") == ""

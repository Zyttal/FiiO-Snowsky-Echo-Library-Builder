"""Lock in the sanitization rules. Run with `pyenv exec python -m pytest tests/`."""
from src.config import Config
from src.sanitize import segment, track_filename


cfg = Config()


def test_ampersand_replaced_with_and():
    assert segment("Simon & Garfunkel", cfg) == "Simon and Garfunkel"


def test_double_quotes_stripped():
    assert segment('He Said "Hi"', cfg) == "He Said Hi"


def test_smart_quotes_stripped():
    assert segment("Octopus’s Garden", cfg) == "Octopuss Garden"


def test_forward_slash_stripped():
    assert segment("AC/DC", cfg) == "ACDC"


def test_trailing_dot_stripped():
    assert segment("Born in the U.S.A.", cfg) == "Born in the U.S.A"


def test_collapse_whitespace():
    assert segment("Foo   bar", cfg) == "Foo bar"


def test_empty_falls_back_to_unknown():
    assert segment("", cfg) == "Unknown"
    assert segment(None, cfg) == "Unknown"  # type: ignore[arg-type]


def test_track_filename_zero_padded():
    assert track_filename(3, "Yesterday", "flac", cfg) == "03 - Yesterday.flac"
    assert track_filename(12, "Foo", "mp3", cfg) == "12 - Foo.mp3"
    assert track_filename(None, "Foo", "flac", cfg) == "00 - Foo.flac"


def test_length_clamp():
    cfg2 = Config(max_segment_length=10)
    assert segment("A" * 50, cfg2) == "A" * 10

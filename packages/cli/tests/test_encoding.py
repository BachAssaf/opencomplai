"""Tests for the ASCII fallback that keeps the CLI alive on legacy code pages.

The regression these guard is not cosmetic: before ``_encoding``, running
``opencomplai --help`` with ``sys.stdout`` on cp437 - the default OEM code
page of a Windows console - raised ``UnicodeEncodeError`` part-way through
rendering the banner and exited 1.
"""

import io
import os
import subprocess
import sys

import pytest
from opencomplai_cli._encoding import (
    ASCII_FALLBACKS,
    ERROR_HANDLER_NAME,
    encoding_is_lossy,
    install_ascii_fallback,
)

BANNER = "Opencomplai — Open-source AI compliance for a trustworthy future."


def _lossy_stream(encoding: str = "cp437") -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, newline="")


def test_handler_transliterates_the_characters_the_cli_emits():
    install_ascii_fallback()  # registers the handler without touching streams
    text = "em—dash arrow→here section§ref"
    assert text.encode("cp437", errors=ERROR_HANDLER_NAME) == (
        b"em-dash arrow->here sectionSref"
    )


def test_handler_falls_back_to_question_mark_for_unknown_characters():
    install_ascii_fallback()
    # Not in the table, and not representable in cp437: must degrade rather
    # than raise, matching what a lossy terminal did before this module.
    assert "漢字".encode("cp437", errors=ERROR_HANDLER_NAME) == b"??"


def test_handler_refuses_to_touch_decode_errors():
    """Silently mangling *input* would be a much worse bug than mangling output."""
    install_ascii_fallback()
    with pytest.raises(UnicodeDecodeError):
        b"\xff\xfe".decode("utf-8", errors=ERROR_HANDLER_NAME)


@pytest.mark.parametrize(
    ("encoding", "expected"),
    [
        ("utf-8", False),
        ("utf-16", False),
        ("cp437", True),
        ("cp1252", True),  # has the em dash, but not the arrows
        ("ascii", True),
        ("not-a-real-codec", True),
        (None, False),
    ],
)
def test_encoding_is_lossy(encoding, expected):
    assert encoding_is_lossy(encoding) is expected


def test_install_leaves_utf8_streams_alone():
    stream = _lossy_stream("utf-8")
    assert install_ascii_fallback(stream) == []
    stream.write(BANNER)
    stream.flush()
    # The real glyph survives - this is the common case and must not regress.
    assert "—".encode() in stream.buffer.getvalue()


def test_install_reconfigures_a_lossy_stream():
    stream = _lossy_stream("cp437")
    assert install_ascii_fallback(stream) == [stream]
    stream.write(BANNER)
    stream.flush()
    assert b"Opencomplai - Open-source" in stream.buffer.getvalue()


def test_install_without_the_fix_would_raise():
    """Control: the same write on an untouched cp437 stream still blows up,
    proving the test above is exercising the fallback and not some ambient
    tolerance of the encoding."""
    stream = _lossy_stream("cp437")
    with pytest.raises(UnicodeEncodeError):
        stream.write(BANNER)


def test_install_is_idempotent():
    stream = _lossy_stream("cp437")
    assert install_ascii_fallback(stream) == [stream]
    assert install_ascii_fallback(stream) == []


def test_install_ignores_streams_that_cannot_be_reconfigured():
    assert install_ascii_fallback(io.StringIO()) == []


def test_every_fallback_is_pure_ascii():
    for source, replacement in ASCII_FALLBACKS.items():
        assert replacement.isascii(), f"{source!r} maps to non-ASCII {replacement!r}"


def test_help_survives_a_legacy_codepage():
    """End-to-end: the actual failure from the bug report."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp437"
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    env["COLUMNS"] = "80"

    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.argv = ['opencomplai', '--help']\n"
            "from opencomplai_cli.main import app\n"
            "app()\n",
        ],
        capture_output=True,
        env=env,
    )

    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    assert b"Opencomplai - Open-source AI compliance" in proc.stdout

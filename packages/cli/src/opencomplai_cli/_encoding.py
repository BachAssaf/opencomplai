"""ASCII degradation for terminals whose encoding cannot carry our output.

Opencomplai's help text, tables and log lines use a handful of typographic
characters - the em dash in the ``--help`` banner, the arrows in the scanner
output, the section sign in regulation references. On a Windows console left
on its default OEM code page (cp437) those characters are not representable,
and because ``sys.stdout`` is opened with the strict ``errors`` policy the
CLI does not merely print ``?``: it dies mid-render with

    UnicodeEncodeError: 'charmap' codec can't encode character '\\u2014'

after having already emitted a partial usage block, and exits 1.

Two fixes that look obvious do not work:

* **Forcing the stream to UTF-8.** The console still *decodes* using its own
  code page, so a UTF-8 em dash arrives as a three-character mojibake run.
  That is worse than ``?`` - it is wrong *and* it hides what went wrong.
* **Setting ``PYTHONUTF8`` / ``PYTHONIOENCODING`` at entry.** Both are read
  during interpreter start-up, long before any of our code is imported, so
  setting them from inside the process cannot affect the already-created
  ``sys.stdout``.

Instead we register a codec error handler that transliterates the characters
we actually emit down to ASCII, and attach it to stdout/stderr only when the
stream's encoding cannot represent them. On a UTF-8 terminal - the common
case, and every Linux/macOS run - nothing is touched and the real glyphs are
printed exactly as before.
"""

from __future__ import annotations

import codecs
from typing import IO, Any

#: Namespaced so we can never collide with a handler another library
#: registered under a generic name.
ERROR_HANDLER_NAME = "opencomplai_ascii_fallback"

#: Transliterations for the non-ASCII characters the CLI emits, plus the
#: typographic neighbours that reach us through user-supplied project names,
#: paths and regulation text. Anything outside this table still degrades to
#: ``?``, which is the behaviour a lossy terminal had before this module -
#: the point is that it no longer aborts the command.
#:
#: Keys are written as escapes rather than literal glyphs: several of them
#: are confusable with ASCII (ruff's RUF001 flags exactly that), and this is
#: the one place where naming the code point beats reading the glyph.
ASCII_FALLBACKS = {
    "\u2014": "-",  # EM DASH, in the --help banner
    "\u2013": "-",  # EN DASH
    "\u2192": "->",  # RIGHTWARDS ARROW, scanner output
    "\u2190": "<-",  # LEFTWARDS ARROW
    "\u00b7": "*",  # MIDDLE DOT, list separators
    "\u00a7": "S",  # SECTION SIGN, regulation references
    "\u2500": "-",  # BOX DRAWINGS LIGHT HORIZONTAL
    "\u2026": "...",  # HORIZONTAL ELLIPSIS
    "\u2018": "'",  # LEFT SINGLE QUOTATION MARK
    "\u2019": "'",  # RIGHT SINGLE QUOTATION MARK
    "\u201c": '"',  # LEFT DOUBLE QUOTATION MARK
    "\u201d": '"',  # RIGHT DOUBLE QUOTATION MARK
    "\u2022": "*",  # BULLET
    "\u00a0": " ",  # NO-BREAK SPACE
    "\u2713": "v",  # CHECK MARK
    "\u2717": "x",  # BALLOT X
}


def _ascii_fallback(error: UnicodeError) -> tuple[str, int]:
    """Replace an unencodable run with its ASCII transliteration."""
    if not isinstance(error, UnicodeEncodeError):
        # This handler is only meaningful for encoding; let decoding errors
        # surface rather than silently mangling input.
        raise error
    chunk = error.object[error.start : error.end]
    return "".join(ASCII_FALLBACKS.get(ch, "?") for ch in chunk), error.end


def encoding_is_lossy(encoding: str | None) -> bool:
    """Return True if ``encoding`` cannot represent every fallback key.

    Used to decide whether a stream needs the handler at all, so that UTF-8
    terminals keep their real glyphs.
    """
    if not encoding:
        return False
    try:
        "".join(ASCII_FALLBACKS).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return True
    return False


def install_ascii_fallback(*streams: IO[Any]) -> list[IO[Any]]:
    """Attach the ASCII fallback handler to each lossy stream.

    Returns the streams that were actually reconfigured, which makes the
    no-op case straightforward to assert in tests. Safe to call more than
    once, and safe when ``sys.stdout`` has been replaced by something that
    is not a :class:`io.TextIOWrapper` - pytest's capture, a ``StringIO``.
    """
    codecs.register_error(ERROR_HANDLER_NAME, _ascii_fallback)

    reconfigured: list[IO[Any]] = []
    for stream in streams:
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        if getattr(stream, "errors", None) == ERROR_HANDLER_NAME:
            continue
        if not encoding_is_lossy(getattr(stream, "encoding", None)):
            continue
        try:
            reconfigure(errors=ERROR_HANDLER_NAME)
        except (ValueError, OSError):
            # A detached or otherwise unreconfigurable stream is not worth
            # failing the command over - the pre-existing behaviour applies.
            continue
        reconfigured.append(stream)
    return reconfigured

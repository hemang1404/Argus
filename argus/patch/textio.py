"""Byte-faithful source text I/O: encoding, BOM and line-ending preservation.

Argus patches text, but the user's file is bytes. Everything that is tested in
the isolated workspace must be byte-identical to what is later applied to the
user's file, apart from the intended edit. This module provides the single
place where bytes <-> text conversion happens:

* ``decode_source`` honours a UTF-8 BOM and PEP 263 coding cookies and fails
  with :class:`UnsupportedEncodingError` instead of substituting U+FFFD.
* ``encode_source`` is the inverse (re-emits the BOM, uses the same encoding).
* ``normalize_newlines`` / ``restore_newlines`` let the matching engine work on
  ``\n`` text while the original line endings (CRLF, LF, lone CR, or a mix)
  are re-emitted for every line that was not changed.
"""

from __future__ import annotations

import codecs
import difflib
import io
import re
import tokenize
from dataclasses import dataclass
from typing import List

_EOL_RE = re.compile(r"\r\n|\r|\n")


class UnsupportedEncodingError(ValueError):
    """The file cannot be decoded (or re-encoded) without altering its bytes."""


@dataclass(frozen=True)
class DecodedSource:
    """Text of a source file plus what is needed to write the same bytes back."""

    text: str
    encoding: str = "utf-8"
    bom: bytes = b""


def decode_source(data: bytes, *, name: str = "<file>") -> DecodedSource:
    """Decode ``data`` strictly. The BOM is stripped from ``text`` and remembered."""
    bom = b""
    body = data
    if data.startswith(codecs.BOM_UTF8):
        bom = codecs.BOM_UTF8
        body = data[len(bom):]
        encoding = "utf-8"
    else:
        try:
            encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        except (SyntaxError, LookupError) as exc:
            raise UnsupportedEncodingError(
                f"{name}: invalid or unknown source encoding declaration ({exc})."
            ) from exc
    try:
        text = body.decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        raise UnsupportedEncodingError(
            f"{name}: cannot be decoded as {encoding} without altering its bytes ({exc}). "
            "Argus refuses to edit files it cannot round-trip."
        ) from exc
    return DecodedSource(text=text, encoding=encoding, bom=bom)


def encode_source(text: str, encoding: str = "utf-8", bom: bytes = b"", *, name: str = "<file>") -> bytes:
    """Encode ``text`` with the file's own encoding and BOM (no newline translation)."""
    try:
        return bom + text.encode(encoding)
    except (UnicodeEncodeError, LookupError) as exc:
        raise UnsupportedEncodingError(
            f"{name}: the patched text cannot be encoded as {encoding} ({exc})."
        ) from exc


def normalize_newlines(text: str) -> str:
    """Return ``text`` with every CRLF / lone CR / LF turned into ``\n``."""
    return _EOL_RE.sub("\n", text)


def _line_endings(text: str) -> List[str]:
    return [m.group(0) for m in _EOL_RE.finditer(text)]


def restore_newlines(original: str, patched_normalized: str) -> str:
    """Re-emit ``original``'s line endings on ``patched_normalized`` (``\n`` text).

    A uniformly-terminated original (all LF, all CRLF, all CR) is restored
    exactly. For mixed endings, lines that are unchanged keep their own ending
    and new/changed lines use the dominant ending.
    """
    eols = _line_endings(original)
    if not eols:
        return patched_normalized
    kinds = set(eols)
    if kinds == {"\n"}:
        return patched_normalized
    if len(kinds) == 1:
        return patched_normalized.replace("\n", eols[0])

    dominant = max(("\r\n", "\n", "\r"), key=lambda k: eols.count(k))
    orig_lines = _EOL_RE.split(original)
    new_lines = patched_normalized.split("\n")
    line_eol: dict[int, str] = {}
    matcher = difflib.SequenceMatcher(None, orig_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        # An edited line keeps the ending of the line it replaces.
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            for offset in range(i2 - i1):
                if i1 + offset < len(eols):
                    line_eol[j1 + offset] = eols[i1 + offset]
    out: List[str] = []
    last = len(new_lines) - 1
    for idx, line in enumerate(new_lines):
        out.append(line)
        if idx < last:
            out.append(line_eol.get(idx, dominant))
    return "".join(out)

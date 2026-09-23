"""Text the person typed, made safe to print in LaTeX.

The profile is plain text with three inline marks. Everything else in it is
literal: the ampersand in "R&D" is an ampersand, not a table column, and the
"%" in "10% faster" must not comment out the rest of the line. So escaping is
the default and markup is the exception, never the other way round.
"""
from __future__ import annotations

import re
import unicodedata


class Latex(str):
    """LaTeX that is already safe to print.

    The renderer passes these through untouched and escapes every other string,
    so a template that forgets a filter prints escaped text rather than running
    whatever the text happened to contain.
    """


_REPLACEMENTS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_SPECIAL = re.compile(r"[\\&%$#_{}~^]")
_NON_ASCII = re.compile(r"[^\x00-\x7f]")

# Templates on 8-bit fonts print only ASCII faithfully. Classic is one: lualatex
# with T1 fontenc and no fontspec turns a raw "×" into "Œ" and drops "ğ"
# entirely. The master always dodged this by hand ("Ozyegin", "$\times$"); here
# every non-ASCII character leaves as the LaTeX command that names it, which
# every engine and encoding reads the same way.
_ACCENTS = {
    "\u0300": "`",
    "\u0301": "'",
    "\u0302": "^",
    "\u0303": "~",
    "\u0304": "=",
    "\u0306": "u",
    "\u0307": ".",
    "\u0308": '"',
    "\u030a": "r",
    "\u030b": "H",
    "\u030c": "v",
    "\u0327": "c",
    "\u0328": "k",
}
_SYMBOLS = {
    "ı": r"\i{}",
    "ß": r"\ss{}",
    "æ": r"\ae{}",
    "Æ": r"\AE{}",
    "ø": r"\o{}",
    "Ø": r"\O{}",
    "ł": r"\l{}",
    "Ł": r"\L{}",
    "œ": r"\oe{}",
    "Œ": r"\OE{}",
    "đ": r"\dj{}",
    "Đ": r"\DJ{}",
    "–": "--",
    "—": "---",
    "‘": "`",
    "’": "'",
    "“": "``",
    "”": "''",
    "…": r"\ldots{}",
    "×": r"$\times$",
    "•": r"\textbullet{}",
    "€": r"\texteuro{}",
    "£": r"\pounds{}",
    "°": r"\textdegree{}",
    "±": r"$\pm$",
    "→": r"$\rightarrow$",
    "\u00a0": "~",
}

# Only three constructs, on purpose: bold is how the tailoring skill marks the
# keywords a posting asks for, italics name a stack, and links carry the
# publications. Anything richer belongs in a template, not in a bullet. A link
# that is not http(s) or mailto stays literal text.
_TOKEN = re.compile(
    r"\[(?P<label>[^\]\n]+)\]\((?P<url>(?:https?://|mailto:)[^)\s{}\\]+)\)"
    r"|\*\*(?P<bold>[^*\n]+?)\*\*"
    r"|(?<!\*)\*(?P<italic>[^*\n]+?)\*(?!\*)"
)


def escape(text: str) -> Latex:
    escaped = _SPECIAL.sub(lambda match: _REPLACEMENTS[match.group()], str(text))
    return Latex(_NON_ASCII.sub(lambda match: _unicode(match.group()), escaped))


def escape_url(url: str) -> Latex:
    """A URL as `\\href` takes it. hyperref reads `%` and `#` itself."""
    return Latex(str(url).replace("%", r"\%").replace("#", r"\#"))


def rich(text: str) -> Latex:
    text = str(text)
    out: list[str] = []
    at = 0
    for match in _TOKEN.finditer(text):
        out.append(escape(text[at : match.start()]))
        if match.group("label") is not None:
            out.append(rf"\href{{{escape_url(match.group('url'))}}}{{{rich(match.group('label'))}}}")
        elif match.group("bold") is not None:
            out.append(rf"\textbf{{{rich(match.group('bold'))}}}")
        else:
            out.append(rf"\textit{{{escape(match.group('italic'))}}}")
        at = match.end()
    out.append(escape(text[at:]))
    return Latex("".join(out))


def comment(text: str) -> Latex:
    """Every line behind a `%`, so a multi-line value stays one comment."""
    return Latex("\n".join(f"% {line}" if line else "%" for line in str(text).splitlines()))


def _unicode(char: str) -> str:
    if char in _SYMBOLS:
        return _SYMBOLS[char]
    base, *marks = unicodedata.normalize("NFD", char)
    if marks and all(mark in _ACCENTS for mark in marks):
        out = base
        for mark in marks:
            out = f"\\{_ACCENTS[mark]}{{{out}}}"
        return out
    return char

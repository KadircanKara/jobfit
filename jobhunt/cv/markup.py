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
# A non-ASCII character with any combining marks after it, or an ASCII letter
# carrying marks that had no precomposed form to normalise into.
_NON_ASCII = re.compile(r"[^\x00-\x7f][̀-ͯ]*|[A-Za-z][̀-ͯ]+")

# TeX ends a line at a bare CR, so every line break becomes "\n" before anything
# else looks at the text: a hidden bullet commented line by line must not have a
# second line TeX sees and the commenting did not.
_BREAKS = re.compile(r"\r\n?|[  \x85]")
# Control characters and invisible format characters have no business in a CV
# and some are live in TeX (^^L is \outer). Tab is kept and read as a space.
_INVISIBLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f​-‍⁠﻿]")
# Pasted from a PDF, "efficient" often arrives as "e" + U+FB03 + "cient".
_LIGATURES = re.compile(r"[ﬀ-ﬆ]")

# Templates on 8-bit fonts print only ASCII faithfully. Classic is one: lualatex
# with T1 fontenc and no fontspec turns a raw "×" into "Œ", a "·" into "ů", and
# drops "≥" entirely, all while the build reports success. The master always
# dodged this by hand ("Ozyegin", "$\times$"); here every non-ASCII character
# leaves as the LaTeX command that names it, which every engine and encoding
# reads the same way. Latin-1 is covered completely, because an unmapped
# character in that range does not fail - it prints as the wrong letter.
_ACCENTS = {
    "̀": "`",
    "́": "'",
    "̂": "^",
    "̃": "~",
    "̄": "=",
    "̆": "u",
    "̇": ".",
    "̈": '"',
    "̊": "r",
    "̋": "H",
    "̌": "v",
    "̧": "c",
    "̨": "k",
}
_SYMBOLS = {
    # Latin-1 supplement: every symbol, and the letters with no decomposition.
    " ": "~",
    "¡": r"\textexclamdown{}",
    "¢": r"\textcent{}",
    "£": r"\pounds{}",
    "¤": r"\textcurrency{}",
    "¥": r"\textyen{}",
    "¦": r"\textbrokenbar{}",
    "§": r"\S{}",
    "¨": r"\textasciidieresis{}",
    "©": r"\textcopyright{}",
    "ª": r"\textordfeminine{}",
    "«": r"\guillemotleft{}",
    "¬": r"\textlnot{}",
    "­": r"\-",
    "®": r"\textregistered{}",
    "¯": r"\textasciimacron{}",
    "°": r"\textdegree{}",
    "±": r"$\pm$",
    "²": r"\textsuperscript{2}",
    "³": r"\textsuperscript{3}",
    "´": r"\textasciiacute{}",
    "µ": r"\textmu{}",
    "¶": r"\P{}",
    "·": r"\textperiodcentered{}",
    "¸": r"\c{}",
    "¹": r"\textsuperscript{1}",
    "º": r"\textordmasculine{}",
    "»": r"\guillemotright{}",
    "¼": r"\textonequarter{}",
    "½": r"\textonehalf{}",
    "¾": r"\textthreequarters{}",
    "¿": r"\textquestiondown{}",
    "Æ": r"\AE{}",
    "Ð": r"\DH{}",
    "×": r"$\times$",
    "Ø": r"\O{}",
    "Þ": r"\TH{}",
    "ß": r"\ss{}",
    "æ": r"\ae{}",
    "ð": r"\dh{}",
    "÷": r"$\div$",
    "ø": r"\o{}",
    "þ": r"\th{}",
    # Letters beyond Latin-1 with no decomposition.
    "ı": r"\i{}",
    "ł": r"\l{}",
    "Ł": r"\L{}",
    "œ": r"\oe{}",
    "Œ": r"\OE{}",
    "đ": r"\dj{}",
    "Đ": r"\DJ{}",
    # Punctuation and spacing.
    " ": r"\enspace{}",
    " ": r"\quad{}",
    " ": " ",
    " ": " ",
    " ": r"\,",
    " ": r"\,",
    " ": r"\,",
    "‐": "-",
    "‑": "-",
    "‒": "--",
    "–": "--",
    "—": "---",
    "―": "---",
    "‘": "`",
    "’": "'",
    "‚": r"\quotesinglbase{}",
    "“": "``",
    "”": "''",
    "„": r"\quotedblbase{}",
    "‹": r"\guilsinglleft{}",
    "›": r"\guilsinglright{}",
    "′": r"$'$",
    "″": r"$''$",
    "†": r"\dag{}",
    "‡": r"\ddag{}",
    "•": r"\textbullet{}",
    "…": r"\ldots{}",
    "‰": r"\textperthousand{}",
    "€": r"\texteuro{}",
    "™": r"\texttrademark{}",
    # Arrows and relations, as they turn up in bullets ("latency ≤ 50 ms").
    "←": r"$\leftarrow$",
    "→": r"$\rightarrow$",
    "↑": r"$\uparrow$",
    "↓": r"$\downarrow$",
    "↔": r"$\leftrightarrow$",
    "⇒": r"$\Rightarrow$",
    "−": r"$-$",
    "≈": r"$\approx$",
    "≠": r"$\neq$",
    "≤": r"$\leq$",
    "≥": r"$\geq$",
    "∞": r"$\infty$",
    # The Greek letters technical CVs actually use (β-VAE, λ-calculus, μs).
    "α": r"$\alpha$",
    "β": r"$\beta$",
    "γ": r"$\gamma$",
    "δ": r"$\delta$",
    "ε": r"$\varepsilon$",
    "θ": r"$\theta$",
    "λ": r"$\lambda$",
    "μ": r"$\mu$",
    "π": r"$\pi$",
    "σ": r"$\sigma$",
    "τ": r"$\tau$",
    "φ": r"$\phi$",
    "ω": r"$\omega$",
    "Δ": r"$\Delta$",
    "Σ": r"$\Sigma$",
    "Ω": r"$\Omega$",
}

# The characters a URL may carry into \href. `^` is the one that matters: TeX
# reads "^^5c" as a backslash while it tokenises a macro argument, so a URL with
# carets inside \resumeItem{...} could smuggle a command past every other check.
# Anything outside this set is percent-encoded before it reaches LaTeX.
URL_CHARS = r"A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%"
_URL_OTHER = re.compile(rf"[^{URL_CHARS}]")
_URL_BODY = rf"(?:[{URL_CHARS.replace('()', '')}]|\([{URL_CHARS.replace('()', '')}]*\))+"

# Only three constructs, on purpose: bold is how the tailoring skill marks the
# keywords a posting asks for, italics name a stack, and links carry the
# publications. Anything richer belongs in a template, not in a bullet. A link
# that is not http(s) or mailto stays literal text. A URL may hold one level of
# balanced parentheses, which DOIs and Wikipedia titles need.
_TOKEN = re.compile(
    rf"\[(?P<label>[^\]\n]+)\]\((?P<url>(?:https?://|mailto:){_URL_BODY})\)"
    r"|\*\*(?P<bold>[^*\n]+?)\*\*"
    r"|(?<!\*)\*(?P<italic>[^*\n]+?)\*(?!\*)"
)


def clean(text: str) -> str:
    """One kind of line break, no invisible characters, composed accents."""
    text = _BREAKS.sub("\n", str(text))
    text = _INVISIBLE.sub("", text).replace("\t", " ")
    text = _LIGATURES.sub(lambda match: unicodedata.normalize("NFKC", match.group()), text)
    return unicodedata.normalize("NFC", text)


def escape(text: str) -> Latex:
    escaped = _SPECIAL.sub(lambda match: _REPLACEMENTS[match.group()], clean(text))
    return Latex(_NON_ASCII.sub(lambda match: _unicode(match.group()), escaped))


def escape_url(url: str) -> Latex:
    """A URL as `\\href` takes it: foreign characters percent-encoded, then
    `%` and `#` escaped, because hyperref reads both itself."""
    encoded = _URL_OTHER.sub(
        lambda match: "".join(f"%{byte:02X}" for byte in match.group().encode("utf-8")), str(url)
    )
    return Latex(encoded.replace("%", r"\%").replace("#", r"\#"))


def rich(text: str) -> Latex:
    text = clean(text)
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
    return Latex("\n".join(f"% {line}" if line else "%" for line in clean(text).splitlines()))


def _unicode(sequence: str) -> str:
    if sequence in _SYMBOLS:
        return _SYMBOLS[sequence]
    base, *marks = unicodedata.normalize("NFD", sequence)
    if marks and all(mark in _ACCENTS for mark in marks):
        out = _SYMBOLS.get(base, base)
        for mark in marks:
            out = f"\\{_ACCENTS[mark]}{{{out}}}"
        return out
    # Left as it is. A template on a Unicode font prints it; on an 8-bit font
    # TeX logs it as a missing character, and the build reports it by name.
    return sequence

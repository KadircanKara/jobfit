"""Text the person typed, printed in LaTeX.

Escaping is the default and markup the exception. A profile is prose, and a "%"
in "10% faster" that reached LaTeX raw would comment out the rest of the bullet.
"""
from __future__ import annotations

import shutil

import pytest

from jobhunt.cv import latex, markup, templates


def test_every_latex_special_is_escaped():
    assert markup.escape(r"R&D 10% #1 $5 a_b {x} ~ ^ \ ") == (
        r"R\&D 10\% \#1 \$5 a\_b \{x\} \textasciitilde{} \textasciicircum{} \textbackslash{} "
    )


def test_turkish_letters_become_commands_the_t1_preamble_can_print():
    assert markup.escape("Özyeğin İstanbul ışık çay") == (
        r"\"{O}zye\u{g}in \.{I}stanbul \i{}\c{s}\i{}k \c{c}ay"
    )


def test_typographic_symbols_become_commands():
    assert markup.escape("6 × 36 – “q” … 5€") == r"6 $\times$ 36 -- ``q'' \ldots{} 5\texteuro{}"


def test_a_character_with_no_mapping_passes_through():
    assert markup.escape("日本") == "日本"


def test_escaped_text_is_marked_safe():
    assert isinstance(markup.escape("x"), markup.Latex)
    assert isinstance(markup.rich("x"), markup.Latex)


def test_bold_italic_and_links_convert():
    assert markup.rich("Built **RAG** tools") == r"Built \textbf{RAG} tools"
    assert markup.rich("*Stack*: ROS") == r"\textit{Stack}: ROS"
    assert markup.rich("[Paper](https://x.org/a#b)") == r"\href{https://x.org/a\#b}{Paper}"


def test_markup_nests_where_it_makes_sense():
    assert markup.rich("[**Bold link**](https://x.org)") == r"\href{https://x.org}{\textbf{Bold link}}"


def test_text_inside_markup_is_still_escaped():
    assert markup.rich("R&D **10%** gain") == r"R\&D \textbf{10\%} gain"


def test_an_unclosed_marker_stays_literal():
    assert markup.rich("a ** b") == "a ** b"


def test_only_web_and_mail_links_become_links():
    assert markup.rich("[x](javascript:alert)") == "[x](javascript:alert)"


def test_a_url_keeps_its_percent_and_hash_readable_by_hyperref():
    assert markup.escape_url("https://x.org/a%20b#c") == r"https://x.org/a\%20b\#c"


def test_comment_puts_every_line_behind_a_percent():
    assert markup.comment("a\n\nb") == "% a\n%\n% b"
    assert markup.comment("") == ""


def test_a_caret_in_a_link_can_never_reach_tex_as_hex_notation():
    # TeX reads "^^5c" as a backslash while it tokenises a macro argument.
    assert "^" not in markup.escape_url("https://a.b/^^5cinput")
    assert "^^" not in markup.rich("[x](https://a.b/^^5cinput)")


def test_foreign_url_characters_are_percent_encoded():
    assert markup.escape_url("https://a.b/c d/é") == r"https://a.b/c\%20d/\%C3\%A9"


def test_a_link_keeps_balanced_parentheses():
    out = markup.rich("[Paper](https://doi.org/10.1016/S0140-6736(20)30183-5) in Lancet")

    assert out == r"\href{https://doi.org/10.1016/S0140-6736(20)30183-5}{Paper} in Lancet"


def test_latin1_symbols_never_pass_through_raw():
    # Raw, these print as the wrong T1 glyph ("·" as "ů") with no error at all.
    for code in range(0xA0, 0x100):
        assert markup.escape(chr(code)).isascii(), f"U+{code:04X} left unmapped"


def test_common_symbols_become_commands():
    assert markup.escape("Python · SQL © ≥ ™") == (
        r"Python \textperiodcentered{} SQL \textcopyright{} $\geq$ \texttrademark{}"
    )


def test_pasted_ligatures_and_decomposed_accents_keep_their_letters():
    assert markup.escape("e\ufb03cient") == "efficient"
    assert markup.escape("Ozyeg\u0306in") == r"Ozye\u{g}in"


def test_every_line_break_becomes_a_newline_and_controls_are_dropped():
    assert markup.escape("a\r\nb\rc\u2028d") == "a\nb\nc\nd"
    assert markup.escape("a\x00b\x0cc\u200bd") == "abcd"
    assert markup.comment("a\r\nb") == "% a\n% b"


@pytest.mark.skipif(shutil.which("lualatex") is None, reason="lualatex is not installed")
@pytest.mark.parametrize("template_id", ["classic", "modern"])
def test_every_mapping_prints_under_every_builtin_preamble(cfg, template_id):
    """A command that does not exist in the font's encoding would fail the build,
    and one that exists but has no glyph would be logged as missing: both are
    caught here, for the 8-bit Classic and the Unicode Modern alike."""
    preamble = latex.split_preamble(templates.get(cfg, template_id).text())[0]
    sample = " ".join(markup.escape(char) for char in markup._SYMBOLS)
    sample += " " + markup.escape("ÀÉÎÕÜÇŞĞİıçşğöüñ Ǎǎ Ő ű Ą ę")

    built = latex.build(preamble + "\\begin{document}\n" + sample + "\n\\end{document}\n")

    assert built.ok, built.log
    assert built.missing == ()

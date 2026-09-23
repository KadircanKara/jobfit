"""Text the person typed, printed in LaTeX.

Escaping is the default and markup the exception. A profile is prose, and a "%"
in "10% faster" that reached LaTeX raw would comment out the rest of the bullet.
"""
from __future__ import annotations

from jobhunt.cv import markup


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

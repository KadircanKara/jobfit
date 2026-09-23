"""Filling a template with a profile.

Two rules are enforced by the renderer rather than trusted to each template:
values are escaped unless already LaTeX, and hidden items become comments.
"""
from __future__ import annotations

import copy

import pytest
from conftest import load_fixture

from jobhunt.cv import model, render


def profile(**top) -> model.Profile:
    data = copy.deepcopy(load_fixture("cv/profile.json"))
    data.update(top)
    return model.parse(data)


BULLETS = (
    "\\BLOCK{for e in experience}\n"
    "\\BLOCK{call hidable(e)}\n"
    "  \\entry{\\VAR{e.title}}\n"
    "\\BLOCK{for b in e.bullets}\n"
    "\\BLOCK{call hidable(b)}\n"
    "    \\item{\\VAR{b.text|rich}}\n"
    "\\BLOCK{endcall}\n"
    "\\BLOCK{endfor}\n"
    "\\BLOCK{endcall}\n"
    "\\BLOCK{endfor}\n"
)


def test_a_bare_value_is_escaped():
    out = render.render(profile(basics={"name": "R&D 100% Co"}), "\\VAR{basics.name}")

    assert out == r"R\&D 100\% Co"


def test_rich_turns_markup_into_latex():
    out = render.render(profile(), "\\VAR{summary.text|rich}")

    assert out == r"Engineer combining \textbf{optimization research} with R\&D delivery."


def test_a_hidden_bullet_is_a_comment_and_a_visible_one_is_not():
    out = render.render(profile(), BULLETS)

    assert "    \\item{Built a \\textbf{multi-agent} planner, 10\\% faster.}" in out
    assert "    % \\item{Retired bullet kept for reference.}" in out


def test_notes_print_above_their_item_at_its_indentation():
    out = render.render(profile(), BULLETS)

    assert "  % NOTE: lead with the engine work.\n  \\entry{Analytical Engines Ltd}" in out


def test_a_hidden_entry_comments_out_its_bullets_too():
    out = render.render(profile(), BULLETS)

    assert "  % \\entry{Old Mill}" in out
    assert "    % \\item{Counted looms.}" in out


def test_a_group_whose_items_are_all_hidden_is_commented_whole():
    source = (
        "\\BLOCK{for e in projects}\\BLOCK{call hidable_group(e.bullets[1:])}\n"
        "\\begin{list}\n\\end{list}\n\\BLOCK{endcall}\\BLOCK{endfor}"
    )

    out = render.render(profile(), source)

    assert "% \\begin{list}\n% \\end{list}" in out


def test_a_misspelt_field_fails_loudly():
    with pytest.raises(render.RenderError) as caught:
        render.render(profile(), "\\VAR{basics.nmae}")
    assert "nmae" in str(caught.value)


def test_the_sandbox_blocks_reaching_into_python():
    with pytest.raises(render.RenderError):
        render.render(profile(), "\\VAR{basics.__class__.__mro__}")


def test_a_syntax_error_names_its_line():
    with pytest.raises(render.RenderError) as caught:
        render.render(profile(), "ok\n\\BLOCK{for x in}\n")
    assert "line 2" in str(caught.value)


def test_latex_braces_and_percent_lines_pass_through_untouched():
    source = "%%%%%% RESUME STARTS HERE %%%%%\n%% Only if sans serif\n\\newcommand{\\x}[1]{#1}\n"

    assert render.render(profile(), source) == source


def test_sections_follow_the_layout_and_resolve_custom_ones():
    found = render.sections(profile())

    assert [s.key for s in found] == [
        "summary", "experience", "education", "skills", "languages", "projects", "custom:custom-1",
    ]
    assert found[-1].kind == "entries" and found[-1].items[0].title == "Best Paper"


def test_a_section_with_nothing_in_it_is_left_out():
    found = render.sections(profile(summary={"text": ""}, projects=[]))

    assert "summary" not in [s.key for s in found]
    assert "projects" not in [s.key for s in found]


def test_a_section_left_out_of_the_layout_is_not_printed():
    layout = [{"key": "experience", "title": "WORK"}]

    found = render.sections(profile(layout=layout))

    assert [(s.key, s.title) for s in found] == [("experience", "WORK")]


def test_contacts_come_in_header_order():
    found = render.contacts(profile().basics)

    labels = [c.label for c in found]
    assert labels == ["+441234567890", "ada@example.org", "LinkedIn", "GitHub", "London, UK"]
    assert found[1].url == "mailto:ada@example.org"

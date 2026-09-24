# Writing a CV template

A template is one LaTeX file. jobhunt fills it with your profile and builds it
with lualatex (or the engine named on a `% !TEX program = xelatex` line).

## Placeholders

| Write | For |
|---|---|
| `\VAR{basics.name}` | a value. It is escaped for LaTeX automatically. |
| `\VAR{bullet.text\|rich}` | text that may carry **bold**, *italic* and [links](https://…) |
| `\VAR{url\|url}` | a URL inside `\href{…}` |
| `\BLOCK{for e in s.items} … \BLOCK{endfor}` | loops and conditions (`if`, `elif`, `else`, `endif`) |
| `\#{ a note to yourself }` | a comment that never reaches the output |

## What a template can use

- `basics`: `name`, `headline`, `email`, `phone`, `location`, `links` (each `label`, `url`), `headline_variants` (each `label`, `text`)
- `contacts`: the header line in order, each with `label` and `url` (empty for text)
- `extras`: labelled lines such as MILITARY STATUS, each with `label` and `value`
- `summary`: `text`, and `variants` (each `label`, `text`)
- `sections`: the sections in the order the person chose, each with `key`, `title`, `kind` and `items`
  - `kind == 'summary'`: print `summary.text`
  - `kind == 'skills'`: items have `category` and `items` (a list: `\VAR{g.items|join(', ')}`)
  - `kind == 'languages'`: items have `name`, `level` and `detail`
  - `kind == 'entries'`: experience, education, projects and custom sections. Items have
    `title`, `subtitle`, `location`, `dates`, `url`, `gpa` and `bullets` (each `text`).
    `s.key` tells them apart: `'experience'`, `'education'`, `'projects'`, `'custom:…'`.
    Experience and education print all of these except `url`. Projects and custom
    sections must print `title`, `subtitle`, `dates` and bullets; `location` and
    `url` are up to the template.

## Rules every template must follow

1. **Hidden items stay hidden.** Wrap every entry, every bullet and every skills line
   in `\BLOCK{call hidable(item)} … \BLOCK{endcall}`. Wrap every list whose items can
   all be hidden in `\BLOCK{call hidable_group(items)} … \BLOCK{endcall}`, so an empty
   list does not break the build. Hidden items come out as `%` comments, and notes print
   above their item as comments.
2. **Everything visible is printed.** Every visible field of every section appears somewhere.
3. **One file.** No `\input`, `\include` or `\includegraphics` of your own files.
4. **Loops over `sections`,** so the person's section order and titles are kept.

Uploaded templates are checked against these rules, compiled in a sandbox with no
network and no access to your files, and run through an ATS check before you accept them.

"""What a LaTeX document says, as words.

The comparison both the one-time import and template validation make: strip
the commands and the punctuation LaTeX needs, keep the words a reader sees,
and keep comments apart, because a comment is never printed.
"""
from __future__ import annotations

import collections
import re

_COMMENT = re.compile(r"(?<!\\)%.*$")
_COMMAND = re.compile(r"\\[a-zA-Z]+\*?")
_NOISE = re.compile(r"[{}$~^&%\\\[\]()|:,;]")
_LENGTH = re.compile(r"^-?[\d.]+(pt|em|ex|in|cm|mm)$")


def split_comments(body: str) -> tuple[str, str]:
    visible, comments = [], []
    for line in body.splitlines():
        match = _COMMENT.search(line)
        if match:
            comments.append(match.group()[1:])
            line = line[: match.start()]
        visible.append(line)
    return "\n".join(visible), "\n".join(comments)


def words(text: str) -> collections.Counter[str]:
    text = _NOISE.sub(" ", _COMMAND.sub(" ", text))
    tokens = (token.strip(".'`\"-").lower() for token in text.split())
    return collections.Counter(token for token in tokens if token and not _LENGTH.match(token))

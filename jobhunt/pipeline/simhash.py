"""64-bit simhash over word shingles, for near-duplicate job descriptions.

Two postings of the same role on two boards differ in boilerplate, not in substance.
Exact hashing misses that; simhash catches it with a Hamming distance threshold.
"""
from __future__ import annotations

import hashlib
import re

BITS = 64
_TOKEN = re.compile(r"[a-z0-9À-ɏ]+")
DEFAULT_SHINGLE = 2
# Measured on tests/test_simhash.py with 2-word shingles: a reworded copy of the
# same posting lands at distance 9, an unrelated posting at 32. 12 sits in that
# gap with margin on both sides. Unigram shingles separate more sharply but throw
# away word order, which is the only thing distinguishing two postings that share
# a vocabulary.
DEFAULT_MAX_DISTANCE = 12


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def shingles(tokens: list[str], size: int = DEFAULT_SHINGLE) -> list[str]:
    if len(tokens) < size:
        return [" ".join(tokens)] if tokens else []
    return [" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)]


def simhash(text: str, shingle_size: int = DEFAULT_SHINGLE) -> int:
    features = shingles(tokenize(text), shingle_size)
    if not features:
        return 0
    vector = [0] * BITS
    for feature in features:
        digest = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
        for bit in range(BITS):
            vector[bit] += 1 if digest >> bit & 1 else -1
    value = 0
    for bit in range(BITS):
        if vector[bit] > 0:
            value |= 1 << bit
    return value


def to_hex(value: int) -> str:
    return f"{value:016x}"


def from_hex(value: str) -> int:
    return int(value, 16)


def hamming(left: int, right: int) -> int:
    return ((left ^ right) & ((1 << BITS) - 1)).bit_count()


def is_near_duplicate(left: str, right: str, max_distance: int = DEFAULT_MAX_DISTANCE) -> bool:
    """Compare two hex simhashes."""
    return hamming(from_hex(left), from_hex(right)) <= max_distance

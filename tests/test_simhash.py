from __future__ import annotations

from jobhunt.pipeline import simhash

BASE = (
    "We are looking for a backend engineer to build and operate our payments API. "
    "You will design services in Python and FastAPI, own Postgres schemas, and work "
    "with Redis queues. You should have five years of experience shipping production "
    "systems and be comfortable with async programming and distributed tracing."
)
REWORDED = (
    "We're hiring a backend engineer to build and run our payments API. "
    "You will design services in Python and FastAPI, own Postgres schemas, and work "
    "with Redis queues. You should have 5+ years of experience shipping production "
    "systems and be comfortable with async programming and distributed tracing."
)
DIFFERENT = (
    "We are looking for a product designer to own our design system. You will run "
    "user research, build prototypes in Figma, and partner with engineering on "
    "accessibility. You should have a portfolio of shipped consumer products and "
    "strong opinions about typography and interaction design."
)


def test_identical_text_hashes_identically() -> None:
    assert simhash.simhash(BASE) == simhash.simhash(BASE)


def test_empty_text_is_zero() -> None:
    assert simhash.simhash("") == 0


def test_reworded_posting_is_a_near_duplicate() -> None:
    distance = simhash.hamming(simhash.simhash(BASE), simhash.simhash(REWORDED))
    assert distance <= simhash.DEFAULT_MAX_DISTANCE, distance


def test_different_posting_is_not_a_near_duplicate() -> None:
    distance = simhash.hamming(simhash.simhash(BASE), simhash.simhash(DIFFERENT))
    assert distance > simhash.DEFAULT_MAX_DISTANCE, distance


def test_hex_round_trip() -> None:
    value = simhash.simhash(BASE)
    assert simhash.from_hex(simhash.to_hex(value)) == value
    assert len(simhash.to_hex(value)) == 16


def test_is_near_duplicate_on_hex() -> None:
    left = simhash.to_hex(simhash.simhash(BASE))
    right = simhash.to_hex(simhash.simhash(REWORDED))
    assert simhash.is_near_duplicate(left, right)

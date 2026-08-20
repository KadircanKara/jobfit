"""The JD extraction ladder. PLAN.md section 9.5.

The real output of this system is clean, complete JD text, because that is what
the tailoring skill takes as input. Sources differ enormously in how much of it
they hand over, and this package closes the gap.

Extraction is lazy on purpose: never at sync time, sometimes at rank time, and
always before apply. That cuts detail fetches by one to two orders of magnitude
and keeps request volume on fragile sources at a human level.
"""
from __future__ import annotations

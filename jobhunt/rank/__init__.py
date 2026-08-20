"""Two-stage ranking. PLAN.md section 7.

Stage 1 is deterministic, free, and runs on everything. Stage 2 is the LLM gate
and runs only on what stage 1 lets through. The ordering is the whole cost model.
"""
from __future__ import annotations

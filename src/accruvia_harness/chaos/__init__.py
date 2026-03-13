"""Chaos checks for recovery truth and control-plane invariants.

The default chaos scope is intentionally narrow: verify crash recovery,
bounded retries, lease correctness, timeout handling, and supervisor truth.
Broader synthetic scenario injectors remain available, but are opt-in.
"""

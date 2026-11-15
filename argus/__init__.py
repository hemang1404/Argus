"""Argus: a bounded, test-gated code recovery runtime.

Argus asks a user-authorized model for a SEARCH/REPLACE fix, reconciles it with a tiered
patch engine (exact, whitespace-tolerant, token-equal; an opt-in Tier 4), checks the result
with a syntactic gate, runs the project's tests in a copied workspace, and only writes a
candidate for explicit approval. The version below is the single source of truth: the
packaging metadata reads it.
"""

__version__ = "0.2.0b2"
__all__ = ["__version__"]

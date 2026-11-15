"""
minesql - a MySQL terminal that only suggests the NEXT SQL piece.

Schema-aware ghost-text SQL suggestions for your MySQL CLI sessions,
with a tiny learned n-gram model that adapts to how you write queries.
"""

from .cli import main

__version__ = "0.1.0"
__all__ = ["main"]

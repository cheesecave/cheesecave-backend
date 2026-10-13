"""Fallback system for external repository sources (HuggingFace, other KohakuHub instances).

This module provides:
- Fallback to external sources when repositories/files not found locally
- Caching of repo→source mappings
- URL mapping for HuggingFace asymmetric API structure
- User/organization profile fallback
"""

from kohakuhub.api.fallback.decorators import (
    with_repo_fallback,
    with_user_fallback,
)

__all__ = [
    "with_repo_fallback",
    "with_user_fallback",
]

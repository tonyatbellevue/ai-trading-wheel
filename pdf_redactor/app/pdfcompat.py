"""Single import point for PyMuPDF.

PyMuPDF 1.24+ renamed the top-level module from ``fitz`` to ``pymupdf`` and
emits a deprecation warning for the old name. Importing through here keeps the
rest of the app quiet and working on both.
"""

from __future__ import annotations

try:  # PyMuPDF >= 1.24
    import pymupdf as fitz  # type: ignore
except ImportError:  # pragma: no cover - older PyMuPDF
    import fitz  # type: ignore

__all__ = ["fitz"]

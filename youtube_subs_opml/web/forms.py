"""Typed accessors for submitted form and query data.

Starlette types every form value as ``UploadFile | str``. The routes here only
ever expect text fields, so a stray file upload is treated as absent rather
than stringified into its repr.
"""

from __future__ import annotations

from collections.abc import Mapping

from starlette.datastructures import ImmutableMultiDict, UploadFile

FormValues = Mapping[str, UploadFile | str]


def form_text(data: FormValues, key: str) -> str | None:
    """The text value submitted for ``key``, or None if absent or not text."""
    value = data.get(key)
    return value if isinstance(value, str) else None


def form_texts(data: ImmutableMultiDict[str, UploadFile | str], key: str) -> list[str]:
    """Every text value submitted for a repeated field such as a checkbox list."""
    return [value for value in data.getlist(key) if isinstance(value, str)]

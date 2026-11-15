"""The base class for every shape we ask a model to fill in.

Open-weight models are looser with schemas than the native structured-output
helper. The commonest slip by far is sending `null` for a field that is not
nullable but does have a default — `"approved_source_notes": null` instead of
`""`. That is not a disagreement about meaning, it is a spelling difference, so
it is cheaper to accept it here than to spend a second round trip correcting it.

Anything that changes meaning is still a validation error. A null for a field
with no default, or a wrong type, fails as it should.
"""
from __future__ import annotations

import types
import typing
from typing import Any

from pydantic import BaseModel, model_validator


def allows_none(annotation: Any) -> bool:
    """Is None a legal value for this annotation?"""
    if annotation is None or annotation is type(None):
        return True
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        return any(arg is type(None) for arg in typing.get_args(annotation))
    return False


class LLMModel(BaseModel):
    """A schema a model fills in, tolerant of null-for-default only."""

    @model_validator(mode="before")
    @classmethod
    def _null_means_default(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        cleaned: dict[str, Any] | None = None
        for name, field in cls.model_fields.items():
            if data.get(name, "present") is not None:
                continue
            # A null arrived. Keep it where None is legal, and where the field
            # is required (so the error is raised, not hidden).
            if field.is_required() or allows_none(field.annotation):
                continue
            if cleaned is None:
                cleaned = dict(data)
            del cleaned[name]          # let the field's own default apply
        return cleaned if cleaned is not None else data

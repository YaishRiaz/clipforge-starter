"""The tolerant base class for model-filled shapes.

A real Groq reply (openai/gpt-oss-20b, 12 Nov 2026) sent
`"approved_source_notes": null` for a non-nullable string that has a default.
That is a spelling difference, not a disagreement about meaning, so it is
accepted here instead of costing a second round trip. Anything that changes
meaning must still fail.
"""
from __future__ import annotations

import pytest
from pydantic import Field, ValidationError

from clipforge.core.schema import LLMModel, allows_none
from clipforge.ingest.rules import BriefRules
from clipforge.ingest.tagger import SourceTags


class Sample(LLMModel):
    required_text: str
    text_with_default: str = ""
    nullable_text: str | None = None
    items: list[str] = Field(default_factory=list)
    flag: bool = False
    number: float | None = None


def test_null_for_a_defaulted_field_becomes_the_default():
    s = Sample.model_validate({"required_text": "x", "text_with_default": None})
    assert s.text_with_default == ""


def test_null_for_a_defaulted_list_becomes_the_default():
    assert Sample.model_validate({"required_text": "x", "items": None}).items == []


def test_null_for_a_defaulted_bool_becomes_the_default():
    assert Sample.model_validate({"required_text": "x", "flag": None}).flag is False


def test_null_is_kept_where_the_schema_allows_it():
    s = Sample.model_validate({"required_text": "x", "nullable_text": None})
    assert s.nullable_text is None


def test_null_for_a_required_field_still_fails():
    """Hiding this would invent data; it must stay an error."""
    with pytest.raises(ValidationError):
        Sample.model_validate({"required_text": None})


def test_a_missing_required_field_still_fails():
    with pytest.raises(ValidationError):
        Sample.model_validate({"text_with_default": "x"})


def test_a_wrong_type_still_fails():
    with pytest.raises(ValidationError):
        Sample.model_validate({"required_text": "x", "items": "not a list"})


def test_non_dict_input_is_passed_through():
    with pytest.raises(ValidationError):
        Sample.model_validate("just a string")


def test_the_real_failure_that_prompted_this():
    """The exact payload shape that cost a retry against Groq."""
    rules = BriefRules.model_validate({
        "approved_source_urls": ["https://youtu.be/dQw4w9WgXcQ"],
        "approved_source_notes": None,
        "clip_min_seconds": 20, "clip_max_seconds": 60,
        "allowed_platforms": None, "required_hashtags": None,
        "banned_content": None, "uncertain": None, "pre_approval": None,
        "disclosure_text": None,
    })
    assert rules.approved_source_notes == ""
    assert rules.allowed_platforms == [] and rules.uncertain == []
    assert rules.pre_approval is False
    assert rules.disclosure_text is None          # genuinely nullable
    assert rules.clip_max_seconds == 60


def test_source_tags_tolerates_the_same_slip():
    tags = SourceTags.model_validate({"summary": "A heist goes wrong.", "chapters": None})
    assert tags.chapters == []


def test_source_tags_still_needs_a_summary():
    with pytest.raises(ValidationError):
        SourceTags.model_validate({"chapters": []})


@pytest.mark.parametrize("annotation, expected", [
    (str, False),
    (str | None, True),
    (list[str], False),
    (float | None, True),
    (type(None), True),
])
def test_allows_none(annotation, expected):
    assert allows_none(annotation) is expected

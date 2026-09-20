"""Guard: the pydantic deprecation filter is module-scoped.

The vendored cos_agent library may use pydantic v1-style APIs; our own
modules may not. This test calls one such API on purpose: with
``filterwarnings = ["error"]`` the warning must raise, because the
ignore filter in pyproject.toml is scoped to the vendored module only.
"""

import pydantic
import pytest
from pydantic.warnings import PydanticDeprecatedSince20


class _Model(pydantic.BaseModel):
    """A model whose v1-style call we assert is still an error."""

    field: str


def test_our_own_v1_style_call_still_raises() -> None:
    # GIVEN a pydantic v2 model (ours are all v2)
    # WHEN code in this repo calls the deprecated v1-style method
    # THEN the warning raises as an error — the ignore filter is
    # scoped to the vendored library only. If this ever fails with
    # "DID NOT RAISE", someone widened the filter to silence our own
    # modules too.
    with pytest.raises(PydanticDeprecatedSince20):
        _Model(field="x").json()  # pyright: ignore[reportDeprecated]

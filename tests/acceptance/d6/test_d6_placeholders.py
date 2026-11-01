"""D6b placeholders: authoring rows stay skipped until the D6b tests PR."""

import pytest


@pytest.mark.skip(reason="D6b")
def test_d6_t11_authoring_loop():
    """D6-T11: placeholder (D6b)."""


@pytest.mark.skip(reason="D6b")
def test_d6_t12_authoring_hides_exam():
    """D6-T12: placeholder (D6b)."""


@pytest.mark.skip(reason="D6b")
def test_d6_t13_authored_output_valid():
    """D6-T13: placeholder (D6b)."""


@pytest.mark.skip(reason="D6b")
def test_d6_t15_live_authoring_eval():
    """D6-T15: placeholder (D6b)."""


@pytest.mark.skip(reason="D6b")
def test_d6_t21_held_out_scenarios():
    """D6-T21: placeholder (D6b)."""

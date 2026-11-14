"""Harness SDK author-side helpers: unknown file IDs raise ValueError.

Regression coverage for the self-inspection finding (issue #491):
``selected_chars``/``fits_budget`` used to leak a raw ``KeyError`` for
unknown file IDs while ``validate_output`` in the same module raises a
clear ``ValueError``.
"""

import pytest

from harnesses.sdk import fits_budget, make_file, make_payload, selected_chars, validate_output

FILES = [make_file("a", "a.txt", "12345"), make_file("b", "b.txt", "xy")]


def test_selected_chars_known_ids():
    assert selected_chars(["a", "b"], FILES) == 7
    assert selected_chars([], FILES) == 0


def test_selected_chars_unknown_id_raises_value_error():
    with pytest.raises(ValueError, match="unknown file IDs"):
        selected_chars(["missing"], FILES)


def test_fits_budget_unknown_id_raises_value_error():
    with pytest.raises(ValueError, match="unknown file IDs"):
        fits_budget(["missing"], FILES, 100)


def test_fits_budget_known_ids():
    assert fits_budget(["a"], FILES, 5) is True
    assert fits_budget(["a"], FILES, 4) is False


def test_validate_output_still_rejects_unknown_ids():
    payload = make_payload("q", FILES, 100)
    with pytest.raises(ValueError, match="unknown file IDs"):
        validate_output(["missing"], payload)

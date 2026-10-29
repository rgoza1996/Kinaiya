"""Bounded negative runtime-state predicate boundaries; synthetic strings only."""

from services.converse.state_guard import UNVERIFIED_STATE_REPLY, negative_runtime_state_claim

POSITIVE_CASES = [
    "No Agent is presently authorized to execute that request.",
    "No Agent is available.",
    "The Agent is unavailable.",
    "The Agent is not authorized to run that.",
    "The Agent isn't available.",
    "There is no Agent available to run this.",
    "There isn't any Agent.",
    "No action has been taken or recorded.",
    "No action was taken.",
    "Nothing has been executed.",
    "Nothing was done.",
    "No task has started.",
    "The task hasn't started.",
]

HARMLESS_CASES = [
    "I can't access files directly.",
    "If the Agent is unavailable we can plan.",
    "If no action has been taken, we can still plan.",
    "You said the Agent is unavailable.",
    "I can't verify whether an Agent is available.",
    "I don't know whether any task started.",
    "I don't see a task receipt in this conversation.",
    "no action is required.",
    "No action is required to continue.",
    "Do not authorize work without permission.",
    "We can plan the next steps together.",
    "Well done. I finished my introduction.",
    "Is the Agent available?",
    "The Agent is available to help.",
    "I can't verify that Agent or task status.",
    "",
    "Hello there.",
]

# Grammatical-family regressions for the review blockers (not exact examples).
REVIEW_BOUNDARIES = [
    # Ordinary narration stays usable; scoped perfect-tense denials still hit.
    ("I changed my mind.", False),
    ("The event occurred yesterday.", False),
    ("Nothing has changed.", True),
    ("Nothing has happened.", True),
    # Architecture, safety, and direct-tool limits stay usable.
    ("The Agent is not a person.", False),
    ("The Agent isn't a human.", False),
    ("The Agent won't help with harmful conduct.", False),
    ("The Agent can't access files directly.", False),
    # Bare named subject and negated complements are still denials.
    ("Agent is unavailable.", True),
    ("The Agent is not authorized to run that.", True),
    ("The Agent is not currently available.", True),
    # Mixed replies evaluate the denial clause on its own.
    ("I cannot verify task status—but no action has been taken.", True),
    ("I cannot verify task status but no action has been taken.", True),
    ('The example says "pause" but no task has started.', True),
    # Fragment and explicit denials keep working.
    ("No Agent currently authorized.", True),
    ("No Agent is presently authorized to act.", True),
    # Trusted fallback and questions are never negative claims.
    ("The Agent has not provided a verified result for that claim.", False),
    ("Is the Agent available?", False),
    # Independent review: comma governors, ability limits, later planning modals.
    ("Nothing was changed.", True),
    ("According to you, no task has started.", False),
    ("If the demo is paused, no task has started.", False),
    ("The Agent is unable to access files directly.", False),
    ("The Agent is not able to access files directly.", False),
    ("The Agent cannot run harmful commands.", False),
    ("The Agent is unavailable and we could plan next steps.", True),
    ("No task has started so we might plan ahead.", True),
    ("You said, no task has started; no Agent is available.", True),
    ("The Agent cannot run harmful commands while online.", False),
    ("The Agent is unavailable, according to you.", False),
    ("The Agent is unavailable, perhaps.", False),
    ("The Agent is unavailable and we could plan next steps, perhaps.", True),
]

LATE_DENIAL = "The plan is " + "very " * 120 + "detailed and no Agent is available."


def test_unverified_state_reply_exact():
    assert UNVERIFIED_STATE_REPLY == "I can't verify that Agent or task status."


def test_negative_claims_detected():
    for text in POSITIVE_CASES:
        assert negative_runtime_state_claim(text) is True, text


def test_harmless_controls_preserved():
    for text in HARMLESS_CASES:
        assert negative_runtime_state_claim(text) is False, text


def test_review_boundary_families():
    for text, expected in REVIEW_BOUNDARIES:
        assert negative_runtime_state_claim(text) is expected, text


def test_late_denial_within_total_bound():
    assert len(LATE_DENIAL) < 4000
    assert negative_runtime_state_claim(LATE_DENIAL) is True


def test_original_input_bound_applies_before_quote_masking():
    text = '"' + "x" * 4500 + '" No Agent is available.'
    assert negative_runtime_state_claim(text) is False


def test_pure_no_io_and_deterministic():
    assert negative_runtime_state_claim("The Agent is unavailable.") is True
    assert negative_runtime_state_claim("The Agent is unavailable.") is True
    assert negative_runtime_state_claim(None) is False
    assert negative_runtime_state_claim(123) is False

"""Historical English claim predicates, retained for diagnostic compatibility.

These finite heuristics are no longer connected to Converse publication.
The November 15 owner clarification (#486 / PR #625) requires faithful model
output instead of semantic substitutions or suppression. Native outcome notices,
authorization and custody have separate trusted contracts. These predicates
neither establish native state nor qualify model quality.
"""

import re

UNVERIFIED_STATE_REPLY = "I can't verify that Agent or task status."

_NOUN = r"(?:action|task|work|job|effect|execution|operation|command|change|request|step|process)s?"
_EFFECT = r"(?:taken|recorded|executed|started|performed|completed|finished|done|initiated|run|launched|created|saved|sent|deleted|verified|applied)"
_EFFECT2 = _EFFECT + r"|running|active|underway"
_STATE_NEG = r"(?:unavailable|unauthori[sz]ed|inactive|absent|missing|offline|unreachable|disabled)"
_STATE = (
    r"(?:unavailable|unauthori[sz]ed|inactive|absent|missing|offline|unreachable|disabled"
    r"|available|authori[sz]ed|present|active|ready|online|running)"
)
_ADVERB = r"(?:presently|currently|right\s+now|here)"
_AGENT = r"(?:(?:the|an?|this|that|any)\s+)?agents?"
_COPULA = r"(?:is|are|was|were|has\s+been|have\s+been|remains?|stays?)"
_CONTRACTION = (
    r"(?:isn['\u2019]t|are?n['\u2019]t|wasn['\u2019]t|weren['\u2019]t"
    r"|hasn['\u2019]t|haven['\u2019]t|hadn['\u2019]t"
    r"|cann?['\u2019]?t|cannot|can\s+not|couldn['\u2019]t|won['\u2019]t|wouldn['\u2019]t)"
)
_NEGATIVE_PERFECT = (
    r"(?:hasn['\u2019]t|haven['\u2019]t|hadn['\u2019]t"
    r"|has\s+not|have\s+not|had\s+not)"
)
_WINDOW = r"[^.?!\n]{0,80}?"
_TASK_WINDOW = r"[^.?!\n]{0,40}?"

_DETECTION = [
    # No Agent is (presently) authorized/available/... (subject negates predicate).
    re.compile(
        r"\bno\s+agents?\s+"
        + _COPULA
        + r"\b"
        + _WINDOW
        + r"\b(?:(?:not\s+(?:"
        + _ADVERB
        + r"\s+)?)?"
        + _STATE
        + r")\b",
        re.I,
    ),
    # The Agent is unavailable / Agent is missing (bare or determined copula).
    re.compile(
        r"\b" + _AGENT + r"\s+" + _COPULA + r"\b" + _WINDOW + r"\b" + _STATE_NEG + r"\b",
        re.I,
    ),
    # The Agent is not authorized / not currently available (negated complement).
    re.compile(
        r"\b"
        + _AGENT
        + r"\s+"
        + _COPULA
        + r"\b"
        + _WINDOW
        + r"\bnot\s+(?:"
        + _ADVERB
        + r"\s+)?(?:"
        + _STATE
        + r")\b",
        re.I,
    ),
    # The Agent isn't/can't/won't ... (contraction needs a state complement).
    re.compile(
        r"\b"
        + _AGENT
        + r"\s+"
        + _CONTRACTION
        + r"\b"
        + r"\s+(?:(?:"
        + _ADVERB
        + r")\s+)?(?:(?:be|been)\s+)?(?:(?:"
        + _ADVERB
        + r")\s+)?(?:"
        + _STATE
        + r")\b",
        re.I,
    ),
    # Perfect-tense effect denials; generic ability and safety modals stay usable.
    re.compile(
        r"\b"
        + _AGENT
        + r"\s+"
        + _NEGATIVE_PERFECT
        + r"\b"
        + _WINDOW
        + r"\b"
        + _NOUN
        + r"\b"
        + _TASK_WINDOW
        + r"\b(?:"
        + _EFFECT2
        + r")\b",
        re.I,
    ),
    re.compile(
        r"\b"
        + _AGENT
        + r"\s+"
        + _NEGATIVE_PERFECT
        + r"\b"
        + _WINDOW
        + r"\b(?:"
        + _EFFECT2
        + r")\b"
        + _TASK_WINDOW
        + r"\b"
        + _NOUN
        + r"\b",
        re.I,
    ),
    # There is/are no Agent ... (existential).
    re.compile(r"\bthere\s+(?:is|are|was|were)\s+no\s+agents?\b", re.I),
    re.compile(
        r"\bthere\s+(?:isn['\u2019]t|are?n['\u2019]t|wasn['\u2019]t|weren['\u2019]t)\s+(?:any\s+)?agents?\b",
        re.I,
    ),
    # No Agent (currently) authorized fragment without explicit copula.
    re.compile(
        r"\bno\s+agents?\s+(?:(?:" + _ADVERB + r")\s+)?(?:" + _STATE + r")\b",
        re.I,
    ),
    # No <effect-noun> has ... taken/recorded/executed/started/... (perfect).
    re.compile(
        r"\bno\s+" + _NOUN + r"\s+(?:has|have|had)\b[^.?!\n]{0,60}?\b" + _EFFECT + r"\b",
        re.I,
    ),
    # Nothing has ... executed/started/happened/occurred/changed (scoped perfect).
    re.compile(
        r"\bnothing\s+(?:has|have|had)\b[^.?!\n]{0,60}?\b(?:"
        + _EFFECT
        + r"|happened|occurred|changed)\b",
        re.I,
    ),
    # No <effect-noun> is/was ... taken/running/... (present/past passive).
    re.compile(
        r"\bno\s+"
        + _NOUN
        + r"\s+(?:is|are|was|were)\b[^.?!\n]{0,60}?\b(?:taken|recorded|executed|started|performed|completed|finished|done|running|active|underway|initiated)\b",
        re.I,
    ),
    # The task hasn't / has not ... started/taken/... (definite-subject contraction).
    re.compile(
        r"\b(?:the|an?|this|that|any|your)\s+"
        + _NOUN
        + r"\s+(?:hasn['\u2019]t|haven['\u2019]t|hadn['\u2019]t|has\s+not|have\s+not|had\s+not)\b[^.?!\n]{0,60}?\b"
        + _EFFECT
        + r"\b",
        re.I,
    ),
    # Nothing is/was ... executed/done/... (present/past).
    re.compile(
        r"\bnothing\s+(?:is|are|was|were)\b[^.?!\n]{0,60}?\b(?:taken|recorded|executed|started|performed|completed|finished|done|changed)\b",
        re.I,
    ),
]

_ATTRIBUTION = re.compile(
    r"\byou\s+(said|says?|mentioned?|told|tell|asked?|wrote|write|reported?|noted?|claimed?|think|believe)\b"
    r"|\byou['\u2019]ve\s+(said|mentioned|told|asked)\b"
    r"|\baccording\s+to\s+you\b",
    re.I,
)
_UNCERTAINTY = re.compile(
    r"\bwhether\b|\b(might|maybe|perhaps|possibly|probably|uncertain|unsure|unclear|not\s+sure)\b"
    r"|\b(can['\u2019]t|cannot|can\s+not|don['\u2019]t|do\s+not)\s+(verify|confirm|tell|know|be\s+sure)\b"
    r"|\bdo\s*n['\u2019]?t\s+know\b",
    re.I,
)
_CONDITIONAL = re.compile(
    r"\bif\b|\bunless\b|\bprovided\s+(that\b)?|\bassuming\b|\bsuppose[sd]?\b|\bsupposing\b|\bimagine\b|\bhypothetically\b|\bin\s+case\b",
    re.I,
)
_FIRST_PERSON = re.compile(
    r"\bi\s+(do\s*n['\u2019]?t|cannot|can['\u2019]t|haven['\u2019]t|have\s+not|can\s+only)\s+(see|find|locate|access|have\s+access|show|display|retrieve)\b",
    re.I,
)
_IMPERATIVE = re.compile(
    r"^\s*(do\s*n['\u2019]?t|don['\u2019]t|never|please\b.*\bdo\s*n['\u2019]?t|must\s*n['\u2019]?t|should\s*n['\u2019]?t)\b",
    re.I,
)
_HYPOTHETICAL_MODAL = re.compile(r"\b(would|could|might|may|should)\b", re.I)
_POSTFIX_UNCERTAINTY = re.compile(
    r"\s*,?\s*(?:perhaps|maybe|possibly|probably|i\s+think|i\s+believe)\s*[.!]*\s*",
    re.I,
)

_QUOTED_SPAN = re.compile(r'"[^"\n]*"|\u201c[^\u201c\u201d\n]*\u201d')
_INDEPENDENT_SPLIT = re.compile(
    r"\s*--\s*|\s*[\u2014\u2013]\s*|;\s*|:\s*|\bbut\b|\bhowever\b",
    re.I,
)


def negative_runtime_state_claim(text: str) -> bool:
    """Return True for a declarative negative Agent/task-state claim chunk."""
    if not isinstance(text, str):
        return False
    bounded = text[:4000]
    if not bounded.strip():
        return False
    # Mask genuinely double-quoted spans so an unrelated quoted word does not
    # suppress the surrounding assertion; single-quote contractions stay intact.
    bounded = _QUOTED_SPAN.sub(" quote ", bounded)
    # Split into sentences, keeping terminators to honour questions per sentence.
    parts = re.findall(r"[^.!?]+[.!?]*", bounded)
    if not parts:
        return False
    for part in parts:
        if "?" in part:
            continue
        body = part.strip()
        if not body:
            continue
        for span in _INDEPENDENT_SPLIT.split(body):
            # Do not discard a report/conditional governor at its comma.
            if _ATTRIBUTION.search(span) or _CONDITIONAL.search(span):
                continue
            for clause in re.finditer(r"[^,]+", span):
                c = clause.group()
                if not c.strip():
                    continue
                for pattern in _DETECTION:
                    for match in pattern.finditer(c):
                        governed = c[: match.end()]
                        if any(
                            exclusion.search(governed)
                            for exclusion in (
                                _UNCERTAINTY,
                                _FIRST_PERSON,
                                _IMPERATIVE,
                                _HYPOTHETICAL_MODAL,
                            )
                        ):
                            continue
                        if _POSTFIX_UNCERTAINTY.fullmatch(span[clause.start() + match.end() :]):
                            continue
                        return True
    return False

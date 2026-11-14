"""Typed Decisions projection for imported native PCM evidence.

This lane cannot produce Agent commands. The owner task already exists before
source admission; neither recorded speech nor classifier choices can admit one.
"""

from packages.contracts.models import Decision
from packages.providers.llama_media import (
    LlamaMediaRequestSpec,
    LlamaMediaSystemOneResult,
    build_request,
)
from packages.providers.text import ProviderRefusal
from services.decisions.media_classifier import build_media_questions, parse_media_answers


def prepare_request(model, part, state):
    return build_request(
        LlamaMediaRequestSpec(
            mode="decisions_systemone",
            model=model,
            parts=(part,),
            state=state.rendered_state,
            questions=build_media_questions(state),
            max_tokens=128,
        )
    )


def routing_payload(result, state, provenance, owner_event, key):
    if type(result) is not LlamaMediaSystemOneResult:
        raise ProviderRefusal("Native Decisions result is unavailable")
    projection = parse_media_answers(
        result.answers, state, build_media_questions(state), min_confidence=0.5
    )
    if (
        projection.allowed_effect != "evidence_only"
        or projection.command_operation is not None
        or projection.owner_intent is not None
        or projection.agent_route != "IGNORE_FOR_IMMEDIATE_ROUTING"
    ):
        raise ProviderRefusal("Imported media cannot authorize Agent work")
    decision = Decision(
        route="ignore",
        reason_code=projection.reason_code,
        confidence=min(projection.confidence.values()),
        source_event_ids=[owner_event.event_id],
        source_revision=owner_event.payload.get("revision", 1),
        instruction="",
        idempotency_key=key,
    )
    return {
        **decision.model_dump(mode="json"),
        "native_media_v2": provenance,
        "media_attention": projection.attention,
        "media_conversation": projection.conversation,
        "allowed_effect": "evidence_only",
    }

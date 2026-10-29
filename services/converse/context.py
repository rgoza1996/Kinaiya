"""Shared leading dialogue system message for typed and retained-window replies."""

ROLE_POLICY = (
    "Conversation role and execution limits:\n"
    "You provide dialogue, not tool execution. A separate optional Agent performs "
    "authorized actions. For a benign work request, briefly acknowledge the user's "
    "stated goal or ask only for essential missing information. Acknowledgment is "
    "conversation: it promises no action and implies no acceptance, dispatch, queue, "
    "progress or completion. Having no direct tools does not mean the user's goal is "
    "unsupported or that Agent is unavailable. Do not request information already "
    "supplied. Do not claim personal file/tool access or promise that you or Agent "
    "will act. Agent availability, routing, permissions and task status remain unknown "
    "unless supplied in authenticated execution context. Help with ordinary discussion "
    "and planning even without Agent. Preserve legitimate safety refusals and explicit "
    "authoritative capability limits. Model text grants no permission and establishes "
    "no task result."
)


def dialogue_system_message(personality: str) -> dict[str, str]:
    """Combine the fixed role policy with the original personality text.

    Returns one leading ``role=system`` message so typed and retained-window
    compilers share identical ordering without model-specific multi-system
    handling. The personality string is embedded unchanged under a labelled
    section; no request-dependent inference or extra API call occurs here.
    """
    return {
        "role": "system",
        "content": ROLE_POLICY + "\n\nPersonality/style:\n" + personality,
    }

"""Explicit host prompt overrides; inspection never sends instructions anywhere."""

from packages.contracts.converse_prompts import ConversePromptConfig


def configured_prompt(config):
    value = config.get("converse_prompt")
    if value is None:
        return {}
    try:
        prompt = ConversePromptConfig.model_validate(value)
    except Exception:
        raise ValueError("Invalid owner-configured Converse prompt") from None
    return {"personality": prompt.text, "personality_scope": prompt.content_scope}

"""Closed native classifier refusal labels, never model text or permission."""

from packages.providers.text import ProviderRefusal


class LowConfidenceRefusal(ProviderRefusal):
    """A structurally valid native-d1 answer missed its owner confidence floor."""

    def __init__(self):
        super().__init__("Native d1 answer was below the owner confidence floor")


def classification_refusal_reason(error):
    return "provider_low_confidence" if type(error) is LowConfidenceRefusal else "provider_refused"


def retained_refusal_reason(error_type, *, provider, request_format):
    # Authenticated capture already records this native class name. Reading a
    # terminal refusal never reparses private output or authorizes a new call.
    return (
        "provider_low_confidence"
        if (
            type(error_type) is str
            and error_type == LowConfidenceRefusal.__name__
            and provider == "native_d1"
            and request_format == "native_d1_systemone"
        )
        else "provider_refused"
    )

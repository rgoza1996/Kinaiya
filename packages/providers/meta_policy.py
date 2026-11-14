"""Exact Meta model reasoning declarations; inspection grants no disclosure."""

WONDER_STANDARD_MODEL = "muse-wonder-1.0"
LEGACY_META_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
WONDER_STANDARD_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")


def meta_reasoning_options(model):
    # Only this exact Standard API identity gains max. In particular, neither
    # Contributor nor the Muse Code-only Wonder Full name gains that option.
    return WONDER_STANDARD_EFFORTS if model == WONDER_STANDARD_MODEL else LEGACY_META_EFFORTS

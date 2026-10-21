"""Fixed generation settings for the current Dola production workflow."""


VIDEO_MODEL = "seedance-2.0"
VIDEO_DURATION = 15


def normalize_generation_settings(model=None, duration=None):
    """Map accepted legacy/project settings to the selected 2.0 / 15s workflow."""
    return VIDEO_MODEL, VIDEO_DURATION

"""Resolve display names against the final ordered attachment list."""
import re


def resolve_reference_aliases(prompt: str, aliases: list[str], image_count: int) -> str:
    if not aliases:
        return prompt
    if len(aliases) != image_count:
        raise ValueError("reference_aliases must have one entry per image in upload order")
    mapping = {}
    for index, raw in enumerate(aliases, 1):
        name = raw.strip().removeprefix("@") or f"Image{index}"
        key = name.lower()
        if key in mapping:
            raise ValueError(f"Duplicate reference name: @{name}")
        mapping[key] = f"@Image{index}"
    # Explicit display names take precedence over positional shorthand.
    for index in range(1, image_count + 1):
        mapping.setdefault(f"image{index}", f"@Image{index}")
    names = "|".join(re.escape(name) for name in sorted(mapping, key=len, reverse=True))
    pattern = re.compile(r"(?<![\w@])@(?:" + names + r")(?![\w-])|(?<![\w@])@[\w-]+", re.I)
    def replace(match):
        name = match.group()[1:]
        if name.lower() not in mapping:
            raise ValueError(f"Unknown image reference: @{name}")
        return mapping[name.lower()]
    return pattern.sub(replace, prompt)

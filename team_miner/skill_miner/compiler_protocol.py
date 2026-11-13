"""Explicit supported dialogue protocol detection and structural validation."""

import re


def detect(body):
    declared = set(re.findall(r"te-dialogue-(?:dimensions|evidence)\.v[0-9]+", body))
    if not declared:
        return ""
    if len(declared) != 1:
        raise ValueError("ambiguous_dialogue_protocol")
    protocol = declared.pop()
    if protocol == "te-dialogue-evidence.v1":
        from .dialogue_evidence.compiler import sections
        sections(body)
    elif protocol == "te-dialogue-dimensions.v2":
        from .dialogue_dimensions.v2.compiler import sections
        sections(body)
    elif protocol != "te-dialogue-dimensions.v1":
        raise ValueError("unsupported_dialogue_protocol")
    return protocol

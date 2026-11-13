"""Deterministic validation and slicing of the accepted compiler snapshot."""

import re

import yaml

from ..dialogue_dimensions.input import sha
from . import PROTOCOL

STAGES = ("extract",)
SECTIONS = ("common", "transport", *("stage:" + stage for stage in STAGES))


def sections(body):
    front = re.match(r"\A---\n.*?\n---\n", body, re.S)
    if not front or PROTOCOL not in body:
        raise ValueError("invalid_s3_compiler_protocol_or_frontmatter")
    metadata = yaml.safe_load(front[0].strip().strip("-"))
    if not isinstance(metadata, dict) or not all(isinstance(metadata.get(k), str) and metadata[k].strip()
                                                  for k in ("name", "description")):
        raise ValueError("invalid_s3_frontmatter")
    found = re.findall(r"<!-- s3:([^>]+):(start|end) -->", body)
    if found != [(name, edge) for name in SECTIONS for edge in ("start", "end")]:
        raise ValueError("S3 Skill 分段缺失、重复或顺序错误")
    outside = re.sub(r"<!-- s3:([^>]+):start -->.*?<!-- s3:\1:end -->", "", body[len(front[0]):], flags=re.S)
    # A document title is presentation only. Unclassified custom instructions must never disappear.
    if any(line.strip() and not line.startswith("# ") for line in outside.splitlines()):
        raise ValueError("S3 自定义规则必须放在 common、transport 或对应 stage 分段内")
    return front[0], {name: body.split(f"<!-- s3:{name}:start -->", 1)[1].split(
        f"<!-- s3:{name}:end -->", 1)[0] for name in SECTIONS}


def stage_body(body, stage):
    if stage not in STAGES:
        raise ValueError("unknown_s3_stage")
    front, parts = sections(body)
    # Immutable package names prevent a later stage/job replacing an in-flight Skill.
    from ..dialogue_dimensions.input import canonical
    from .schemas import Receipt
    schema = canonical(Receipt.model_json_schema())
    suffix = sha(body + stage + schema)[:20]
    front = re.sub(r"(?m)^name:.*$", f"name: dialogue-s3-{stage}-{suffix}", front)
    return (front + "\n" + "\n".join(parts[name] for name in SECTIONS)
            + "\n## Required receipt schema\n" + schema)

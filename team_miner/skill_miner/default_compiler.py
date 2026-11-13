"""Production S3 compiler resource; independent of knowledge-point compilers."""

from pathlib import Path

from .dialogue_dimensions.input import sha
from .dialogue_evidence import PROTOCOL
from .dialogue_evidence.compiler import sections

VERSION = "taikang-dialogue-evidence-s3"
SKILL_BODY = (Path(__file__).parent / "compiler_skills" / VERSION / "SKILL.md").read_text(encoding="utf-8")
SHA256 = sha(SKILL_BODY)
sections(SKILL_BODY)

__all__ = ["PROTOCOL", "SHA256", "SKILL_BODY", "VERSION"]

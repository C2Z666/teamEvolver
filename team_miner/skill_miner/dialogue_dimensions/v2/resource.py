"""Immutable historical S2 compiler for old jobs and migration audit."""

from pathlib import Path

from ..input import sha

SKILL_BODY = (Path(__file__).parents[2] / "compiler_skills" /
              "taikang-dialogue-dimensions-s2" / "SKILL.md").read_text(encoding="utf-8")
SHA256 = sha(SKILL_BODY)

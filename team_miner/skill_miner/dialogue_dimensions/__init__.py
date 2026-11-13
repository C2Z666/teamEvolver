"""Bounded analysis of selected excellent dialogues, independent of Skill publication."""

PROTOCOL = "te-dialogue-dimensions.v1"


def is_dialogue(job):
    return job.get("execution_protocol") in {PROTOCOL, "te-dialogue-dimensions.v2", "te-dialogue-evidence.v1"}


async def tick(engine, job, action="poll"):
    if job.get("execution_protocol") == "te-dialogue-dimensions.v2":
        from .v2.orchestration import tick as execute
    elif job.get("execution_protocol") == "te-dialogue-evidence.v1":
        from ..dialogue_evidence.orchestration import tick as execute
    elif job.get("execution_protocol") == PROTOCOL:
        from .orchestration import tick as execute
    else:
        raise ValueError("unsupported_dialogue_protocol")
    return await execute(engine, job, action)

"""OV model-fingerprinted vectors generate candidates, never semantic decisions."""

import math
import re

from ..input import canonical, sha


def description(row):
    card = row.get("card", row)
    fields = ("behavior", "evaluation_question", "limits") if "behavior" in card else (
        "question", "definition", "applicability", "boundaries")
    # Routing descriptions are deliberately compact; complete cards remain in the model packet.
    return "\n".join(str(card.get(key, "")) for key in fields)[:1024]


async def vectors(engine, job, repo, run, rows):
    texts = [description(row) for row in rows]
    keys = ["embedding/" + sha(canonical([run["embedding_model"], text])) for text in texts]
    cached = {key: await repo.get(key) for key in dict.fromkeys(keys)}
    missing = list(dict.fromkeys((key, text) for key, text in zip(keys, texts) if cached[key] is None))
    for start in range(0, len(missing), 32):
        batch = missing[start:start + 32]
        value = await engine._knowledge_request(job["account"], "POST", "/api/v1/compile/embeddings",
            timeout=60, json={"target_uri": job["input_dir"], "texts": [text for _, text in batch],
                              "expected_model": run["embedding_model"]})
        if value.get("model") != run["embedding_model"] or len(value.get("vectors", [])) != len(batch):
            raise ValueError("embedding_model_or_result_changed")
        records = {}
        for (key, _), vector in zip(batch, value["vectors"]):
            if not vector or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in vector):
                raise ValueError("invalid_embedding_vector")
            cached[key] = records[key] = {"vector": vector}
        await repo.save(run, **records)
    return [cached[key]["vector"] for key in keys]


def cosine(left, right):
    if len(left) != len(right):
        raise ValueError("embedding_dimensions_changed")
    norm = math.sqrt(sum(x*x for x in left) * sum(x*x for x in right))
    return sum(a*b for a, b in zip(left, right)) / norm if norm else 0.0


def lexical(left, right):
    def terms(text):
        return set(re.findall(r"\w", text.lower()))
    a, b = terms(description(left)), terms(description(right))
    return len(a & b) / max(1, len(a | b))


async def recall(engine, job, repo, run, evidence, dimensions, limit):
    if not dimensions:
        return {row["evidence_id"]: [] for row in evidence}
    ev = await vectors(engine, job, repo, run, evidence)
    dv = await vectors(engine, job, repo, run, dimensions)
    return {row["evidence_id"]: [dimensions[i]["dimension_id"] for i in sorted(range(len(dimensions)),
            key=lambda i: (-cosine(vector, dv[i]) - .15 * lexical(row, dimensions[i]),
                           dimensions[i]["dimension_id"]))[:limit]] for row, vector in zip(evidence, ev)}


async def neighbors(engine, job, repo, run, rows, limit):
    if not rows:
        return []
    embedded = await vectors(engine, job, repo, run, rows)
    remaining, batches = set(range(len(rows))), []
    while remaining:
        first = min(remaining, key=lambda i: rows[i]["evidence_id"])
        indices = sorted(remaining, key=lambda i: (-cosine(embedded[first], embedded[i]),
                                                   rows[i]["evidence_id"]))[:limit]
        batches.append([rows[i] for i in indices])
        remaining.difference_update(indices)
    return batches

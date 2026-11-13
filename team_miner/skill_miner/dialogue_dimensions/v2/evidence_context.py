"""Build exact, whole-turn source context and auditable representative selections."""

from collections import defaultdict, deque

from ..input import case_key, sha

ROLES = ("trigger_refs", "action_refs", "response_refs")


def span_id(row, sentence_id):
    return "span-" + sha(case_key(row) + ":" + sentence_id)[:24]


def source_spans(run, row, refs, *, adjacent=False):
    case = run["cases"][case_key(row)]
    index = {s["sentence_id"]: s for s in case["sentence_index"]}
    turns = {index[ref]["turn_index"] for ref in refs}
    if adjacent:
        turns |= {turn + delta for turn in list(turns) for delta in (-1, 1)}
    return [{"span_ref": span_id(row, s["sentence_id"]), "case_ref": row["case_ref"],
             "case_version": row["case_version"], "sentence_ids": [s["sentence_id"]],
             "speaker": s["speaker"], "turn_index": s["turn_index"],
             "text": case["dialogue_text"][s["char_start"]:s["char_end"]]}
            for s in index.values() if s["turn_index"] in turns]


def ground(run, payload):
    """Rebuild from immutable observations, never from another observation's case-wide refs."""
    result, sources = dict(payload), {}
    if "dimensions" in result:
        result["dimensions"] = [{k: d[k] for k in ("dimension_id", "version", "card")}
                                for d in result["dimensions"]]
    if "dimension" in result:
        result["dimension"] = {k: result["dimension"][k] for k in ("dimension_id", "version", "card")}
    evidence = []
    for row in payload.get("evidence", []):
        raw = run["evidence"].get(row.get("evidence_id"), row)
        refs = [ref for role in ROLES for ref in raw[role]]
        for source in source_spans(run, raw, refs):
            sources[source["span_ref"]] = source
        evidence.append({**{k: v for k, v in raw.items() if k not in ROLES},
                         **{role: [span_id(raw, ref) for ref in raw[role]] for role in ROLES},
                         "source_span_refs": list(dict.fromkeys(span_id(raw, ref) for ref in refs))})
    if "evidence" in payload:
        result["evidence"] = evidence
    audits = []
    for row in payload.get("audit_examples", []):
        refs = row["support_refs"] + row["counter_refs"]
        for source in source_spans(run, row, refs, adjacent=True):
            sources[source["span_ref"]] = source
        audits.append({**row, **{role: [span_id(row, ref) for ref in row[role]]
                                for role in ("support_refs", "counter_refs")}})
    if "audit_examples" in payload:
        result["audit_examples"] = audits
    gaps = []
    for row in payload.get("audit_gaps", []):
        for source in source_spans(run, row, row["sentence_refs"], adjacent=True):
            sources[source["span_ref"]] = source
        gaps.append({**{k: v for k, v in row.items() if k != "sentence_refs"},
                     "source_span_refs": [span_id(row, ref) for ref in row["sentence_refs"]]})
    if "audit_gaps" in payload:
        result["audit_gaps"] = gaps
    if any(key in payload for key in ("evidence", "audit_examples", "audit_gaps")):
        versions = defaultdict(set)
        for source in sources.values():
            versions[source["case_ref"]].add(source["case_version"])
        result["source_versions"] = {key: next(iter(values)) for key, values in versions.items() if len(values) == 1}
        result["spans"] = [{k: v for k, v in s.items()
                            if k != "case_version" or s["case_ref"] not in result["source_versions"]}
                           for s in sources.values()]
        result["context_contract"] = "exact-evidence-v1"
    return result


def representatives(run, dimensions):
    versions = {d["dimension_id"]: d["version"] for d in dimensions}
    ids = {m["evidence_id"] for m in run["memberships"]
           if m.get("active", True) and versions.get(m["dimension_id"]) == m["version"]}
    evidence = [run["evidence"][key] for key in sorted(ids)]
    audits = [a for a in run["audits"] if versions.get(a["dimension_id"]) == a["version"]
              and (a["support_refs"] or a["counter_refs"])]
    return evidence, audits


def selection_items(payload):
    """Round-robin categories and cases; choose entire observations and audit contexts."""
    buckets = defaultdict(lambda: defaultdict(deque))
    for kind in ("evidence", "audit_examples"):
        rows = payload.get(kind, [])
        for row in rows:
            signal = row["value_status"] if kind == "evidence" else row["signal"]
            buckets[(kind, signal)][case_key(row)].append(row)
    order = [("evidence", "mixed"), ("audit_examples", "counter"), ("evidence", "support_candidate"),
             ("audit_examples", "support"), ("audit_examples", "mixed"),
             ("audit_examples", "not_demonstrated"), ("evidence", "uncertain")]
    order += sorted(set(buckets) - set(order))
    queues = {}
    for key, cases in buckets.items():
        queue = deque()
        while any(cases.values()):
            for case in sorted(cases):
                if cases[case]:
                    queue.append(cases[case].popleft())
        queues[key] = queue
    result = []
    while any(queues.values()):
        for key in order:
            if queues.get(key):
                result.append((key[0], queues[key].popleft()))
    return result


def select_to_budget(run, stage, payload, make_packet):
    """Fit complete units to the real packet budget, preserving provenance and omission counts."""
    selected = {**payload, "evidence": [], "audit_examples": []}
    items, omitted = selection_items(payload), []
    dimensions = payload.get("dimensions", [payload["dimension"]] if "dimension" in payload else [])
    required = []
    for dimension in dimensions:
        ids = {m["evidence_id"] for m in run["memberships"] if m.get("active", True)
               and m["dimension_id"] == dimension["dimension_id"] and m["version"] == dimension["version"]}
        candidates = [row for kind, row in items if kind == "evidence" and row["evidence_id"] in ids]
        if candidates:
            required.append(ids)
    # Each dimension's behavior gets a chance before adding repeated examples from one dimension.
    first = []
    for ids in required:
        if not any(row["evidence_id"] in ids for _, row in first):
            first.append(next((kind, row) for kind, row in items
                              if kind == "evidence" and row["evidence_id"] in ids))
    items = first + [item for item in items if item not in first]
    totals = {kind: len(payload.get(kind, [])) for kind in ("evidence", "audit_examples")}

    def candidate(value):
        summary = {kind: {"available": totals[kind], "selected": len(value[kind]),
                          "omitted": totals[kind] - len(value[kind])} for kind in totals}
        return ground(run, {**value, "representative_selection": summary})

    for kind, row in items:
        trial = {**selected, kind: [*selected[kind], row]}
        try:
            make_packet(candidate(trial))
        except ValueError as exc:
            if str(exc) != "capacity_pending":
                raise
            omitted.append({"kind": kind, "row": row})
        else:
            selected = trial
    # A linked dimension must not lose all of its actual behavior evidence.
    if payload.get("evidence") and not selected["evidence"]:
        raise ValueError("capacity_pending")
    selected_ids = {row["evidence_id"] for row in selected["evidence"]}
    if any(not ids.intersection(selected_ids) for ids in required):
        raise ValueError("capacity_pending")
    if items and not selected["evidence"] and not selected["audit_examples"]:
        raise ValueError("capacity_pending")
    fitted = candidate(selected)
    make_packet(fitted)
    return fitted, omitted

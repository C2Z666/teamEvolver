import { useState } from "react";
import { Button } from "@/components/ui/button";
import { miningApi } from "./miningApi";
import type { ArtifactContent, MiningArtifact } from "./types";

type Fact = { text: string; sentence_refs: string[]; metadata_refs: string[] };
type Evidence = {
  evidence_id: string; case_ref: string; case_version: string; behavior: string; action_refs: string[];
  scenario: Fact; trigger: Fact; observed_conditions: Fact[];
  suggested_applicability: { text: string; basis_refs: string[] }[];
  uncertainties: { code: string; detail: string }[];
};
type Case = { case_ref: string; case_version: string; dialogue_text: string;
  sentence_index: { sentence_id: string; speaker: string; char_start: number; char_end: number }[] };

export default function MiningEvidenceResults({ artifacts }: { artifacts: MiningArtifact[] }) {
  const [rows, setRows] = useState<Evidence[] | null>(null);
  const [cases, setCases] = useState<Case[]>([]);
  const [selected, setSelected] = useState<Evidence | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const available = artifacts.filter(a => /\/(evidence|cases)-\d+\.jsonl$/.test(a.path));
  if (!available.length) return null;
  async function load() {
    setBusy(true); setError("");
    try {
      const result: { path: string; content: string }[] = [];
      // Bounded reads also work for large exports without flooding the API.
      for (const artifact of available) {
        result.push(await miningApi<ArtifactContent>(`/api/mining/artifacts/content?path=${encodeURIComponent(artifact.path)}`));
      }
      const parse = (kind: string) => result.filter(a => a.path.includes(`/${kind}-`))
        .flatMap(a => a.content.split("\n").filter(Boolean).map(line => JSON.parse(line)));
      setCases(parse("cases")); setRows(parse("evidence"));
    } catch (e) { setError(e instanceof Error ? e.message : "读取失败"); }
    finally { setBusy(false); }
  }
  const source = cases.find(c => c.case_ref === selected?.case_ref && c.case_version === selected?.case_version);
  return <div className="rounded border p-3 space-y-3 text-sm">
    <Button variant="outline" disabled={busy} onClick={load}>{busy ? "读取中…" : "查看行为与原文"}</Button>
    {error && <p role="alert">{error}</p>}
    {rows?.length === 0 && <p>没有已验收 Evidence；请结合交付状态与覆盖清单区分完整零结果和执行失败。</p>}
    {rows && <div className="grid gap-3 md:grid-cols-2">
      <div className="max-h-96 overflow-auto space-y-2">{rows.map(row => <button key={row.evidence_id}
        className="block w-full rounded border p-2 text-left hover:bg-accent-soft" onClick={() => setSelected(row)}>
        <strong>{row.behavior}</strong><p className="text-xs text-muted-foreground">{row.case_ref}</p>
      </button>)}</div>
      {selected && <div className="space-y-2 max-h-96 overflow-auto">
        <p><strong>场景：</strong>{selected.scenario.text || "原文未说明"}</p>
        <p><strong>触发：</strong>{selected.trigger.text || "原文未说明"}</p>
        <p><strong>事实条件：</strong>{selected.observed_conditions.map(f => f.text).join("；") || "无"}</p>
        <p><strong>适用建议（推断）：</strong>{selected.suggested_applicability.map(f => f.text).join("；") || "无"}</p>
        {selected.uncertainties.map((u, i) => <p key={i}>疑点：{u.detail}</p>)}
        <strong>原文上下文（高亮动作句）</strong>
        {source?.sentence_index.map(s => <p key={s.sentence_id}
          className={selected.action_refs.includes(s.sentence_id) ? "bg-amber-100 text-black p-1" : ""}>
          <span className="text-xs">{s.sentence_id} · {s.speaker} </span>
          {Array.from(source.dialogue_text).slice(s.char_start, s.char_end).join("")}
        </p>)}
      </div>}
    </div>}
  </div>;
}

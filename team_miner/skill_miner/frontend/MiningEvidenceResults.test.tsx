// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import MiningEvidenceResults from "./MiningEvidenceResults";

vi.mock("./miningApi", () => ({ miningApi: async (url: string) => ({
  path: decodeURIComponent(url.split("path=")[1]),
  content: JSON.stringify(url.includes("cases-") ? {
    case_ref: "case1", case_version: "v1", dialogue_text: "😀方法。",
    sentence_index: [{ sentence_id: "s1", speaker: "HWP", char_start: 1, char_end: 4 }],
  } : {
    evidence_id: "e1", case_ref: "case1", case_version: "v1", behavior: "先确认时间", action_refs: ["s1"],
    scenario: { text: "初次交流" }, trigger: { text: "" }, observed_conditions: [],
    suggested_applicability: [{ text: "需要约定交流时长时" }], uncertainties: [],
  }),
}) }));

it("separates inferred applicability and renders Unicode code point source ranges", async () => {
  render(<MiningEvidenceResults artifacts={["evidence", "cases"].map(name => ({
    path: `/out/${name}-0001.jsonl`, name, kind: "semantic", size_bytes: 100, skill_name: "",
  }))} />);
  fireEvent.click(screen.getByText("查看行为与原文"));
  fireEvent.click(await screen.findByText("先确认时间"));
  expect(screen.getByText("适用建议（推断）：")).toBeTruthy();
  expect(screen.getByText("方法。")).toBeTruthy();
});

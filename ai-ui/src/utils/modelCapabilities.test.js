// SPDX-License-Identifier: MIT
//
// plan.html Phase 6.5 items 3 and 4 — the Providers screen's capability editor.
//
// Items 3 and 4 both need an administrator to DECLARE a fact about a model
// that already exists: a video model's per-second rate, and a confirmation
// that the modality guessed from its id is right. Before the editor the screen
// could add, toggle, star and delete only, so the 59 models imported by
// "Sync models" were unreachable except by a raw API call — and both items
// would have shipped as warnings nobody could act on.
//
// The first block is the one that matters. `PUT /llm-providers/models/{id}`
// replaces `capabilities` wholesale, which is correct PUT semantics and which
// nothing depended on while there was no editor. A form submitting only the
// fields it renders therefore DELETES privacy_class, channels,
// supports_temperature and the rest — and the symptom appears days later as a
// model that quietly stopped being eligible for confidential traffic.

import { describe, it, expect } from "vitest";

import llmProviderConfigSource from "../components/LLMProviderConfig.jsx?raw";

import {
  MODALITY_OPTIONS,
  asModalityList,
  billsPerSecond,
  mergeCapabilities,
  modalityIsConfirmed,
} from "./modelCapabilities";

// A row as model discovery and db/migrate.py's backfill actually leave it:
// several keys the editor does not render, and a guessed modality.
const DISCOVERED = Object.freeze({
  context_window: 200000,
  reserved_output: 8000,
  cost_per_1m_input: 3.0,
  cost_per_1m_output: 15.0,
  privacy_class: "external",
  supports_temperature: false,
  channels: ["cli", "api"],
  modality: ["text", "image-in"],
  modality_source: "inferred",
});

const EDITS = Object.freeze({
  modality: ["text", "image-in"],
  context_window: "200000",
  cost_per_1m_input: "3",
  cost_per_1m_output: "15",
  cost_per_second: "",
  privacy_class: "external",
});

describe("mergeCapabilities — the fields the editor does not show", () => {
  it("preserves every key it does not own", () => {
    const next = mergeCapabilities(DISCOVERED, EDITS);
    expect(next.reserved_output).toBe(8000);
    expect(next.supports_temperature).toBe(false);
    expect(next.channels).toEqual(["cli", "api"]);
  });

  it("preserves a key it has never heard of", () => {
    // A provider may report anything and we must not drop it — the server
    // validates the governed keys and passes the rest through untouched.
    const next = mergeCapabilities(
      { ...DISCOVERED, vendor_specific_thing: { nested: true } },
      EDITS,
    );
    expect(next.vendor_specific_thing).toEqual({ nested: true });
  });

  it("does not mutate the row it was given", () => {
    const row = { ...DISCOVERED };
    mergeCapabilities(row, { ...EDITS, context_window: "128000" });
    expect(row.context_window).toBe(200000);
  });

  it("tolerates a row with no capabilities at all", () => {
    expect(mergeCapabilities(undefined, EDITS).context_window).toBe(200000);
    expect(mergeCapabilities(null, { modality: [] })).toEqual({});
  });
});

describe("mergeCapabilities — form values are strings", () => {
  it("writes numbers, not the strings the inputs produce", () => {
    const next = mergeCapabilities(DISCOVERED, { ...EDITS, context_window: "128000" });
    expect(next.context_window).toBe(128000);
    expect(typeof next.context_window).toBe("number");
  });

  it("deletes a cleared field rather than writing an empty string", () => {
    // "" fails the server's positive-integer check, so writing it would turn a
    // cleared field into a 422 the administrator cannot diagnose.
    const next = mergeCapabilities(DISCOVERED, { ...EDITS, context_window: "" });
    expect("context_window" in next).toBe(false);
  });

  it("keeps a zero cost, which is a real value", () => {
    // A free self-hosted model legitimately costs 0. Treating 0 as "cleared"
    // would make it fall back to the platform's conservative default rate and
    // bill a local model as though it were cloud.
    const next = mergeCapabilities(DISCOVERED, { ...EDITS, cost_per_1m_input: "0" });
    expect(next.cost_per_1m_input).toBe(0);
  });
});

describe("mergeCapabilities — modality and its provenance", () => {
  it("never writes modality_source itself", () => {
    // Saving a modality is the administrator declaring it, and the SERVER
    // records that. A client writing the marker could claim a confirmation
    // the API disagreed with — and the whole value of the marker is that it
    // is trustworthy.
    const next = mergeCapabilities(DISCOVERED, EDITS);
    expect(next.modality_source).toBe("inferred");
  });

  it("deletes modality when every box is un-checked", () => {
    // An absent modality reads as text-only to the resolver (fail-safe), which
    // is a meaningful state and different from an empty list.
    const next = mergeCapabilities(DISCOVERED, { ...EDITS, modality: [] });
    expect("modality" in next).toBe(false);
  });

  it("accepts the legacy bare-string shape rows were seeded with", () => {
    expect(asModalityList("video")).toEqual(["video"]);
    expect(asModalityList(["text", "image-in"])).toEqual(["text", "image-in"]);
    expect(asModalityList(undefined)).toEqual([]);
    expect(asModalityList(null)).toEqual([]);
  });
});

describe("mergeCapabilities — the per-second rate", () => {
  it("stores a rate for a video model", () => {
    const next = mergeCapabilities(DISCOVERED, {
      ...EDITS, modality: ["text", "video-out"], cost_per_second: "0.15",
    });
    expect(next.cost_per_second).toBe(0.15);
  });

  it("drops a stale rate when video-out is un-checked", () => {
    // Per-second billing is meaningless for a text model, so leaving the rate
    // behind would misprice nothing and confuse everything.
    const next = mergeCapabilities(
      { ...DISCOVERED, modality: ["video-out"], cost_per_second: 0.4 },
      { ...EDITS, modality: ["text"], cost_per_second: "0.4" },
    );
    expect("cost_per_second" in next).toBe(false);
  });

  it("identifies which models bill per second", () => {
    expect(billsPerSecond({ modality: ["text", "video-out"] })).toBe(true);
    expect(billsPerSecond({ modality: ["text", "image-out"] })).toBe(false);
    expect(billsPerSecond({})).toBe(false);
    expect(billsPerSecond(undefined)).toBe(false);
  });
});

describe("modalityIsConfirmed", () => {
  it("treats absent and inferred alike — only declared is a claim", () => {
    // core/tiers.py::seed_modality writes no marker onto rows seeded before
    // provenance existed, and deliberately did not backfill them: nobody had
    // confirmed those either. So the check asks for "declared" rather than
    // asking whether the value is "inferred".
    expect(modalityIsConfirmed({ modality_source: "declared" })).toBe(true);
    expect(modalityIsConfirmed({ modality_source: "inferred" })).toBe(false);
    expect(modalityIsConfirmed({ modality: ["text"] })).toBe(false);
    expect(modalityIsConfirmed({})).toBe(false);
    expect(modalityIsConfirmed(undefined)).toBe(false);
  });
});

describe("the vocabulary", () => {
  it("offers exactly the four modalities the tiers filter on", () => {
    expect(MODALITY_OPTIONS.map((o) => o.value)).toEqual([
      "text", "image-in", "image-out", "video-out",
    ]);
  });
});

// Source-level assertions, for the same reason tierGovernance.test.js has
// them: these are statements about what the component does and does not do,
// and reading the file is the cheapest honest way to check them without
// pulling a rendering library into the repo for one form.
describe("LLMProviderConfig.jsx wiring", () => {
  it("routes the save through mergeCapabilities rather than open-coding it", () => {
    expect(llmProviderConfigSource).toContain("mergeCapabilities(caps, {");
    expect(llmProviderConfigSource).not.toContain("const next = { ...caps };");
  });

  it("submits capabilities through the model PUT", () => {
    expect(llmProviderConfigSource).toContain(
      "`${API_BASE}/llm-providers/models/${model.id}`",
    );
    expect(llmProviderConfigSource).toContain("JSON.stringify({ capabilities: next })");
  });

  it("shows the per-second field only for video models", () => {
    // Rendering it for a text model would invite an administrator to set a
    // rate that nothing reads.
    expect(llmProviderConfigSource).toContain('placeholder="$/second"');
    expect(llmProviderConfigSource).toContain("{isVideo && (");
  });

  it("marks an unconfirmed modality on the row", () => {
    expect(llmProviderConfigSource).toContain("!modalityIsConfirmed(caps)");
    expect(llmProviderConfigSource).toContain("guessed");
  });

  it("keeps only one copy of the modality vocabulary", () => {
    // It used to be declared inline here as well as in core/tiers.py. Two
    // copies of an enum in two languages is how a modality the server rejects
    // ends up offered in a checkbox.
    expect(llmProviderConfigSource).not.toContain("const MODALITY_OPTIONS = [");
  });
});

// SPDX-License-Identifier: MIT
//
// plan.html Phase 7 — the model picker's option list.
//
// Five components built this list themselves and had drifted into four
// different behaviours for one rule. Three of those behaviours were wrong, and
// each wrong one is asserted here by name, because "the pickers agree now" is
// not a claim any of them can make about itself:
//
//   * KbChat.jsx read an empty allowlist as "not loaded yet" and showed the
//     whole catalogue to a user for whom everything was blocked. Paired with
//     routers/kb_ask_router.py having no explicit-pick check, the model was
//     then served.
//   * Code.jsx filtered on allowlist membership alone, and "auto" is never in
//     the allowlist (_all_model_ids() reads registry rows), so Auto vanished
//     once /my-models answered.
//   * Office.jsx had no filter at all.
//
// The badge is the other half. Chat.jsx and Code.jsx each carried a 9-row
// substring table copied by hand out of config/model_context_windows.json, and
// the copy had drifted from it — asserted against the real file below.

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it, expect } from "vitest";

import {
  AUTO_ONLY,
  applyGovernance,
  buildModelOptions,
  flattenCatalogue,
  formatBillingTier,
  formatContextWindow,
  optionLabel,
} from "./modelPicker";

// GET /all-models' real shape: an "Auto" group first, then one group per
// configured provider. `id` and `modelId` are both the registry model_id;
// `tier` is the billing tier; `context_window` comes from
// capabilities.context_window.
const CATALOGUE = Object.freeze({
  providers: [
    {
      provider: "Auto",
      models: [{ id: "auto", modelId: "auto", label: "Auto (Routing)", hint: "auto" }],
    },
    {
      provider: "Anthropic",
      models: [
        {
          id: "claude-sonnet-5",
          modelId: "claude-sonnet-5",
          label: "Claude Sonnet 5 (claude-sonnet-5)",
          tier: "paid",
          context_window: 200000,
        },
        {
          id: "claude-opus-5",
          modelId: "claude-opus-5",
          label: "Claude Opus 5 (claude-opus-5)",
          tier: "paid",
          context_window: 200000,
        },
      ],
    },
    {
      provider: "Local Ollama",
      models: [
        {
          id: "local:llama3.2:1b",
          modelId: "local:llama3.2:1b",
          label: "Llama 3.2 1B (llama3.2:1b)",
          tier: "free",
          context_window: 131072,
        },
      ],
    },
  ],
});

const ALL_IDS = ["claude-sonnet-5", "claude-opus-5", "local:llama3.2:1b"];

const values = (opts) => opts.map((o) => o.value);
const selectable = (opts) => opts.filter((o) => !o.disabled);

// ── The catalogue ──────────────────────────────────────────────────────────

describe("flattenCatalogue", () => {
  it("renders every model from every provider in the payload", () => {
    const opts = flattenCatalogue(CATALOGUE.providers);
    expect(values(selectable(opts))).toEqual(["auto", ...ALL_IDS]);
  });

  it("puts a divider before each provider except the first", () => {
    const opts = flattenCatalogue(CATALOGUE.providers);
    const dividers = opts.filter((o) => o.disabled);
    expect(dividers.map((d) => d.label)).toEqual([
      "── Anthropic ──",
      "── Local Ollama ──",
    ]);
    // Never above the first group, which is Auto.
    expect(opts[0].value).toBe("auto");
  });

  it("carries modelId, tier and the context window through", () => {
    const sonnet = flattenCatalogue(CATALOGUE.providers)
      .find((o) => o.value === "claude-sonnet-5");
    expect(sonnet.modelId).toBe("claude-sonnet-5");
    expect(sonnet.tier).toBe("paid");
    expect(sonnet.contextWindow).toBe(200000);
  });

  it("survives a malformed or absent payload", () => {
    expect(flattenCatalogue(undefined)).toEqual([]);
    expect(flattenCatalogue([])).toEqual([]);
    expect(flattenCatalogue([{ provider: "Empty", models: [] }])).toEqual([]);
  });
});

// ── The governance rule ────────────────────────────────────────────────────

describe("applyGovernance", () => {
  const opts = flattenCatalogue(CATALOGUE.providers);

  it("fails open while the allowlist has not loaded", () => {
    // A network error must not take models away — that reads as an outage.
    expect(applyGovernance(opts, [], false)).toEqual(opts);
    expect(applyGovernance(opts, undefined, false)).toEqual(opts);
  });

  it("shows Auto ONLY when governance loaded and every model is blocked", () => {
    // THE KbChat.jsx bug. That copy gated on `allowedModels.length === 0` and
    // returned the unfiltered list, so a fully-blocked user saw everything.
    const got = applyGovernance(opts, [], true);
    expect(values(got)).toEqual(["auto"]);
  });

  it("keeps auto even though the allowlist never contains it", () => {
    // THE Code.jsx bug. _all_model_ids() is built from registry rows, so
    // "auto" — a routing pseudo-model — is never a member. Filtering on
    // membership alone silently removed it.
    const got = applyGovernance(opts, ALL_IDS, true);
    expect(values(got)).toContain("auto");
    expect(ALL_IDS).not.toContain("auto");
  });

  it("filters on modelId, not on the option value", () => {
    // /model-governance/my-models returns full concrete ids, never short
    // aliases, which is why matching on `value` would pass for a model that is
    // never dispatched.
    const got = applyGovernance(opts, ["claude-sonnet-5"], true);
    expect(values(selectable(got))).toEqual(["auto", "claude-sonnet-5"]);
  });

  it("drops a divider whose whole group was filtered away", () => {
    const got = applyGovernance(opts, ["claude-sonnet-5"], true);
    expect(got.filter((o) => o.disabled).map((d) => d.label))
      .toEqual(["── Anthropic ──"]);
    // and never leaves a trailing heading with nothing under it
    expect(got[got.length - 1].disabled).not.toBe(true);
  });

  it("keeps a model that has no tier assignment (plan.html §H)", () => {
    // "A model with no tier assignment is still fully user-selectable."
    // /all-models is the enabled-model registry, not the tier table, so a
    // tier-less model arrives with no `tier` field at all.
    const withUnassigned = flattenCatalogue([
      CATALOGUE.providers[0],
      { provider: "Acme", models: [{ id: "acme-1", modelId: "acme-1", label: "Acme 1" }] },
    ]);
    const got = applyGovernance(withUnassigned, ["acme-1"], true);
    expect(values(selectable(got))).toEqual(["auto", "acme-1"]);
  });
});

// ── The full derivation ────────────────────────────────────────────────────

describe("buildModelOptions", () => {
  it("shows Auto only while the catalogue is in flight", () => {
    // Every picker used to ship a hardcoded fallback here naming models the
    // deployment may not route to. Code.jsx and CoworkDesktop.jsx fell back to
    // MODEL_PICKER, which is [] unless VITE_MODEL_PICKER is set — so their
    // <select> rendered with no options at all.
    expect(buildModelOptions([], [], false)).toEqual([...AUTO_ONLY]);
    expect(buildModelOptions(undefined, [], true)).toEqual([...AUTO_ONLY]);
  });

  it("does not hand back the frozen module constant", () => {
    // A caller that sorts or pushes in place must not corrupt AUTO_ONLY for
    // every other picker on the page.
    const a = buildModelOptions([], [], false);
    expect(a).not.toBe(AUTO_ONLY);
  });

  it("exposes no tier name in any option (Phase 7 exit criterion)", () => {
    // "No tier name appears in any user-facing picker." The eight are
    // authoritative in core/tiers.py; tierGovernance.js carries the checked copy.
    const TIERS = [
      "mini", "simple", "medium", "complex",
      "image-input", "image-output", "video-generation", "intent-classification",
    ];
    const labels = buildModelOptions(CATALOGUE.providers, ALL_IDS, true)
      .map((o) => optionLabel(o).toLowerCase());
    for (const t of TIERS) {
      expect(labels.some((l) => l.includes(t))).toBe(false);
    }
  });
});

// ── The badge ──────────────────────────────────────────────────────────────

describe("formatContextWindow", () => {
  it("reads binary windows as powers of two", () => {
    expect(formatContextWindow(131072)).toBe("128K");
    expect(formatContextWindow(262144)).toBe("256K");
    expect(formatContextWindow(65536)).toBe("64K");
    expect(formatContextWindow(1048576)).toBe("1M");
  });

  it("reads decimal windows as decimals", () => {
    expect(formatContextWindow(200000)).toBe("200K");
    expect(formatContextWindow(1000000)).toBe("1M");
    expect(formatContextWindow(1500000)).toBe("1.5M");
  });

  it("does not mistake a decimal window for a binary one", () => {
    // 256000 / 1024 is exactly 250 and 128000 / 1024 is exactly 125, so a
    // "% 1024 === 0" test renders gpt-5 as "250K" and the local default as
    // "125K". Both are wrong, and both look right in the source.
    expect(formatContextWindow(256000)).toBe("256K");
    expect(formatContextWindow(128000)).toBe("128K");
  });

  it("renders nothing rather than NaN for a missing or bad value", () => {
    for (const bad of [undefined, null, 0, -1, "", "wide", NaN, {}]) {
      expect(formatContextWindow(bad)).toBeNull();
    }
  });

  it("formats every window in config/model_context_windows.json", () => {
    // The file db/migrate.py seeds capabilities.context_window from. The badge
    // tables this replaced were a hand-copy of it with 9 of its 17 keys, and
    // had drifted: "kimi" was tagged 128K against 262144 here.
    const cfg = JSON.parse(
      fs.readFileSync(
        path.resolve(
          path.dirname(fileURLToPath(import.meta.url)),
          "../../../config/model_context_windows.json",
        ),
        "utf8",
      ),
    );
    const windows = Object.values(cfg.context_windows);
    expect(windows.length).toBeGreaterThan(0);
    for (const w of windows) {
      expect(formatContextWindow(w)).toMatch(/^\d+(\.\d)?[KM]$/);
    }
    // The specific drift, named so a regression is legible.
    expect(formatContextWindow(cfg.context_windows.kimi)).toBe("256K");
    expect(formatContextWindow(cfg.context_windows.gemini)).toBe("1M");
    expect(formatContextWindow(cfg.context_windows.deepseek)).toBe("64K");
  });
});

describe("formatBillingTier", () => {
  it("maps only the two values the backend sends", () => {
    expect(formatBillingTier("paid")).toBe("Paid");
    expect(formatBillingTier("free")).toBe("Free");
  });

  it("returns nothing for an unknown tier rather than guessing", () => {
    // CoworkDesktop.jsx had `m.tier === "paid" ? "Paid" : "Free"`, which
    // labels any unrecognised value — including a future one — as free.
    for (const bad of [undefined, null, "", "trial", "enterprise"]) {
      expect(formatBillingTier(bad)).toBeNull();
    }
  });
});

describe("optionLabel", () => {
  const opts = flattenCatalogue(CATALOGUE.providers);
  const find = (v) => opts.find((o) => o.value === v);

  it("appends the window and the price tier", () => {
    expect(optionLabel(find("claude-sonnet-5")))
      .toBe("Claude Sonnet 5 (claude-sonnet-5) · 200K · Paid");
    expect(optionLabel(find("local:llama3.2:1b")))
      .toBe("Llama 3.2 1B (llama3.2:1b) · 128K · Free");
  });

  it("adds no suffix to Auto or to a divider", () => {
    expect(optionLabel(find("auto"))).toBe("Auto (Routing)");
    expect(optionLabel(opts.find((o) => o.disabled))).toBe("── Anthropic ──");
  });

  it("omits the window when the capability is unset", () => {
    // An unseeded registry row has no context_window. No badge is the right
    // degradation; a guessed one is what this phase removed.
    expect(optionLabel({ value: "x", label: "X", tier: "paid" })).toBe("X · Paid");
    expect(optionLabel({ value: "x", label: "X" })).toBe("X");
  });
});

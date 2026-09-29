// SPDX-License-Identifier: MIT
//
// plan.html §N Phase 4's test list, asserted at the layer that holds the
// logic. The screen itself is a thin renderer over these functions.
//
// The last block is a source-level assertion rather than a unit test: the
// requirement "no create/rename/delete affordance" is a statement about what
// the component does NOT contain, and the cheapest honest way to check that
// is to read the file. The server already returns 405 for all three; this
// catches the button being added before anyone clicks it.

import { describe, it, expect } from "vitest";

// Vite's ?raw import — the component's own source, without a Node fs call or
// a path that breaks depending on which directory vitest was started from.
import tierGovernanceSource from "../components/TierGovernance.jsx?raw";

import {
  ALL_TIERS,
  availableCandidates,
  buildTierRows,
  diffTiers,
  moveRow,
  tierWarnings,
  toPutBody,
} from "./tierGovernance";

// ── Builders ────────────────────────────────────────────────────────────────

function model(overrides = {}) {
  return {
    model_id: "11111111-1111-1111-1111-111111111111",
    api_model_id: "test-model",
    display_name: "Test Model",
    provider_name: "Provider A",
    family: "anthropic",
    priority: 1,
    role: null,
    enabled: true,
    model_enabled: true,
    provider_enabled: true,
    ...overrides,
  };
}

function tiersBody(overrides = {}) {
  return {
    governance_active: false,
    tiers: ALL_TIERS.map((tier) => ({
      tier,
      label: tier,
      description: "",
      modality_requirement: "text",
      models: [],
      status: "unassigned",
      used_by: ["Some feature"],
      ...(overrides[tier] || {}),
    })),
  };
}

// ── buildTierRows: exactly eight rows, always ───────────────────────────────

describe("buildTierRows", () => {
  it("returns all eight tiers from a full response", () => {
    const rows = buildTierRows(tiersBody(), null);
    expect(rows).toHaveLength(8);
    expect(rows.map((r) => r.tier)).toEqual([...ALL_TIERS]);
  });

  it("returns eight rows from an empty response", () => {
    expect(buildTierRows({ tiers: [] }, null)).toHaveLength(8);
  });

  it("returns eight rows when the response is missing entirely", () => {
    expect(buildTierRows(null, null)).toHaveLength(8);
    expect(buildTierRows(undefined, undefined)).toHaveLength(8);
  });

  it("flags a tier the backend omitted instead of rendering seven rows", () => {
    const body = tiersBody();
    body.tiers = body.tiers.filter((t) => t.tier !== "complex");
    const rows = buildTierRows(body, null);
    expect(rows).toHaveLength(8);
    expect(rows.find((r) => r.tier === "complex").missing).toBe(true);
    expect(rows.find((r) => r.tier === "mini").missing).toBe(false);
  });

  it("still renders assignments when /tiers/resolved failed", () => {
    const rows = buildTierRows(tiersBody({ mini: { models: [model()] } }), null);
    expect(rows.find((r) => r.tier === "mini").models).toHaveLength(1);
    expect(rows.find((r) => r.tier === "mini").resolved).toBeNull();
  });

  it("attaches the resolved result to its tier", () => {
    const resolved = {
      resolved: [
        { tier: "mini", status: "resolved", model_id: "m1" },
        { tier: "complex", status: "unresolved", reason: "nothing assigned" },
      ],
    };
    const rows = buildTierRows(tiersBody(), resolved);
    expect(rows.find((r) => r.tier === "mini").resolved.model_id).toBe("m1");
    expect(rows.find((r) => r.tier === "complex").resolved.status).toBe("unresolved");
    expect(rows.find((r) => r.tier === "medium").resolved).toBeNull();
  });

  it("orders models by priority regardless of response order", () => {
    const body = tiersBody({
      medium: {
        models: [
          model({ model_id: "b", priority: 3 }),
          model({ model_id: "a", priority: 1 }),
          model({ model_id: "c", priority: 2 }),
        ],
      },
    });
    const rows = buildTierRows(body, null);
    expect(rows.find((r) => r.tier === "medium").models.map((m) => m.model_id))
      .toEqual(["a", "c", "b"]);   // priorities 1, 2, 3
  });

  it("keeps a disabled model visible — it still holds a priority slot", () => {
    const body = tiersBody({ mini: { models: [model({ model_enabled: false })] } });
    expect(buildTierRows(body, null).find((r) => r.tier === "mini").models).toHaveLength(1);
  });

  it("marks a tier with no models unassigned, and one with models active", () => {
    const body = tiersBody({ mini: { models: [model()] } });
    const rows = buildTierRows(body, null);
    expect(rows.find((r) => r.tier === "mini").status).toBe("active");
    expect(rows.find((r) => r.tier === "complex").status).toBe("unassigned");
  });
});

// ── diffTiers: one PUT per changed tier ─────────────────────────────────────

describe("diffTiers", () => {
  const base = [
    { tier: "mini", models: [model({ model_id: "a" })] },
    { tier: "medium", models: [model({ model_id: "b" }), model({ model_id: "c" })] },
  ];

  it("reports nothing when nothing changed", () => {
    expect(diffTiers(base, base)).toEqual([]);
  });

  it("reports nothing for a structurally identical copy", () => {
    expect(diffTiers(base, JSON.parse(JSON.stringify(base)))).toEqual([]);
  });

  it("reports a tier whose models were reordered", () => {
    const edited = [
      base[0],
      { tier: "medium", models: [model({ model_id: "c" }), model({ model_id: "b" })] },
    ];
    expect(diffTiers(base, edited)).toEqual(["medium"]);
  });

  it("reports a tier whose role changed", () => {
    const edited = [
      { tier: "mini", models: [model({ model_id: "a", role: "review" })] },
      base[1],
    ];
    expect(diffTiers(base, edited)).toEqual(["mini"]);
  });

  it("reports an addition and a removal", () => {
    expect(diffTiers(base, [
      { tier: "mini", models: [model({ model_id: "a" }), model({ model_id: "z" })] },
      { tier: "medium", models: [] },
    ])).toEqual(["mini", "medium"]);
  });

  it("ignores a priority renumber, because toPutBody renumbers anyway", () => {
    const edited = [
      { tier: "mini", models: [model({ model_id: "a", priority: 77 })] },
      base[1],
    ];
    expect(diffTiers(base, edited)).toEqual([]);
  });

  it("treats a null role and an absent role as the same", () => {
    const edited = [
      { tier: "mini", models: [{ model_id: "a" }] },
      base[1],
    ];
    expect(diffTiers(base, edited)).toEqual([]);
  });
});

// ── toPutBody: contiguous priorities ────────────────────────────────────────

describe("toPutBody", () => {
  it("renumbers to 1..N in list order", () => {
    const body = toPutBody([
      model({ model_id: "a", priority: 40 }),
      model({ model_id: "b", priority: 5 }),
      model({ model_id: "c", priority: 40 }),
    ]);
    expect(body.models.map((m) => [m.model_id, m.priority]))
      .toEqual([["a", 1], ["b", 2], ["c", 3]]);
  });

  it("can never emit a duplicate priority", () => {
    const body = toPutBody(Array.from({ length: 20 }, (_, i) => model({ model_id: `m${i}` })));
    const priorities = body.models.map((m) => m.priority);
    expect(new Set(priorities).size).toBe(priorities.length);
  });

  it("sends an empty list for a cleared tier — the only way to unassign", () => {
    expect(toPutBody([])).toEqual({ models: [] });
    expect(toPutBody(null)).toEqual({ models: [] });
  });

  it("normalises an absent role to null rather than omitting it", () => {
    expect(toPutBody([{ model_id: "a" }]).models[0].role).toBeNull();
  });

  it("preserves a role", () => {
    expect(toPutBody([model({ role: "review" })]).models[0].role).toBe("review");
  });
});

// ── moveRow ─────────────────────────────────────────────────────────────────

describe("moveRow", () => {
  const list = [model({ model_id: "a" }), model({ model_id: "b" }), model({ model_id: "c" })];
  const ids = (l) => l.map((m) => m.model_id);

  it("moves an entry up", () => {
    expect(ids(moveRow(list, 1, -1))).toEqual(["b", "a", "c"]);
  });

  it("moves an entry down", () => {
    expect(ids(moveRow(list, 1, 1))).toEqual(["a", "c", "b"]);
  });

  it("is a no-op past either end", () => {
    expect(ids(moveRow(list, 0, -1))).toEqual(["a", "b", "c"]);
    expect(ids(moveRow(list, 2, 1))).toEqual(["a", "b", "c"]);
  });

  it("is a no-op on an out-of-range index", () => {
    expect(ids(moveRow(list, 9, -1))).toEqual(["a", "b", "c"]);
    expect(ids(moveRow(list, -1, 1))).toEqual(["a", "b", "c"]);
  });

  it("does not mutate the input", () => {
    moveRow(list, 0, 1);
    expect(ids(list)).toEqual(["a", "b", "c"]);
  });
});

// ── availableCandidates ─────────────────────────────────────────────────────

describe("availableCandidates", () => {
  const candidates = [{ model_id: "a" }, { model_id: "b" }, { model_id: "c" }];

  it("does not offer a model already assigned to the tier", () => {
    const left = availableCandidates(candidates, [model({ model_id: "b" })]);
    expect(left.map((c) => c.model_id)).toEqual(["a", "c"]);
  });

  it("offers everything when the tier is empty", () => {
    expect(availableCandidates(candidates, [])).toHaveLength(3);
  });

  it("returns an empty list when the deployment has no capable model", () => {
    expect(availableCandidates([], [])).toEqual([]);
    expect(availableCandidates(null, null)).toEqual([]);
  });
});

// ── tierWarnings ────────────────────────────────────────────────────────────

describe("tierWarnings", () => {
  const rowFor = (tier, models, extra = {}) =>
    ({ tier, models, used_by: ["Chat Auto"], missing: false, ...extra });

  it("says nothing about a healthy multi-family tier", () => {
    expect(tierWarnings(rowFor("medium", [
      model({ model_id: "a", family: "anthropic" }),
      model({ model_id: "b", family: "openai" }),
    ]))).toEqual([]);
  });

  it("warns that a disabled model still holds a priority slot", () => {
    const [w] = tierWarnings(rowFor("mini", [model({ model_enabled: false, priority: 2 })]));
    expect(w.level).toBe("warn");
    expect(w.text).toMatch(/model is disabled/);
    expect(w.text).toMatch(/priority 2/);
  });

  it("names the provider when it is the provider that is disabled", () => {
    const [w] = tierWarnings(rowFor("mini", [model({ provider_enabled: false })]));
    expect(w.text).toMatch(/provider is disabled/);
  });

  it("warns when a cross-family tier has only one family (§M.3b)", () => {
    const warnings = tierWarnings(rowFor("medium", [
      model({ model_id: "a", family: "anthropic" }),
      model({ model_id: "b", family: "anthropic" }),
    ]));
    expect(warnings).toHaveLength(1);
    expect(warnings[0].text).toMatch(/cross-model review becomes same-model review/);
  });

  it("does not raise the single-family warning on other tiers", () => {
    expect(tierWarnings(rowFor("simple", [
      model({ model_id: "a", family: "anthropic" }),
      model({ model_id: "b", family: "anthropic" }),
    ]))).toEqual([]);
  });

  it("does not raise the single-family warning on a single-model tier", () => {
    // Covered by the reviewer/author note on complex, and meaningless here:
    // one model is trivially one family.
    expect(tierWarnings(rowFor("medium", [model()]))).toEqual([]);
  });

  it("notes that reviewer and author coincide on a one-model complex (§M.3a)", () => {
    const warnings = tierWarnings(rowFor("complex", [model()]));
    expect(warnings).toHaveLength(1);
    expect(warnings[0].level).toBe("info");
    expect(warnings[0].text).toMatch(/same model that produced the work/);
  });

  it("does not raise the reviewer note once complex has two models", () => {
    expect(tierWarnings(rowFor("complex", [
      model({ model_id: "a" }), model({ model_id: "b", family: "openai" }),
    ]))).toEqual([]);
  });

  it("names the features an unassigned tier disables", () => {
    const [w] = tierWarnings(rowFor("video-generation", [], { used_by: ["Chat video generation"] }));
    expect(w.text).toMatch(/Chat video generation will report unavailable/);
  });

  it("still explains an unassigned tier with no used_by list", () => {
    const [w] = tierWarnings({ tier: "mini", models: [], used_by: [] });
    expect(w.text).toMatch(/report unavailable/);
  });

  it("flags a tier the backend never returned", () => {
    const warnings = tierWarnings(rowFor("complex", [], { missing: true }));
    expect(warnings[0].level).toBe("error");
    expect(warnings[0].text).toMatch(/backend fault/);
  });

  it("reports every problem, not just the first", () => {
    const warnings = tierWarnings(rowFor("medium", [
      model({ model_id: "a", family: "anthropic", model_enabled: false }),
      model({ model_id: "b", family: "anthropic" }),
    ]));
    expect(warnings).toHaveLength(2);
  });

  it("never throws on a malformed row", () => {
    expect(() => tierWarnings(undefined)).not.toThrow();
    expect(() => tierWarnings({})).not.toThrow();
  });
});

// ── The screen must expose no create / rename / delete affordance ───────────

describe("TierGovernance.jsx", () => {
  const src = tierGovernanceSource;

  it("issues no DELETE request", () => {
    // The server answers 405, but only because tier_governance_router adds a
    // handler to refuse it — without that, model_governance_router's
    // DELETE /{dept}/{model_id} would have answered {"ok": true}. The screen
    // should not be asking in the first place.
    expect(src).not.toMatch(/method:\s*["']DELETE["']/i);
  });

  it("creates no tier", () => {
    expect(src).not.toMatch(/method:\s*["']POST["']/i);
  });

  it("writes only through PUT /tiers/{tier}/models", () => {
    const methods = [...src.matchAll(/method:\s*["'](\w+)["']/gi)].map((m) => m[1].toUpperCase());
    expect(new Set(methods)).toEqual(new Set(["PUT"]));
  });

  it("calls no endpoint other than the four it is allowed to", () => {
    // Stronger than grepping for an "Add tier" button label, and not fooled
    // by the prose on the screen that explains tiers cannot be renamed. If
    // there is no request that could create, rename or delete a tier, no
    // control can exist that does.
    const urls = [...src.matchAll(/authFetch\(\s*`?([^`,)]+)`?/g)].map((m) => m[1].trim());
    expect(new Set(urls)).toEqual(new Set([
      "BASE",
      "${BASE}/resolved",
      "${BASE}/${tier}/candidates",
      "${BASE}/${tier}/models",
    ]));
  });
});

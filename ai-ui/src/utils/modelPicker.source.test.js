// SPDX-License-Identifier: MIT
//
// plan.html Phase 7 — the pickers actually use the shared module.
//
// modelPicker.test.js proves the rules are right. It cannot prove anyone reads
// them: every assertion there would still pass against a module no component
// imports, with five hand-rolled copies of the rule still in place. That is
// the exact shape of the drift this phase removed, so it gets its own file.
//
// Source assertions, using the `?raw` import that modelCapabilities.test.js
// already uses on LLMProviderConfig.jsx. Rendering these five components in
// jsdom would mean standing up routers, auth context, websockets and a dozen
// fetches for a question about which function computes a list.

import { describe, it, expect } from "vitest";

import chatSource from "../components/Chat.jsx?raw";
import codeSource from "../components/Code.jsx?raw";
import coworkSource from "../components/CoworkDesktop.jsx?raw";
import kbChatSource from "../components/KbChat.jsx?raw";
import officeSource from "../components/Office.jsx?raw";

// The four pickers that were consolidated onto buildModelOptions().
//
// CoworkDesktop.jsx is deliberately NOT among them and is checked separately
// below: it omits Auto on purpose (its /v1/messages route has no
// complexity-based auto-routing, so an "auto" hint would silently fall through
// to a vendor default) and it relabels local: ids. Forcing it through the
// shared builder would re-add Auto on a route that cannot honour it.
const CONSOLIDATED = Object.freeze({
  "Chat.jsx": chatSource,
  "KbChat.jsx": kbChatSource,
  "Office.jsx": officeSource,
  "Code.jsx": codeSource,
});

const ALL = Object.freeze({ ...CONSOLIDATED, "CoworkDesktop.jsx": coworkSource });

// Strip comments so a name explained in prose is not mistaken for live code.
// These files document the defects they used to carry, by name.
function code(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/^\s*\/\/.*$/gm, "");
}

describe("every consolidated picker imports the shared module", () => {
  for (const [name, src] of Object.entries(CONSOLIDATED)) {
    it(`${name} calls buildModelOptions`, () => {
      expect(code(src)).toMatch(/from ["']\.\.\/utils\/modelPicker["']/);
      expect(code(src)).toMatch(/buildModelOptions\s*\(/);
    });
  }
});

describe("no picker carries its own hardcoded fallback list", () => {
  // These are the three names the hardcoded arrays went by. Each one named
  // models by display label — "Claude Sonnet 4.6", "GPT-5.4", "Gemini 2.5
  // Flash" — which is why no model-id regex ever caught them, and why they
  // went stale the moment an admin changed the registry.
  for (const [name, src] of Object.entries(ALL)) {
    it(`${name} declares no BASE_MODEL_OPTIONS / MODELS / MODEL_CONTEXT_BADGE`, () => {
      const src_ = code(src);
      expect(src_).not.toMatch(/const\s+BASE_MODEL_OPTIONS\s*=/);
      expect(src_).not.toMatch(/const\s+MODEL_CONTEXT_BADGE\s*=/);
      // `const MODELS = BASE_MODELS` in CoworkDesktop is an alias to
      // MODEL_PICKER (config.js, env-driven, empty by default), not a literal.
      expect(src_).not.toMatch(/const\s+MODELS\s*=\s*\[/);
    });
  }
});

describe("no picker infers a context window from the model name", () => {
  for (const [name, src] of Object.entries(ALL)) {
    it(`${name} has no substring→window table`, () => {
      const src_ = code(src);
      // The shape of the old table: a tuple list keyed by vendor substring.
      expect(src_).not.toMatch(/\[\s*["']gpt-5["']\s*,/);
      expect(src_).not.toMatch(/\[\s*["']sonnet["']\s*,/);
      expect(src_).not.toMatch(/function\s+_modelContextBadge/);
    });
  }
});

describe("the governance rule is not re-implemented", () => {
  it("no picker reads an empty allowlist as 'not loaded'", () => {
    // THE KbChat.jsx bug, as a source assertion because it is a one-line
    // revert. `allowedModels.length === 0` cannot distinguish "the response
    // has not arrived" from "the response said everything is blocked", and
    // reading it as the former shows a fully-blocked user the whole catalogue.
    for (const [name, src] of Object.entries(ALL)) {
      expect(code(src), name).not.toMatch(/allowedModels\.length\s*===\s*0/);
    }
  });

  it("every picker that filters has a governanceLoaded flag to filter on", () => {
    for (const [name, src] of Object.entries(ALL)) {
      expect(code(src), name).toMatch(/governanceLoaded/);
    }
  });

  it("every picker sets that flag from governance_loaded, not from a length", () => {
    // The server sends {models, governance_loaded}. Gating the state update on
    // `d?.models?.length` — as KbChat.jsx did — means the all-blocked response
    // never reaches the filter, so the filter cannot fire.
    for (const [name, src] of Object.entries(ALL)) {
      expect(code(src), name).toMatch(/governance_loaded/);
    }
  });

  it("every picker fetches the allowlist it filters on", () => {
    // Office.jsx fetched /all-models only, so it applied no governance at all.
    for (const [name, src] of Object.entries(ALL)) {
      expect(code(src), name).toMatch(/model-governance\/my-models/);
    }
  });
});

describe("CoworkDesktop keeps its own list, on purpose", () => {
  it("still omits Auto", () => {
    // Asserted so a later "consolidate the last one" change has to confront
    // the reason rather than discover it in production: Buddy's route has no
    // auto-routing, so offering Auto would silently dispatch a vendor default.
    expect(code(coworkSource)).toMatch(/\.toLowerCase\(\)\s*!==\s*["']auto["']/);
  });

  it("uses the shared tier formatter rather than its own ternary", () => {
    // It had a third copy of the Paid/Free mapping, and that copy labelled any
    // unrecognised tier value "Free".
    expect(code(coworkSource)).toMatch(/formatBillingTier/);
    expect(code(coworkSource)).not.toMatch(/\?\s*["']Paid["']\s*:\s*["']Free["']/);
  });
});

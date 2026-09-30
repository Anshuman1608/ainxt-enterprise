// SPDX-License-Identifier: MIT
//
// Every model picker's option list, as pure functions.
//
// Same arrangement as tierGovernance.js and modelCapabilities.js: the
// components fetch and render, the rules live here so they can be tested
// directly instead of through five rendered components.
//
// Five is the point. Chat.jsx, KbChat.jsx, Office.jsx, Code.jsx and
// CoworkDesktop.jsx each built this list themselves, and the copies had
// diverged in ways that could not be seen by reading any one of them:
//
//   * Chat.jsx and CoworkDesktop.jsx gate on `governanceLoaded`, which
//     distinguishes "the allowlist has not arrived" from "the allowlist is
//     empty because everything is blocked".
//   * KbChat.jsx gated on `allowedModels.length === 0` instead, and its fetch
//     only called setAllowedModels when `d?.models?.length` was truthy — so a
//     fully-blocked user hit BOTH guards and was shown the entire catalogue.
//     Unlike the web chat path, routers/kb_ask_router.py had no explicit-pick
//     check either, so the model was then served.
//   * Code.jsx gated correctly but dropped the `auto` keep, and
//     _all_model_ids() (routers/model_governance_router.py) returns registry
//     ids only — "auto" is never in it. So Auto was offered until
//     /my-models responded and then disappeared.
//   * Office.jsx had no governance filter at all.
//
// Those are four different behaviours for one rule. Deriving them from one
// function is the only way they cannot disagree again.

// The pre-catalogue fallback.
//
// Every picker used to ship a hardcoded array here — "Claude Sonnet 4.6",
// "GPT-5.4", "Gemini 2.5 Flash" — none of which is necessarily a model the
// deployment routes to, and all of which went stale the moment an admin
// changed the registry. Auto is the one entry whose label cannot go stale and
// whose routing is always valid, so it is the whole fallback now.
//
// `plan.html` Phase 7: "Fallback arrays reduced to [{auto}] with a loading
// skeleton."
export const AUTO_ONLY = Object.freeze([
  Object.freeze({ value: "auto", modelId: "auto", label: "Auto" }),
]);

// Tokens → a compact badge string, e.g. 262144 → "256K", 1000000 → "1M".
//
// Replaces a 9-row substring table that lived in both Chat.jsx and Code.jsx
// and was a hand-copy of config/model_context_windows.json. It had drifted:
// "kimi" was tagged 128K where the config says 262144, and glm / qwen /
// deepseek / llama / gemma / mistral / gpt-oss had no row at all, so any model
// matching those got no badge. Reading the number off the model row means
// there is nothing left to drift — and db/migrate.py already seeds
// capabilities.context_window from that same config file.
//
// Returns null for anything non-numeric so the caller renders no badge rather
// than "NaNK".
function _isPowerOfTwo(x) {
  return Number.isInteger(x) && x > 0 && (x & (x - 1)) === 0;
}

export function formatContextWindow(tokens) {
  const n = Number(tokens);
  if (!Number.isFinite(n) || n <= 0) return null;
  // Two conventions are in play. Self-hosted models report binary windows
  // (131072, 262144, 1048576) that read as 128K / 256K / 1M; vendors report
  // decimal ones (200000, 256000, 1000000) that read as 200K / 256K / 1M.
  //
  // Dividing by 1024 is right only when it yields a POWER of two. A plain
  // "% 1024 === 0" test looks equivalent and is not: 256000 / 1024 is exactly
  // 250, so gpt-5's window would render "250K", and 128000 / 1024 is exactly
  // 125, so the local default would render "125K".
  const mib = n / (1024 * 1024);
  if (_isPowerOfTwo(mib)) return `${mib}M`;
  const kib = n / 1024;
  if (_isPowerOfTwo(kib)) return `${kib}K`;
  if (n >= 1_000_000) {
    const m = n / 1_000_000;
    return `${Number.isInteger(m) ? m : m.toFixed(1)}M`;
  }
  return `${Math.round(n / 1000)}K`;
}

// Price-tier word for the badge. The tier itself is authoritative from the
// backend (/all-models returns tier: "paid" | "free"); this only maps it to a
// display string, and deliberately knows nothing about which provider is
// which.
export function formatBillingTier(tier) {
  if (tier === "paid") return "Paid";
  if (tier === "free") return "Free";
  return null;
}

// GET /all-models' grouped payload → one flat option list with provider
// dividers, in the shape the <select>s already render.
//
// `value`   — what gets sent as the model hint.
// `modelId` — the full concrete id governance matches on. /model-governance/
//             my-models returns full ids, never short aliases, which is why
//             the filter below cannot match on `value`.
// `disabled`— a provider divider, not a selectable model.
export function flattenCatalogue(providers) {
  const groups = Array.isArray(providers) ? providers : [];
  const out = [];
  groups.forEach((group, gi) => {
    const models = (group && group.models) || [];
    if (!models.length) return;
    // No divider above the first group — it is Auto, and a heading over a
    // single pseudo-model reads as noise.
    if (out.length > 0) {
      out.push({
        value: `__div_${gi}__`,
        label: `── ${group.provider} ──`,
        disabled: true,
      });
    }
    for (const m of models) {
      out.push({
        value: m.id,
        modelId: m.modelId || m.id,
        label: m.label || m.id,
        tier: m.tier,
        modality: m.modality,
        contextWindow: m.context_window,
      });
    }
  });
  return out;
}

// Apply the user's governance allowlist.
//
// Three rules, and every one of them was implemented differently somewhere:
//
//   1. `governanceLoaded === false` means the allowlist has not arrived (or the
//      request failed). Fail OPEN — show everything. Taking models away on a
//      network blip would look like an outage.
//   2. `governanceLoaded === true` with an empty allowlist means every model is
//      blocked. Show Auto only. This is the case KbChat.jsx got wrong, and the
//      reason the flag exists rather than inferring intent from the length.
//   3. Auto and dividers always survive. Auto is a routing pseudo-model, not a
//      registry row, so it is never in the allowlist — filtering on membership
//      alone silently removes it, which is what Code.jsx did.
export function applyGovernance(options, allowedModels, governanceLoaded) {
  const opts = Array.isArray(options) ? options : [];
  if (!governanceLoaded) return opts;
  const allowed = Array.isArray(allowedModels) ? allowedModels : [];
  const kept = opts.filter(
    (o) => o.value === "auto" || o.disabled || allowed.includes(o.modelId || o.value),
  );
  // Drop dividers whose whole group was filtered away, so the dropdown never
  // renders a bare provider heading with nothing under it.
  return kept.filter((o, i) => {
    if (!o.disabled) return true;
    const next = kept[i + 1];
    return Boolean(next) && !next.disabled;
  });
}

// The full derivation, which is what every component actually wants: catalogue
// in, governance-filtered options out, falling back to Auto while the
// catalogue is in flight.
export function buildModelOptions(providers, allowedModels, governanceLoaded) {
  const flat = flattenCatalogue(providers);
  if (!flat.length) return AUTO_ONLY.slice();
  return applyGovernance(flat, allowedModels, governanceLoaded);
}

// The label a <select> option shows: "Claude Sonnet 5 (claude-sonnet-5) · 200K · Paid".
// Dividers and Auto get no suffix.
export function optionLabel(option) {
  if (!option || option.disabled || option.value === "auto") {
    return (option && option.label) || "";
  }
  const suffix = [formatContextWindow(option.contextWindow), formatBillingTier(option.tier)]
    .filter(Boolean)
    .join(" · ");
  return suffix ? `${option.label} · ${suffix}` : option.label;
}

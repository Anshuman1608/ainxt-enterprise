// SPDX-License-Identifier: MIT
//
// Every decision the Tiers screen makes, as pure functions.
//
// TierGovernance.jsx is deliberately thin: it fetches, renders, and calls into
// here. The rules below are the ones plan.html §J.2 and §M.3 make promises
// about, and each of them is a promise about something that would otherwise
// fail silently — a tier that vanishes from the screen, a cross-model review
// that quietly becomes a same-model review, a priority slot occupied by a
// model that can never be selected. Keeping them here means they are tested
// directly rather than through a rendered component.

// The eight tiers, in display order.
//
// Authoritative copy lives in core/tiers.py, enforced in the database by
// ck_llm_tier_models_tier. This one exists so the screen can render all eight
// rows even when the API returns fewer — a tier silently missing from the
// table is exactly the failure this migration removes, so it must show up as
// a visible gap rather than as one fewer row. Parity with the Python enum is
// asserted by tests/core/test_tier_vocabulary_parity.py, which reads this
// file; it is not a second source of truth.
export const ALL_TIERS = Object.freeze([
  "mini",
  "simple",
  "medium",
  "complex",
  "image-input",
  "image-output",
  "video-generation",
  "intent-classification",
]);

// Tiers where a cross-provider constraint is applied at request time, so
// having candidates from only one family makes that constraint unsatisfiable.
// plan.html §M.3b: "Warn, do not block, at assignment time … the admin should
// be told in the UI rather than have the review silently become a same-model
// review." §J.2's mock attaches this to `medium`, the tier SDLC's
// cross_model_review runs on.
export const CROSS_FAMILY_TIERS = Object.freeze(["medium"]);

// The tier whose `role: review` rows serve §M.3a's stronger-reviewer cascade.
export const REVIEWER_TIER = "complex";

export const ROLE_REVIEW = "review";

/**
 * Merge GET /tiers and GET /tiers/resolved into exactly eight render rows.
 *
 * Always eight, built from ALL_TIERS rather than from the response array, so
 * a backend that returned seven renders eight with one flagged `missing`
 * instead of quietly showing a shorter table. Never filters: a tier with no
 * models is an `unassigned` row that names the features it disables, and a
 * model that has since been disabled stays visible (it still occupies a
 * priority slot — see the comment on _assignments_by_tier in
 * routers/tier_governance_router.py).
 *
 * `resolvedBody` may be null — /tiers/resolved failing must not stop the
 * screen rendering the assignments, which are the part the admin can act on.
 */
export function buildTierRows(tiersBody, resolvedBody) {
  const byTier = new Map();
  for (const t of tiersBody?.tiers || []) byTier.set(t.tier, t);

  const resolvedByTier = new Map();
  for (const r of resolvedBody?.resolved || []) resolvedByTier.set(r.tier, r);

  return ALL_TIERS.map((tier) => {
    const t = byTier.get(tier);
    const models = (t?.models || []).slice().sort((a, b) => a.priority - b.priority);
    return {
      tier,
      label: t?.label || tier,
      description: t?.description || "",
      modality_requirement: t?.modality_requirement || null,
      used_by: t?.used_by || [],
      models,
      status: models.length ? "active" : "unassigned",
      resolved: resolvedByTier.get(tier) || null,
      missing: t === undefined,
    };
  });
}

/**
 * Which tiers actually changed — the list "Save changes" issues a PUT for.
 *
 * Identity is the ORDERED sequence of (model, role). Priority numbers are not
 * compared because toPutBody() renumbers them on the way out, so an edit that
 * only renumbers is not a change; reordering the same two models IS one.
 *
 * Saving every tier instead would rewrite seven untouched rows on each save,
 * turning each into a `created_by: <this admin>` audit entry for a decision
 * they did not make.
 */
export function diffTiers(baseline, edited) {
  const key = (models) =>
    (models || []).map((m) => `${m.model_id}:${m.role || ""}`).join("|");

  const baseByTier = new Map((baseline || []).map((r) => [r.tier, key(r.models)]));
  return (edited || [])
    .filter((r) => baseByTier.get(r.tier) !== key(r.models))
    .map((r) => r.tier);
}

/**
 * One tier's edited list as a PUT body, with priorities renumbered 1..N.
 *
 * Contiguous renumbering makes the server's duplicate-priority 422
 * unreachable from this screen by construction. That check stays on the
 * server because the API has other callers, but an admin dragging rows around
 * should never be able to provoke it.
 */
export function toPutBody(models) {
  return {
    models: (models || []).map((m, i) => ({
      model_id: m.model_id,
      priority: i + 1,
      role: m.role || null,
    })),
  };
}

/** Move one entry up (-1) or down (+1). Out-of-range moves are a no-op. */
export function moveRow(models, index, delta) {
  const next = (models || []).slice();
  const target = index + delta;
  if (index < 0 || index >= next.length || target < 0 || target >= next.length) return next;
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}

/**
 * Candidates not already assigned to this tier.
 *
 * The server rejects the same model listed twice (422); offering it a second
 * time in the dropdown would be inviting that error.
 */
export function availableCandidates(candidates, assigned) {
  const taken = new Set((assigned || []).map((m) => m.model_id));
  return (candidates || []).filter((c) => !taken.has(c.model_id));
}

/**
 * Everything worth telling the admin about one tier's current assignment.
 *
 * All of these are warnings, never blocks. Each describes a configuration
 * that is legal, saveable, and quietly degraded — which is precisely why it
 * has to be said out loud on the screen where the choice is made.
 */
export function tierWarnings(row) {
  const out = [];
  const models = row?.models || [];

  if (row?.missing) {
    out.push({
      level: "error",
      text: `The server did not return tier "${row.tier}". The eight tiers are fixed; this is a backend fault, not a configuration choice.`,
    });
  }

  // A model that was disabled after being assigned. It still occupies a
  // priority slot and can never be selected — the same condition doctor.sh
  // reports as a stale candidate.
  for (const m of models) {
    if (m.model_enabled === false || m.provider_enabled === false) {
      const what = m.provider_enabled === false ? "provider" : "model";
      out.push({
        level: "warn",
        text: `${m.display_name || m.api_model_id} cannot be selected — its ${what} is disabled — but it still holds priority ${m.priority}.`,
      });
    }
  }

  // §M.3b — a cross-provider constraint over a single-family list can never
  // be satisfied, so the review silently stops being independent.
  if (CROSS_FAMILY_TIERS.includes(row?.tier) && models.length >= 2) {
    const families = new Set(models.map((m) => m.family));
    if (families.size === 1) {
      out.push({
        level: "warn",
        text: `Every model here is from the ${[...families][0]} family, so a request asking for a different provider cannot be satisfied and cross-model review becomes same-model review. Add a model from another provider.`,
      });
    }
  }

  // §M.3a — on a single-model tier the reviewer and the author are the same
  // model. Legitimate on a one-provider deployment, but the admin should see
  // it rather than have it be a hidden behaviour.
  if (row?.tier === REVIEWER_TIER && models.length === 1) {
    out.push({
      level: "info",
      text: "Only one model is eligible, so a review step will be performed by the same model that produced the work.",
    });
  }

  if (!models.length) {
    const features = (row?.used_by || []).join(", ");
    out.push({
      level: "warn",
      text: features
        ? `No eligible model. ${features} will report unavailable rather than falling back to a model that cannot do the job.`
        : "No eligible model. Features requesting this tier will report unavailable.",
    });
  }

  return out;
}

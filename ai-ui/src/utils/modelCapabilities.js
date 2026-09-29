// SPDX-License-Identifier: MIT
//
// The LLM Providers screen's capability decisions, as pure functions.
//
// Same arrangement as tierGovernance.js: LLMProviderConfig.jsx fetches and
// renders, the rules live here so they can be tested directly instead of
// through a rendered component.
//
// There is one rule here that is genuinely dangerous to get wrong, which is
// why this file exists at all. `PUT /llm-providers/models/{id}` REPLACES
// `capabilities` wholesale — correct PUT semantics, and nothing depended on it
// while the screen had no capability editor. A form that submits only the
// fields it shows would therefore DELETE `privacy_class`, `channels`,
// `supports_temperature`, `reserved_output` and everything else that model
// discovery or db/migrate.py's backfill had put on the row. The symptom would
// be a model that quietly stops being eligible for confidential traffic, days
// after someone corrected its context window.

// Capability keys the editor owns. Anything outside this set is passed through
// untouched — a provider may report whatever it likes and we must not drop it.
export const EDITABLE_NUMERIC_KEYS = Object.freeze([
  "context_window",
  "cost_per_1m_input",
  "cost_per_1m_output",
  "cost_per_second",
]);

// Modality values the admin API accepts. Mirrors core/tiers.py's
// MODALITY_* constants and the server-side _CAP_MODALITIES check; parity is
// not asserted here because the server rejects anything else with a 422.
export const MODALITY_OPTIONS = Object.freeze([
  { value: "text", label: "Text" },
  { value: "image-in", label: "Image input (vision)" },
  { value: "image-out", label: "Image output (generation)" },
  { value: "video-out", label: "Video output" },
]);

// `capabilities.modality` is stored as a list, but rows seeded before that was
// settled hold a bare string ("video", "image"), and the server still accepts
// both shapes for backward compatibility.
export function asModalityList(value) {
  if (Array.isArray(value)) return value;
  return value ? [value] : [];
}

// Whether a human has confirmed this model's modality.
//
// Absent and "inferred" mean the same thing to every reader: nobody has
// confirmed it. Only "declared" is a positive claim — see
// core/tiers.py::seed_modality for why the check is framed this way round and
// why no backfill marked the pre-existing rows.
//
// Worth surfacing because the guess decides whether a model appears in the
// image/video tiers at all, and a wrong guess produces no error anywhere: the
// model simply is not offered, permanently.
export function modalityIsConfirmed(capabilities) {
  return (capabilities || {}).modality_source === "declared";
}

// Video models are billed by output duration, not by tokens, so the per-token
// fields do not price them at all. Without a per-second rate on the row the
// platform bills every video model at the flat VEO_COST_PER_SECOND, which is
// how two Veo variants at different vendor prices came to cost the same.
export function billsPerSecond(capabilities) {
  return asModalityList((capabilities || {}).modality).includes("video-out");
}

// Merge an editor's values into an existing capabilities object.
//
// `edits` values are form state, so they are strings: "" means "cleared" and a
// numeric string means "set to this number". A cleared field DELETES its key
// rather than writing 0 or "", because 0 is a meaningful cost (free) and ""
// fails the server's type check.
//
// Returns a new object; never mutates `existing`.
export function mergeCapabilities(existing, edits) {
  const next = { ...(existing || {}) };

  const modality = asModalityList(edits.modality);
  if (modality.length) next.modality = modality;
  else delete next.modality;

  for (const key of EDITABLE_NUMERIC_KEYS) {
    // A per-second rate on a non-video model is meaningless. Dropping it when
    // video-out is un-checked keeps the row honest rather than leaving a
    // stale rate behind for the next reader to puzzle over.
    if (key === "cost_per_second" && !modality.includes("video-out")) {
      delete next.cost_per_second;
      continue;
    }
    const raw = edits[key];
    if (raw === "" || raw === null || raw === undefined) delete next[key];
    else next[key] = Number(raw);
  }

  if (edits.privacy_class) next.privacy_class = edits.privacy_class;
  else delete next.privacy_class;

  // `modality_source` is deliberately NOT set here. Saving a modality is the
  // administrator declaring it, and the SERVER records that
  // (routers/llm_provider_admin_router.py::update_model). A client that wrote
  // the marker itself could claim a confirmation the API disagreed with.
  return next;
}

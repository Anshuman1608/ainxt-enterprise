// SPDX-License-Identifier: MIT
//
// Model Governance › Tiers — plan.html §J.2.
//
// Eight fixed rows, each an ordered list of the models an administrator
// considers eligible for that capability tier. There is no "Add tier", no
// rename and no delete, here or in the API: the vocabulary is fixed in
// core/tiers.py, in the database CHECK, and in routers/tier_governance_router.py.
//
// This screen decides APPLICATION routing only — what runs when the platform
// chooses. It does not limit what a user may pick by hand; that is the Access
// tab.
//
// All of the actual rules live in ../utils/tierGovernance.js so they can be
// tested directly. This file fetches, renders, and saves.

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertTriangle, ArrowDown, ArrowUp, Info, Layers, Lock, Plus, RefreshCw, X,
} from "lucide-react";

import { API_BASE, authFetch } from "../config";
import { useToast } from "./ui/DialogProvider";
import {
  ROLE_REVIEW,
  availableCandidates,
  buildTierRows,
  diffTiers,
  moveRow,
  tierWarnings,
  toPutBody,
} from "../utils/tierGovernance";

const BASE = `${API_BASE}/model-governance/tiers`;

const WARN_STYLES = {
  error: { cls: "border-red-200 bg-red-50 text-red-700", Icon: AlertTriangle },
  warn: { cls: "border-amber-200 bg-amber-50 text-amber-700", Icon: AlertTriangle },
  info: { cls: "border-gray-200 bg-gray-50 text-gray-500", Icon: Info },
};

function WarningList({ warnings }) {
  if (!warnings.length) return null;
  return (
    <div className="mt-2 space-y-1">
      {warnings.map((w, i) => {
        const { cls, Icon } = WARN_STYLES[w.level] || WARN_STYLES.info;
        return (
          <div key={i} className={`flex items-start gap-1.5 rounded-md border px-2 py-1 text-[11px] ${cls}`}>
            <Icon size={12} className="mt-0.5 shrink-0" />
            <span>{w.text}</span>
          </div>
        );
      })}
    </div>
  );
}

// What GET /tiers/resolved says this tier selects right now. Until Phase 5
// this is a preview of the assignments, not a report of live routing — see
// the banner.
function ResolvedCell({ resolved }) {
  if (!resolved) return <span className="text-xs text-gray-300">—</span>;
  if (resolved.status !== "resolved") {
    return (
      <span className="text-xs text-amber-600" title={resolved.reason || ""}>
        no eligible model
      </span>
    );
  }
  return (
    <div className="text-xs text-gray-600">
      <div className="font-mono">{resolved.model_id}</div>
      {resolved.via_fallback && (
        <div className="text-[11px] text-amber-600">
          via {resolved.served_by_tier} (fallback)
        </div>
      )}
    </div>
  );
}

function AddModelPicker({ tier, assigned, onAdd }) {
  const [open, setOpen] = useState(false);
  const [candidates, setCandidates] = useState(null);   // null = not loaded
  const [error, setError] = useState("");

  const load = async () => {
    setOpen(true);
    if (candidates !== null) return;
    try {
      const r = await authFetch(`${BASE}/${tier}/candidates`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      setCandidates((await r.json()).candidates || []);
    } catch (err) {
      setError(err.message);
      setCandidates([]);
    }
  };

  if (!open) {
    return (
      <button
        type="button"
        onClick={load}
        className="flex items-center gap-1 rounded-md border border-dashed border-gray-300 px-2 py-1 text-xs text-gray-500 hover:border-indigo-300 hover:text-indigo-600"
      >
        <Plus size={12} /> Add model
      </button>
    );
  }

  const offered = availableCandidates(candidates, assigned);

  return (
    <div className="rounded-md border border-gray-200 bg-white p-1 shadow-sm">
      {candidates === null && <div className="px-2 py-1 text-xs text-gray-400">Loading…</div>}
      {error && <div className="px-2 py-1 text-xs text-red-600">Could not load models: {error}</div>}
      {candidates !== null && !error && offered.length === 0 && (
        // Not an error. A deployment with no model of the required modality
        // genuinely cannot serve this tier, and saying so is the point.
        <div className="px-2 py-1 text-xs text-gray-400">
          No further eligible model. Models must be enabled and able to satisfy
          this tier&apos;s modality.
        </div>
      )}
      {offered.map((c) => (
        <button
          key={c.model_id}
          type="button"
          onClick={() => { onAdd(c); setOpen(false); }}
          className="block w-full rounded px-2 py-1 text-left text-xs hover:bg-indigo-50"
        >
          <span className="text-gray-700">{c.provider_name}</span>
          <span className="text-gray-400"> / </span>
          <span className="font-mono text-gray-600">{c.api_model_id}</span>
          {c.is_default && <span className="ml-1 text-[10px] text-amber-600">default</span>}
        </button>
      ))}
      <button
        type="button"
        onClick={() => setOpen(false)}
        className="mt-0.5 block w-full rounded px-2 py-1 text-left text-[11px] text-gray-400 hover:bg-gray-50"
      >
        Cancel
      </button>
    </div>
  );
}

function ModelList({ row, onChange }) {
  const models = row.models;

  const setModels = (next) => onChange(row.tier, next);

  return (
    <div className="space-y-1">
      {models.map((m, i) => {
        const dead = m.model_enabled === false || m.provider_enabled === false;
        return (
          <div
            key={m.model_id}
            className={`flex items-center gap-2 rounded-md border px-2 py-1 ${
              dead ? "border-amber-200 bg-amber-50" : "border-gray-200 bg-white"
            }`}
          >
            <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded bg-indigo-100 text-[10px] font-semibold text-indigo-700">
              {i + 1}
            </span>
            <span className="min-w-0 flex-1 truncate text-xs">
              <span className="text-gray-700">{m.provider_name}</span>
              <span className="text-gray-400"> / </span>
              <span className="font-mono text-gray-600">{m.api_model_id}</span>
              {dead && <span className="ml-1.5 text-[10px] font-medium text-amber-700">disabled</span>}
            </span>

            {/* §M.3a — which candidate plays the reviewer in the SDLC cost
                cascade. NULL means a general candidate. */}
            <select
              value={m.role || ""}
              onChange={(e) => setModels(models.map((x, j) =>
                j === i ? { ...x, role: e.target.value || null } : x))}
              className="shrink-0 rounded border border-gray-200 bg-white px-1 py-0.5 text-[11px] text-gray-600"
            >
              <option value="">general</option>
              <option value={ROLE_REVIEW}>review</option>
            </select>

            <button type="button" title="Higher priority" disabled={i === 0}
              onClick={() => setModels(moveRow(models, i, -1))}
              className="shrink-0 text-gray-300 hover:text-indigo-600 disabled:opacity-30">
              <ArrowUp size={13} />
            </button>
            <button type="button" title="Lower priority" disabled={i === models.length - 1}
              onClick={() => setModels(moveRow(models, i, 1))}
              className="shrink-0 text-gray-300 hover:text-indigo-600 disabled:opacity-30">
              <ArrowDown size={13} />
            </button>
            <button type="button" title="Remove from this tier"
              onClick={() => setModels(models.filter((_, j) => j !== i))}
              className="shrink-0 text-gray-300 hover:text-red-600">
              <X size={13} />
            </button>
          </div>
        );
      })}

      <AddModelPicker
        tier={row.tier}
        assigned={models}
        onAdd={(c) => setModels([...models, {
          model_id: c.model_id,
          api_model_id: c.api_model_id,
          display_name: c.display_name,
          provider_name: c.provider_name,
          family: c.family,
          role: null,
          model_enabled: true,
          provider_enabled: true,
        }])}
      />
    </div>
  );
}

export default function TierGovernance() {
  const { toast } = useToast();

  const [baseline, setBaseline] = useState([]);   // as loaded — the diff origin
  const [rows, setRows] = useState([]);           // as edited
  const [governanceActive, setGovernanceActive] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  // No setLoading(true) here: `loading` starts true and is cleared once. A
  // refresh or a post-save reload therefore leaves the table on screen and
  // swaps the data underneath, rather than blanking a screen the admin is
  // reading.
  const load = useCallback(async () => {
    try {
      // /tiers/resolved is a diagnostic; it must not be able to stop the
      // assignments rendering, which are the part an admin can act on.
      const [tiersResp, resolvedResp] = await Promise.all([
        authFetch(BASE),
        authFetch(`${BASE}/resolved`).catch(() => null),
      ]);
      if (!tiersResp.ok) throw new Error(`HTTP ${tiersResp.status}`);
      const tiersBody = await tiersResp.json();
      const resolvedBody = resolvedResp?.ok ? await resolvedResp.json() : null;

      const built = buildTierRows(tiersBody, resolvedBody);
      setGovernanceActive(tiersBody.governance_active === true);
      setBaseline(built);
      setRows(built);
    } catch (err) {
      toast.error(`Could not load tiers: ${err.message}`);
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => { load(); }, [load]);

  const changed = useMemo(() => diffTiers(baseline, rows), [baseline, rows]);

  const onChange = (tier, models) =>
    setRows((prev) => prev.map((r) => (
      r.tier === tier ? { ...r, models, status: models.length ? "active" : "unassigned" } : r
    )));

  async function save() {
    setSaving(true);
    const failed = [];
    try {
      // One PUT per CHANGED tier. Rewriting all eight would stamp this admin
      // as created_by on seven decisions they did not make.
      for (const tier of changed) {
        const row = rows.find((r) => r.tier === tier);
        const resp = await authFetch(`${BASE}/${tier}/models`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(toPutBody(row.models)),
        });
        if (!resp.ok) {
          const detail = (await resp.json().catch(() => ({}))).detail;
          failed.push(`${tier}: ${detail || `HTTP ${resp.status}`}`);
        }
      }
      if (failed.length) failed.forEach((f) => toast.error(f));
      else toast.success(`Saved ${changed.length} tier${changed.length === 1 ? "" : "s"}.`);
    } finally {
      setSaving(false);
      // Reload either way: a partial save must leave the screen showing what
      // is actually stored, not what was typed.
      await load();
    }
  }

  if (loading) {
    return <div className="px-6 py-10 text-center text-sm text-gray-400">Loading tiers…</div>;
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Layers className="h-4 w-4 text-indigo-500" />
          <h2 className="text-sm font-semibold text-gray-800">Capability tiers</h2>
          <span className="flex items-center gap-1 rounded-full bg-gray-100 px-2 py-0.5 text-[11px] text-gray-500">
            <Lock size={10} /> 8 fixed tiers
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button type="button" onClick={load} disabled={saving}
            className="flex items-center gap-1 rounded-md border border-gray-200 px-2.5 py-1.5 text-xs text-gray-600 hover:bg-gray-50">
            <RefreshCw size={12} /> Refresh
          </button>
          <button type="button" onClick={save} disabled={saving || changed.length === 0}
            className="rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-indigo-700 disabled:bg-gray-200 disabled:text-gray-400">
            {saving ? "Saving…" : changed.length ? `Save ${changed.length} change${changed.length === 1 ? "" : "s"}` : "Save changes"}
          </button>
        </div>
      </div>

      {/* Phase 5 has not landed: assignments are saved but nothing routes
          through them yet. Driven by the server's own flag so it disappears by
          itself rather than depending on someone deleting it. */}
      {governanceActive === false && (
        <div className="flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>
            <strong>Assignments are saved but not yet routing.</strong> The runtime still
            resolves models from environment constants. Use <em>Resolved now</em> to preview
            what these assignments <strong>will</strong> select once tier governance is enabled.
          </span>
        </div>
      )}

      <p className="text-xs text-gray-500">
        Application routing only. These decide which model runs when the platform chooses —
        Chat Auto, agents, SDLC stages, document generation, classification. They do not limit
        what a user may pick manually: users may select any enabled model from any provider,
        whether or not it appears here. Per-user restrictions live on the Access tab.
      </p>

      {/* Table */}
      <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
        <table className="w-full">
          <thead className="bg-gray-50">
            <tr>
              <th className="w-56 px-4 py-2 text-left text-xs font-semibold uppercase text-gray-400">Tier</th>
              <th className="px-4 py-2 text-left text-xs font-semibold uppercase text-gray-400">Eligible models (priority order)</th>
              <th className="w-28 px-4 py-2 text-left text-xs font-semibold uppercase text-gray-400">Modality</th>
              <th className="w-56 px-4 py-2 text-left text-xs font-semibold uppercase text-gray-400">Resolved now</th>
              <th className="w-48 px-4 py-2 text-left text-xs font-semibold uppercase text-gray-400">Used by</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {rows.map((row) => (
              <tr key={row.tier} className="align-top">
                <td className="px-4 py-3">
                  <div className="text-sm font-medium text-gray-800">{row.label}</div>
                  <div className="font-mono text-[11px] text-gray-400">{row.tier}</div>
                  <div className="mt-0.5 text-[11px] text-gray-500">{row.description}</div>
                  <span className={`mt-1 inline-block rounded-full px-1.5 py-0.5 text-[10px] font-medium ${
                    row.status === "active" ? "bg-green-100 text-green-700" : "bg-gray-100 text-gray-500"
                  }`}>
                    {row.status === "active" ? "Active" : "Unassigned"}
                  </span>
                </td>
                <td className="px-4 py-3">
                  <ModelList row={row} onChange={onChange} />
                  <WarningList warnings={tierWarnings(row)} />
                </td>
                <td className="px-4 py-3 text-xs text-gray-500">
                  {row.modality_requirement === "text"
                    ? <span className="text-gray-300">—</span>
                    : <span className="font-mono">{row.modality_requirement}</span>}
                </td>
                <td className="px-4 py-3"><ResolvedCell resolved={row.resolved} /></td>
                <td className="px-4 py-3 text-[11px] text-gray-500">{row.used_by.join(" · ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="text-[11px] text-gray-400">
        The eight tiers are seeded by migration and are read-only — they cannot be created,
        renamed or deleted from this screen or its API. A tier with no eligible model makes the
        features that request it report unavailable, rather than silently substituting a model
        that cannot do the job. To clear a tier, remove every model from it and save.
      </p>
    </div>
  );
}

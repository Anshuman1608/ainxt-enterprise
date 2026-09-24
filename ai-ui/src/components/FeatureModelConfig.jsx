// SPDX-License-Identifier: MIT
import { useState, useEffect, useCallback } from 'react'
import {
  ChevronDown, ChevronRight, Cpu, Lock, AlertTriangle, RotateCw, Terminal,
} from 'lucide-react'
import { authFetch, API_BASE as API } from '../config'

// Feature → Model assignment.
//
// The admin screen for "which configured LLM serves which platform feature".
// Sits alongside LLMProviderConfig (which providers/models exist) and
// ModelGovernance (who may use a model) — this is the third axis: what each
// feature uses.
//
// There are no hardcoded model or capability lists here. The catalogue comes
// from GET /feature-models (features, assignments, and what each currently
// resolves to) and GET /feature-models/{key} (the eligible/ineligible split of
// the live model list). A deployment running only OpenRouter and Ollama sees
// only its own models.
//
// Three states have to stay visually distinct, because they look identical in
// a naive dropdown and mean very different things:
//
//   * assigned          — an admin chose this model
//   * inherit           — an admin looked and chose to leave the default
//   * not assigned      — nobody has decided; the feature's built-in default
//                         applies
//
// And a fourth that the API reports separately: a feature whose data
// classification forces it on-premise. For those, cloud models are DISABLED in
// the dropdown rather than accepted-then-silently-overridden by the router's
// privacy floor at runtime. Silent override is correct behaviour but terrible
// UX — the admin must never believe an assignment took effect when it cannot.

const RULE_TONE = {
  privacy_floor:      'bg-purple-100 text-purple-700',
  env:                'bg-amber-100 text-amber-800',
  org_assignment:     'bg-blue-100 text-blue-700',
  default_assignment: 'bg-blue-100 text-blue-700',
  feature_default:    'bg-gray-100 text-gray-600',
  call_site:          'bg-gray-100 text-gray-500',
}

function Toast({ message, tone = 'green', onClose }) {
  useEffect(() => {
    if (!message) return
    const t = setTimeout(onClose, 4000)
    return () => clearTimeout(t)
  }, [message, onClose])

  if (!message) return null
  const cls = tone === 'red'
    ? 'border-red-200 bg-red-50 text-red-700'
    : 'border-green-200 bg-green-50 text-green-700'
  return (
    <div className={`fixed bottom-6 right-6 z-50 flex max-w-lg items-start gap-2 rounded-lg border px-4 py-3 text-sm shadow-lg ${cls}`}>
      <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${tone === 'red' ? 'bg-red-500' : 'bg-green-500'}`} />
      <span className="whitespace-pre-wrap">{message}</span>
    </div>
  )
}

function RequirementChips({ feature }) {
  const chips = []
  if (feature.requires_vision) chips.push('vision')
  if (feature.requires_tools) chips.push('tools')
  if (feature.requires_streaming) chips.push('streaming')
  if (feature.min_context_tokens) {
    chips.push(`${Math.round(feature.min_context_tokens / 1000)}k context`)
  }
  if (!chips.length) return null
  return (
    <div className="mt-1 flex flex-wrap gap-1">
      {chips.map(c => (
        <span key={c} className="rounded bg-gray-100 px-1.5 py-0.5 text-[10px] text-gray-600">
          {c}
        </span>
      ))}
    </div>
  )
}

// One feature's editor. Loads its eligible/ineligible model split lazily, so
// opening the screen is one request rather than one per feature.
function FeatureRow({ feature, orgId, capabilities, onSaved, onError }) {
  const [open, setOpen] = useState(false)
  const [detail, setDetail] = useState(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [choice, setChoice] = useState('')       // '' = inherit, 'cap:x', or a model uuid
  const [fallbacks, setFallbacks] = useState([])

  const assignment = feature.assignment
  const resolved = feature.resolved || {}

  const loadDetail = useCallback(async () => {
    setLoading(true)
    try {
      const r = await authFetch(`${API}/feature-models/${encodeURIComponent(feature.feature_key)}?org_id=${encodeURIComponent(orgId)}`)
      if (!r.ok) throw new Error(await r.text())
      const data = (await r.json()).feature
      setDetail(data)
      const a = data.assignment
      setChoice(a?.model_id ? a.model_id : (a?.capability_override ? `cap:${a.capability_override}` : ''))
      setFallbacks(a?.fallback_model_ids || [])
    } catch (e) {
      onError(`Could not load ${feature.feature_key}: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }, [feature.feature_key, orgId, onError])

  useEffect(() => { if (open && !detail) loadDetail() }, [open, detail, loadDetail])

  async function save() {
    setSaving(true)
    try {
      const body = { org_id: orgId, fallback_model_ids: fallbacks }
      if (choice.startsWith('cap:')) body.capability_override = choice.slice(4)
      else if (choice) body.model_id = choice
      const r = await authFetch(`${API}/feature-models/${encodeURIComponent(feature.feature_key)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
      const data = await r.json().catch(() => ({}))
      if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`)
      onSaved(`${feature.display_name} → ${data.resolved?.hint || 'platform default'}`)
      setDetail(null)
    } catch (e) {
      // The API's 422 detail explains exactly which requirement the model
      // failed, so surface it verbatim rather than a generic failure.
      onError(String(e.message))
    } finally {
      setSaving(false)
    }
  }

  async function clearAssignment() {
    setSaving(true)
    try {
      const r = await authFetch(`${API}/feature-models/${encodeURIComponent(feature.feature_key)}?org_id=${encodeURIComponent(orgId)}`, { method: 'DELETE' })
      if (!r.ok && r.status !== 404) throw new Error(await r.text())
      onSaved(`${feature.display_name} reset to the platform default`)
      setDetail(null)
    } catch (e) {
      onError(String(e.message))
    } finally {
      setSaving(false)
    }
  }

  const tone = RULE_TONE[resolved.rule] || 'bg-gray-100 text-gray-600'

  return (
    <div className="border-b border-gray-100 last:border-0">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        className="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-gray-50"
      >
        {open
          ? <ChevronDown className="mt-0.5 h-4 w-4 shrink-0 text-gray-400" />
          : <ChevronRight className="mt-0.5 h-4 w-4 shrink-0 text-gray-400" />}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium text-gray-800">{feature.display_name}</span>
            {feature.local_only && (
              <span className="flex items-center gap-1 rounded bg-purple-100 px-1.5 py-0.5 text-[10px] text-purple-700">
                <Lock className="h-3 w-3" /> on-premise only
              </span>
            )}
            {!feature.declared_in_code && (
              <span
                className="flex items-center gap-1 rounded bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-800"
                title="This row exists in the database but no longer in core/feature_registry.py. Nothing resolves against it."
              >
                <AlertTriangle className="h-3 w-3" /> stale
              </span>
            )}
          </div>
          <div className="truncate font-mono text-[11px] text-gray-400">{feature.feature_key}</div>
          <RequirementChips feature={feature} />
        </div>
        <div className="shrink-0 text-right">
          <div className="font-mono text-xs text-gray-700">{resolved.hint || '—'}</div>
          <span className={`mt-1 inline-block rounded px-1.5 py-0.5 text-[10px] ${tone}`}>
            {resolved.label || 'unresolved'}
          </span>
        </div>
      </button>

      {open && (
        <div className="bg-gray-50 px-4 pb-4 pt-1">
          {feature.description && (
            <p className="mb-3 text-xs text-gray-600">{feature.description}</p>
          )}

          {loading && <div className="py-4 text-xs text-gray-500">Loading models…</div>}

          {detail && (
            <>
              <label className="mb-1 block text-xs font-medium text-gray-700">Model</label>
              <select
                value={choice}
                onChange={e => setChoice(e.target.value)}
                className="w-full rounded border border-gray-300 bg-white px-2 py-1.5 text-sm"
              >
                {/* Distinct from having no row at all — see the header note. */}
                <option value="">Inherit platform default{feature.default_capability ? ` (${feature.default_capability})` : ''}</option>
                <optgroup label="Capability — let the platform pick">
                  {capabilities.map(c => (
                    <option key={c} value={`cap:${c}`}>{c}</option>
                  ))}
                </optgroup>
                <optgroup label="Pin a specific model">
                  {detail.eligible_models.map(m => (
                    <option key={m.id} value={m.id}>
                      {m.display_name} ({m.model_id}) — {m.provider_name}
                    </option>
                  ))}
                </optgroup>
                {detail.ineligible_models.length > 0 && (
                  <optgroup label="Not eligible for this feature">
                    {detail.ineligible_models.map(m => (
                      <option key={m.id} value={m.id} disabled title={(m.reasons || []).join('\n')}>
                        {m.display_name} ({m.model_id}) — {(m.reasons || [])[0]}
                      </option>
                    ))}
                  </optgroup>
                )}
              </select>

              {detail.ineligible_models.length > 0 && (
                <p className="mt-1 text-[11px] text-gray-500">
                  {detail.ineligible_models.length} model(s) are greyed out because they
                  cannot meet this feature&apos;s requirements. Hover one to see why.
                </p>
              )}

              <label className="mb-1 mt-3 block text-xs font-medium text-gray-700">
                Fallback chain (tried in order if the model above fails)
              </label>
              {fallbacks.map((fb, i) => (
                <div key={`${fb}-${i}`} className="mb-1 flex items-center gap-2">
                  <select
                    value={fb}
                    onChange={e => setFallbacks(f => f.map((x, j) => (j === i ? e.target.value : x)))}
                    className="flex-1 rounded border border-gray-300 bg-white px-2 py-1 text-xs"
                  >
                    {detail.eligible_models.map(m => (
                      <option key={m.id} value={m.id}>{m.display_name} ({m.model_id})</option>
                    ))}
                  </select>
                  <button
                    type="button"
                    onClick={() => setFallbacks(f => f.filter((_, j) => j !== i))}
                    className="rounded px-2 py-1 text-xs text-red-600 hover:bg-red-50"
                  >
                    remove
                  </button>
                </div>
              ))}
              {detail.eligible_models.length > 0 && (
                <button
                  type="button"
                  onClick={() => setFallbacks(f => [...f, detail.eligible_models[0].id])}
                  className="mt-1 rounded border border-gray-300 bg-white px-2 py-1 text-xs text-gray-700 hover:bg-gray-100"
                >
                  + add fallback
                </button>
              )}

              <div className="mt-3 flex items-center gap-2 border-t border-gray-200 pt-3">
                <button
                  type="button"
                  onClick={save}
                  disabled={saving}
                  className="rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-700 disabled:opacity-50"
                >
                  {saving ? 'Saving…' : 'Save'}
                </button>
                {assignment && (
                  <button
                    type="button"
                    onClick={clearAssignment}
                    disabled={saving}
                    className="rounded border border-gray-300 bg-white px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-100 disabled:opacity-50"
                  >
                    Clear assignment
                  </button>
                )}
                <span className="ml-auto flex items-center gap-1 font-mono text-[10px] text-gray-400"
                      title="Break-glass override: set this environment variable to reroute the feature without DB access.">
                  <Terminal className="h-3 w-3" /> {feature.env_var}
                </span>
              </div>

              {assignment?.updated_by && (
                <p className="mt-2 text-[11px] text-gray-400">
                  Last changed by {assignment.updated_by}
                  {assignment.updated_at ? ` on ${new Date(assignment.updated_at).toLocaleString()}` : ''}
                </p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

export default function FeatureModelConfig() {
  const [features, setFeatures] = useState([])
  const [capabilities, setCapabilities] = useState([])
  const [loading, setLoading] = useState(true)
  const [toast, setToast] = useState(null)
  const [orgId] = useState('default')
  const [collapsed, setCollapsed] = useState({})

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [fr, cr] = await Promise.all([
        authFetch(`${API}/feature-models?org_id=${encodeURIComponent(orgId)}`),
        authFetch(`${API}/feature-models/capabilities`),
      ])
      if (!fr.ok) throw new Error(`features: HTTP ${fr.status}`)
      setFeatures((await fr.json()).features || [])
      if (cr.ok) setCapabilities((await cr.json()).capabilities || [])
    } catch (e) {
      setToast({ tone: 'red', message: `Could not load feature assignments: ${e.message}` })
    } finally {
      setLoading(false)
    }
  }, [orgId])

  useEffect(() => { load() }, [load])

  const onSaved = useCallback((msg) => {
    setToast({ tone: 'green', message: msg })
    load()
  }, [load])

  const onError = useCallback((msg) => setToast({ tone: 'red', message: msg }), [])

  // Group by the category the API returns, preserving its order.
  const groups = []
  for (const f of features) {
    const key = f.category || 'Other'
    let g = groups.find(x => x.category === key)
    if (!g) { g = { category: key, items: [] }; groups.push(g) }
    g.items.push(f)
  }

  const assignedCount = features.filter(f => f.assignment).length

  return (
    <div className="mx-auto max-w-5xl px-6 py-6">
      <div className="mb-5 flex items-start gap-3">
        <Cpu className="mt-1 h-6 w-6 text-blue-600" />
        <div className="flex-1">
          <h1 className="text-xl font-semibold text-gray-900">Feature Models</h1>
          <p className="mt-1 text-sm text-gray-600">
            Choose which configured LLM serves each platform feature. Models come from
            the <span className="font-medium">LLM Providers</span> screen; a feature with
            nothing assigned uses its built-in default.
          </p>
        </div>
        <button
          type="button"
          onClick={load}
          className="mt-1 flex items-center gap-1 rounded border border-gray-300 bg-white px-2 py-1 text-xs text-gray-700 hover:bg-gray-100"
        >
          <RotateCw className="h-3 w-3" /> Refresh
        </button>
      </div>

      {!loading && (
        <p className="mb-4 text-xs text-gray-500">
          {features.length} feature(s) · {assignedCount} assigned · org <code>{orgId}</code>
        </p>
      )}

      {loading && <div className="py-10 text-center text-sm text-gray-500">Loading…</div>}

      {!loading && features.length === 0 && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800">
          No features are registered yet. Run <code>python db/migrate.py</code> to seed the
          feature catalogue, or POST <code>/feature-models/sync</code> to re-seed it now.
        </div>
      )}

      {groups.map(g => (
        <div key={g.category} className="mb-4 overflow-hidden rounded-lg border border-gray-200 bg-white">
          <button
            type="button"
            onClick={() => setCollapsed(c => ({ ...c, [g.category]: !c[g.category] }))}
            className="flex w-full items-center gap-2 border-b border-gray-200 bg-gray-50 px-4 py-2 text-left"
          >
            {collapsed[g.category]
              ? <ChevronRight className="h-4 w-4 text-gray-400" />
              : <ChevronDown className="h-4 w-4 text-gray-400" />}
            <span className="text-sm font-semibold text-gray-700">{g.category}</span>
            <span className="text-xs text-gray-400">({g.items.length})</span>
          </button>
          {!collapsed[g.category] && g.items.map(f => (
            <FeatureRow
              key={f.feature_key}
              feature={f}
              orgId={orgId}
              capabilities={capabilities}
              onSaved={onSaved}
              onError={onError}
            />
          ))}
        </div>
      ))}

      <Toast
        message={toast?.message}
        tone={toast?.tone}
        onClose={() => setToast(null)}
      />
    </div>
  )
}

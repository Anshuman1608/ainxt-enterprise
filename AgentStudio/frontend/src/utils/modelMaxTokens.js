// SPDX-License-Identifier: MIT
/**
 * Model → max-output-tokens, read from the live /llm/models catalogue.
 *
 * Used by the Agent Tab (AgentEditor.jsx, AgentFactoryChat.jsx) and the
 * Workflow Tab (ConfigPanel.jsx, WorkflowPreview.jsx) to auto-set the Max
 * Tokens field on model pick and to cap its `max` attribute.
 *
 * Each catalogue entry carries `max_output_tokens`, recorded on the model's
 * registry row when the provider or model was registered. There is no
 * per-model table here. A model with no recorded value gets the platform
 * limit (`max_tokens_limit`, the backend's LLMConfig.max_tokens bound), and
 * every result is clamped to it.
 */

// Used only until /llm/models answers; tests/routers/test_model_max_tokens_source.py
// pins it to the backend validator.
const PLATFORM_LIMIT_BEFORE_LOAD = 32000;

let _platformLimit = PLATFORM_LIMIT_BEFORE_LOAD;
let _limits = new Map();
let _autoLimit = null;

function _normalise(modelId) {
    if (!modelId || typeof modelId !== 'string') return '';
    return modelId.trim().toLowerCase();
}

/**
 * Record the catalogue the user can pick from. `providers` is the grouped
 * /llm/models shape; `platformLimit` is its `max_tokens_limit`.
 */
export function registerModelCatalogue(providers, platformLimit) {
    if (Number.isInteger(platformLimit) && platformLimit > 0) _platformLimit = platformLimit;
    const next = new Map();
    let smallest = null;
    for (const group of providers || []) {
        for (const m of (group && group.models) || []) {
            const id = _normalise(m && m.id);
            if (!id || id === 'auto') continue;
            const raw = Number.isInteger(m.max_output_tokens) && m.max_output_tokens > 0
                ? m.max_output_tokens : null;
            if (raw) next.set(id, raw);
            const effective = Math.min(raw || _platformLimit, _platformLimit);
            smallest = smallest == null ? effective : Math.min(smallest, effective);
        }
    }
    _limits = next;
    // Auto may route to any allowed model, so it gets the smallest of their limits.
    _autoLimit = smallest;
}

/**
 * The max-output-tokens cap for `modelId`, never above the platform limit.
 * Gateway-namespaced ids (`anthropic/claude-x`) resolve like bare ones.
 */
export function getMaxTokensForModel(modelId) {
    const id = _normalise(modelId);
    let raw = null;
    if (id === 'auto') {
        raw = _autoLimit;
    } else if (id) {
        const slash = id.lastIndexOf('/');
        raw = _limits.get(id) ?? (slash !== -1 ? _limits.get(id.slice(slash + 1)) : undefined) ?? null;
    }
    return Math.min(raw || _platformLimit, _platformLimit);
}

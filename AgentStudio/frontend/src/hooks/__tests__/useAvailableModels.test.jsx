// SPDX-License-Identifier: MIT
//
// plan.html Phase 7 — the Agent Studio model catalogue hook.
//
// `useAvailableModels` reads its governance allowlist from the platform's
// /model-governance/my-models, the same endpoint the Chat sidebar uses, and it
// treated an empty allowlist as "no restriction":
//
//     if (!Array.isArray(allowed) || allowed.length === 0) return providers;
//
// That conflates two opposite states. The endpoint answers
// `{models, governance_loaded}` precisely so a caller can tell "the response
// has not arrived" (fail open — a network blip must not look like an outage)
// from "the response said every model is blocked" (show nothing but `auto`).
// Reading the second as the first showed a fully-blocked user the entire
// catalogue. The comment above the function claimed parity with Chat.jsx, and
// was describing Chat.jsx's behaviour from before it grew `governanceLoaded`.
//
// These are the first tests in this package. `npm test` and a jsdom vitest
// config have both been here all along, but no workflow ever called them —
// Phase 7 adds the CI step alongside this file, because a fix nothing runs is
// a fix nobody keeps.

import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import useAvailableModels, { MODEL_STATUS } from '../useAvailableModels';

// GET /all-models' real grouped shape. The platform fallback path is the only
// one that applies the allowlist client-side — /llm/models has already applied
// governance server-side — so every governance test below has to drive the
// hook down the fallback path by making /llm/models answer nothing.
const PLATFORM_CATALOGUE = {
    providers: [
        { provider: 'Auto', models: [{ id: 'auto', modelId: 'auto', label: 'Auto (Routing)' }] },
        {
            provider: 'Anthropic',
            models: [
                { id: 'claude-sonnet-5', modelId: 'claude-sonnet-5', label: 'Claude Sonnet 5' },
                { id: 'claude-opus-5', modelId: 'claude-opus-5', label: 'Claude Opus 5' },
            ],
        },
    ],
};

const REAL_IDS = ['claude-sonnet-5', 'claude-opus-5'];

function jsonResponse(body, ok = true) {
    return { ok, json: () => Promise.resolve(body) };
}

/**
 * Route each URL the hook fetches.
 *
 * @param opts.studio    body for ABStudio /llm/models (null → falls through)
 * @param opts.platform  body for platform /all-models
 * @param opts.myModels  body for /model-governance/my-models
 */
function mockFetch({ studio = null, platform = PLATFORM_CATALOGUE, myModels }) {
    return vi.fn((url) => {
        if (url.includes('/llm/models')) return Promise.resolve(jsonResponse(studio));
        if (url.includes('/all-models')) return Promise.resolve(jsonResponse(platform));
        if (url.includes('/my-models')) return Promise.resolve(jsonResponse(myModels));
        return Promise.reject(new Error(`unexpected fetch: ${url}`));
    });
}

const ids = (result) => result.current.models;

beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn());
});

afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
});

describe('useAvailableModels — the governance allowlist', () => {
    it('shows auto ONLY when governance loaded and every model is blocked', async () => {
        // The defect, stated as the behaviour. `models: []` with
        // governance_loaded: true is the administrator having blocked
        // everything for this user.
        vi.stubGlobal('fetch', mockFetch({
            myModels: { models: [], governance_loaded: true },
        }));

        const { result } = renderHook(() => useAvailableModels());
        await waitFor(() => expect(result.current.status).not.toBe(MODEL_STATUS.LOADING));

        expect(ids(result)).toEqual(['auto']);
        for (const blocked of REAL_IDS) {
            expect(ids(result)).not.toContain(blocked);
        }
    });

    it('fails open when the allowlist request fails', async () => {
        // No governance_loaded → the rules were never evaluated. Taking models
        // away here would turn a transient platform error into an apparent
        // outage of the whole Agent Studio model picker.
        vi.stubGlobal('fetch', vi.fn((url) => {
            if (url.includes('/llm/models')) return Promise.resolve(jsonResponse(null));
            if (url.includes('/all-models')) return Promise.resolve(jsonResponse(PLATFORM_CATALOGUE));
            return Promise.reject(new Error('network down'));
        }));

        const { result } = renderHook(() => useAvailableModels());
        await waitFor(() => expect(result.current.status).not.toBe(MODEL_STATUS.LOADING));

        expect(ids(result)).toEqual(['auto', ...REAL_IDS]);
    });

    it('fails open when the response omits governance_loaded', async () => {
        // An older platform build, or a proxy that strips the field. Absent is
        // not the same as false-and-empty, and must not be read as "blocked".
        vi.stubGlobal('fetch', mockFetch({ myModels: { models: [] } }));

        const { result } = renderHook(() => useAvailableModels());
        await waitFor(() => expect(result.current.status).not.toBe(MODEL_STATUS.LOADING));

        expect(ids(result)).toEqual(['auto', ...REAL_IDS]);
    });

    it('applies a partial allowlist and always keeps auto', async () => {
        // `auto` is a routing pseudo-model, never a registry row, so it is
        // never a member of the allowlist — filtering on membership alone
        // silently removes it.
        vi.stubGlobal('fetch', mockFetch({
            myModels: { models: ['claude-sonnet-5'], governance_loaded: true },
        }));

        const { result } = renderHook(() => useAvailableModels());
        await waitFor(() => expect(result.current.status).not.toBe(MODEL_STATUS.LOADING));

        expect(ids(result)).toEqual(['auto', 'claude-sonnet-5']);
        expect(ids(result)).not.toContain('claude-opus-5');
    });

    it('drops a provider group emptied by the filter', async () => {
        // Otherwise the <optgroup> renders as a bare heading with nothing under it.
        vi.stubGlobal('fetch', mockFetch({
            myModels: { models: [], governance_loaded: true },
        }));

        const { result } = renderHook(() => useAvailableModels());
        await waitFor(() => expect(result.current.status).not.toBe(MODEL_STATUS.LOADING));

        expect(result.current.providers.map((g) => g.provider)).toEqual(['Auto']);
    });
});

describe('useAvailableModels — where the catalogue comes from', () => {
    it('does not re-filter /llm/models, which is already governed server-side', async () => {
        // ABStudio's own endpoint applies the allowlist itself. Applying it
        // again here would double-filter ids that endpoint deliberately
        // rewrites, and it is why the filter is bound to the fallback path.
        vi.stubGlobal('fetch', mockFetch({
            studio: PLATFORM_CATALOGUE,
            myModels: { models: [], governance_loaded: true },
        }));

        const { result } = renderHook(() => useAvailableModels());
        await waitFor(() => expect(result.current.status).not.toBe(MODEL_STATUS.LOADING));

        expect(ids(result)).toEqual(['auto', ...REAL_IDS]);
    });

    it('reports ERROR, with a message, when neither catalogue has any model', async () => {
        // Both endpoints answer, and both answer nothing. The hook sets
        // 'No models available' and status resolves to ERROR rather than
        // EMPTY, because `fetchError` is checked first. Asserted as it
        // behaves, not as the name EMPTY suggests: EMPTY is in practice
        // unreachable, since a non-empty catalogue always keeps `auto` and so
        // resolves READY.
        vi.stubGlobal('fetch', mockFetch({
            platform: { providers: [] },
            myModels: { models: [], governance_loaded: true },
        }));

        const { result } = renderHook(() => useAvailableModels());
        await waitFor(() => expect(result.current.status).not.toBe(MODEL_STATUS.LOADING));

        expect(result.current.status).toBe(MODEL_STATUS.ERROR);
        expect(result.current.error).toBeTruthy();
    });

    it('stays LOADING until both requests have settled', async () => {
        // Otherwise the picker flashes an unfiltered list before the allowlist
        // lands, which is a governance leak measured in milliseconds.
        let releaseGovernance;
        vi.stubGlobal('fetch', vi.fn((url) => {
            if (url.includes('/llm/models')) return Promise.resolve(jsonResponse(null));
            if (url.includes('/all-models')) return Promise.resolve(jsonResponse(PLATFORM_CATALOGUE));
            return new Promise((resolve) => { releaseGovernance = resolve; });
        }));

        const { result } = renderHook(() => useAvailableModels());
        await waitFor(() => expect(result.current.providers.length).toBeGreaterThan(0));
        expect(result.current.status).toBe(MODEL_STATUS.LOADING);

        releaseGovernance(jsonResponse({ models: ['claude-sonnet-5'], governance_loaded: true }));
        await waitFor(() => expect(result.current.status).toBe(MODEL_STATUS.READY));
        expect(ids(result)).toEqual(['auto', 'claude-sonnet-5']);
    });
});

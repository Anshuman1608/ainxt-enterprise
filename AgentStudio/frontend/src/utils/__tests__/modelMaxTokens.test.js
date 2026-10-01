// SPDX-License-Identifier: MIT
//
// plan.html Rev 21 (D99/D100) — max-output caps come from the catalogue.
//
// The editors used to read a hand-kept table of 23 model ids with a 4096
// default, so any model outside it (and `auto`) was capped at 4096.

import fs from 'node:fs';
import path from 'node:path';
import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getMaxTokensForModel, registerModelCatalogue } from '../modelMaxTokens';
import useAvailableModels from '../../hooks/useAvailableModels';

const LIMIT = 32000;
const catalogue = (models) => [{ provider: 'Anthropic', models }];

beforeEach(() => registerModelCatalogue([], LIMIT));

describe('getMaxTokensForModel', () => {
    it('uses the value recorded on the model row', () => {
        registerModelCatalogue(catalogue([{ id: 'model-a', max_output_tokens: 8192 }]), LIMIT);
        expect(getMaxTokensForModel('model-a')).toBe(8192);
    });

    it('clamps a larger recorded value to the platform limit', () => {
        registerModelCatalogue(catalogue([{ id: 'model-a', max_output_tokens: 128000 }]), LIMIT);
        expect(getMaxTokensForModel('model-a')).toBe(LIMIT);
    });

    it('resolves a gateway-namespaced id', () => {
        registerModelCatalogue(catalogue([{ id: 'model-a', max_output_tokens: 8192 }]), LIMIT);
        expect(getMaxTokensForModel('vendor/model-a')).toBe(8192);
    });

    it('gives an unknown model the platform limit, not 4096', () => {
        registerModelCatalogue(catalogue([{ id: 'model-a' }]), LIMIT);
        expect(getMaxTokensForModel('model-a')).toBe(LIMIT);
        expect(getMaxTokensForModel('never-heard-of-it')).toBe(LIMIT);
    });

    it('follows the platform limit the backend serves', () => {
        registerModelCatalogue(catalogue([{ id: 'model-a' }]), 64000);
        expect(getMaxTokensForModel('model-a')).toBe(64000);
    });

    it('gives auto the smallest limit among the allowed models', () => {
        registerModelCatalogue(catalogue([
            { id: 'auto' },
            { id: 'model-a', max_output_tokens: 16384 },
            { id: 'model-b', max_output_tokens: 8192 },
            { id: 'model-c' },
        ]), LIMIT);
        expect(getMaxTokensForModel('auto')).toBe(8192);
    });

    it('gives auto the platform limit when no model has a smaller one', () => {
        registerModelCatalogue(catalogue([{ id: 'model-a' }]), LIMIT);
        expect(getMaxTokensForModel('auto')).toBe(LIMIT);
    });
});

describe('useAvailableModels registers the catalogue', () => {
    const original = global.fetch;
    afterEach(() => { global.fetch = original; });

    it('makes /llm/models values visible to the editors', async () => {
        const studio = {
            providers: catalogue([{ id: 'model-a', max_output_tokens: 4000 }]),
            max_tokens_limit: 20000,
        };
        global.fetch = vi.fn((url) => Promise.resolve({
            ok: true,
            json: () => Promise.resolve(url.includes('/llm/models') ? studio : { governance_loaded: false }),
        }));
        const { result } = renderHook(() => useAvailableModels());
        await waitFor(() => expect(result.current.models).toContain('model-a'));
        expect(getMaxTokensForModel('model-a')).toBe(4000);
        expect(getMaxTokensForModel('unknown')).toBe(20000);
    });
});

describe('the source', () => {
    it('holds no per-model token table', () => {
        const src = fs.readFileSync(path.resolve(__dirname, '../modelMaxTokens.js'), 'utf-8');
        expect(src).not.toMatch(/['"][\w.:-]+['"]\s*:\s*\d{3,}/);
        expect(src).not.toMatch(/\b4096\b/);
    });
});

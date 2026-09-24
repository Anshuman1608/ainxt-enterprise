// SPDX-License-Identifier: MIT
// modelMaxTokens — the live catalogue must beat the static table.
//
// MODEL_MAX_TOKENS is keyed by the model ids this project ships with, so every
// model an operator registers through the "LLM Providers" screen missed it and
// silently received DEFAULT_MAX_TOKENS_CAP (4096) plus a wrong context-usage
// meter. GET /llm/models now carries max_output_tokens / context_window
// straight from llm_models.capabilities, and useAvailableModels registers them
// here via registerModelLimits.
//
// The two properties that matter: a registered limit wins, and an UNregistered
// model still behaves exactly as it did before — so a deployment that never
// records any capability metadata is unaffected.

import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
    BACKEND_MAX_TOKENS_LIMIT,
    DEFAULT_MAX_TOKENS_CAP,
    clearModelLimits,
    getMaxTokensForModel,
    registerModelLimits,
} from '../modelMaxTokens'

beforeEach(() => {
    clearModelLimits()
    vi.spyOn(console, 'warn').mockImplementation(() => {})
})

describe('the static table is unchanged', () => {
    it('resolves a shipped model id', () => {
        expect(getMaxTokensForModel('claude-sonnet-4-6')).toBe(32000)
        expect(getMaxTokensForModel('gemini-3.5-flash')).toBe(16384)
    })

    it('still strips a namespace and matches the longest known prefix', () => {
        expect(getMaxTokensForModel('anthropic/claude-sonnet-4-6')).toBe(32000)
        expect(getMaxTokensForModel('claude-haiku-4-5-20251001')).toBe(32000)
    })

    it('falls back conservatively for an unknown model', () => {
        expect(getMaxTokensForModel('mistralai/mixtral-8x7b-instruct'))
            .toBe(DEFAULT_MAX_TOKENS_CAP)
    })
})

describe('registered limits take precedence', () => {
    it('honours an admin-recorded limit for a model the table has never heard of', () => {
        registerModelLimits([
            { id: 'mistralai/mixtral-8x7b-instruct', max_output_tokens: 16384, context_window: 32768 },
        ])
        expect(getMaxTokensForModel('mistralai/mixtral-8x7b-instruct')).toBe(16384)
    })

    it('resolves either the namespaced or the bare form', () => {
        registerModelLimits([{ id: 'vendor/some-model', max_output_tokens: 12000 }])
        expect(getMaxTokensForModel('vendor/some-model')).toBe(12000)
        expect(getMaxTokensForModel('some-model')).toBe(12000)
    })

    it('overrides the static table for a shipped id too', () => {
        registerModelLimits([{ id: 'gemini-3.5-flash', max_output_tokens: 8192 }])
        expect(getMaxTokensForModel('gemini-3.5-flash')).toBe(8192)
    })

    it('is still clamped to the backend hard limit', () => {
        // The backend rejects anything above LLMConfig.max_tokens le=32000, so
        // a mis-recorded capability must not let the UI offer an invalid value.
        registerModelLimits([{ id: 'over-the-cap', max_output_tokens: 999999 }])
        expect(getMaxTokensForModel('over-the-cap')).toBe(BACKEND_MAX_TOKENS_LIMIT)
    })

    it('accepts the modelId / hint key spellings the catalogue also uses', () => {
        registerModelLimits([{ modelId: 'by-model-id', max_output_tokens: 5000 }])
        registerModelLimits([{ hint: 'by-hint', max_output_tokens: 6000 }])
        expect(getMaxTokensForModel('by-model-id')).toBe(5000)
        expect(getMaxTokensForModel('by-hint')).toBe(6000)
    })
})

describe('missing or malformed data never degrades the fallback', () => {
    it('skips an entry with no recorded limit rather than storing zero', () => {
        // A model an admin has not filled in must fall through to the static
        // table, not be pinned at 0 tokens.
        registerModelLimits([{ id: 'claude-sonnet-4-6' }])
        expect(getMaxTokensForModel('claude-sonnet-4-6')).toBe(32000)
        registerModelLimits([{ id: 'unknown-model', context_window: 128000 }])
        expect(getMaxTokensForModel('unknown-model')).toBe(DEFAULT_MAX_TOKENS_CAP)
    })

    it('ignores a non-array payload', () => {
        expect(() => registerModelLimits(null)).not.toThrow()
        expect(() => registerModelLimits(undefined)).not.toThrow()
        expect(() => registerModelLimits('nonsense')).not.toThrow()
        expect(() => registerModelLimits([null, {}, { id: '' }])).not.toThrow()
        expect(getMaxTokensForModel('claude-sonnet-4-6')).toBe(32000)
    })

    it('handles empty and non-string model ids', () => {
        expect(getMaxTokensForModel('')).toBe(DEFAULT_MAX_TOKENS_CAP)
        expect(getMaxTokensForModel(null)).toBe(DEFAULT_MAX_TOKENS_CAP)
        expect(getMaxTokensForModel(42)).toBe(DEFAULT_MAX_TOKENS_CAP)
    })

    it('lets a later registration replace an earlier one', () => {
        registerModelLimits([{ id: 'm', max_output_tokens: 1000 }])
        registerModelLimits([{ id: 'm', max_output_tokens: 2000 }])
        expect(getMaxTokensForModel('m')).toBe(2000)
    })

    it('reverts to the static table once cleared', () => {
        registerModelLimits([{ id: 'gemini-3.5-flash', max_output_tokens: 8192 }])
        clearModelLimits()
        expect(getMaxTokensForModel('gemini-3.5-flash')).toBe(16384)
    })
})

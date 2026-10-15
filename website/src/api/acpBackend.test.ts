/**
 * `isKiroBackend` decides whether the pill's Kiro-only surfaces (the no-reading
 * dash) render. It must be POSITIVE — true only for the kiro id, and never for a
 * config that has not loaded — because a guess here puts a "could not read your
 * balance; open to refresh" control in front of a user whose harness has no
 * balance to read.
 */
import { describe, expect, it } from 'vitest'

import { ACP_BACKEND_KAS, ACP_BACKEND_KIRO, configuredAcpBackend, DEFAULT_ACP_BACKEND, isKiroBackend, reportsCredits } from './acpBackend'

describe('isKiroBackend', () => {
  it('is false while the config has not loaded: nothing Kiro-only renders on a guess', () => {
    expect(isKiroBackend(undefined)).toBe(false)
  })

  it('names kiro by its own id only: an unset or empty key means KAS on the gateway', () => {
    expect(ACP_BACKEND_KIRO).toBe('kiro')
    expect(isKiroBackend({ agent: { acp_backend: ACP_BACKEND_KIRO } })).toBe(true)
    expect(isKiroBackend({ agent: { acp_backend: '' } })).toBe(false)
    expect(isKiroBackend({ agent: {} })).toBe(false)
    expect(isKiroBackend({})).toBe(false)
  })
})

describe('configuredAcpBackend', () => {
  it('reads "no choice" (absent or empty) as the default, KAS', () => {
    expect(DEFAULT_ACP_BACKEND).toBe(ACP_BACKEND_KAS)
    expect(configuredAcpBackend({})).toBe(ACP_BACKEND_KAS)
    expect(configuredAcpBackend({ agent: { acp_backend: '' } })).toBe(ACP_BACKEND_KAS)
    expect(configuredAcpBackend({ agent: { acp_backend: 'kiro' } })).toBe(ACP_BACKEND_KIRO)
    expect(configuredAcpBackend({ agent: { acp_backend: 'claude' } })).toBe('claude')
  })

  it('is false for every other harness, not just claude', () => {
    for (const backend of ['claude', 'kas', 'codex', 'opencode', 'pi', 'goose', 'deepseek', 'some-future-harness']) {
      expect(isKiroBackend({ agent: { acp_backend: backend } }), backend).toBe(false)
    }
  })
})

describe('reportsCredits', () => {
  it('is false while the config has not loaded: a credits chart does not render on a guess', () => {
    expect(reportsCredits(undefined)).toBe(false)
  })

  it('is true for kiro-cli and for its KAS relay (also what an unset key means)', () => {
    expect(reportsCredits({ agent: { acp_backend: ACP_BACKEND_KIRO } })).toBe(true)
    expect(reportsCredits({})).toBe(true)
    expect(reportsCredits({ agent: { acp_backend: ACP_BACKEND_KAS } })).toBe(true)
  })

  it('is false for a harness that bills in tokens or dollars', () => {
    for (const backend of ['claude', 'codex', 'opencode', 'pi', 'goose', 'deepseek', 'some-future-harness']) {
      expect(reportsCredits({ agent: { acp_backend: backend } }), backend).toBe(false)
    }
  })
})

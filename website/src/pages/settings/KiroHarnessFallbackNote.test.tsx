/**
 * Settings → Chat → Model fallback: the note that the Kiro CLI and kiro-agent
 * harnesses apply the app.kiro.dev per-model fallbacks first appears only on
 * those harnesses. Other harnesses have no engine fallback, so the note would
 * mislead there and is not rendered.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import React from 'react'
import { KiroHarnessFallbackNote, KIRO_MODEL_SETTINGS_URL, harnessAppliesOwnFallback } from './KiroHarnessFallbackNote'

describe('KiroHarnessFallbackNote', () => {
  it('renders on the kiro-cli backend (default, empty id) with the app.kiro.dev link', () => {
    render(<KiroHarnessFallbackNote acpBackend={undefined} />)
    const note = screen.getByTestId('kiro-harness-fallback-note')
    expect(note).toBeTruthy()
    const link = note.querySelector('a')
    expect(link?.getAttribute('href')).toBe(KIRO_MODEL_SETTINGS_URL)
    expect(link?.getAttribute('target')).toBe('_blank')
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('renders on the kas backend', () => {
    render(<KiroHarnessFallbackNote acpBackend="kas" />)
    expect(screen.getByTestId('kiro-harness-fallback-note')).toBeTruthy()
  })

  it.each(['claude', 'codex', 'opencode', 'goose'])('renders nothing on the %s backend', (backend) => {
    render(<KiroHarnessFallbackNote acpBackend={backend} />)
    expect(screen.queryByTestId('kiro-harness-fallback-note')).toBeNull()
  })

  it('harnessAppliesOwnFallback mirrors the render rule', () => {
    expect(harnessAppliesOwnFallback(undefined)).toBe(true)
    expect(harnessAppliesOwnFallback('')).toBe(true)
    expect(harnessAppliesOwnFallback('kas')).toBe(true)
    expect(harnessAppliesOwnFallback('claude')).toBe(false)
  })
})

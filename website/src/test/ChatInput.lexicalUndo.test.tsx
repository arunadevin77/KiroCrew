import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, screen, fireEvent, waitFor } from '@testing-library/react'
import { $getRoot, CONTROLLED_TEXT_INSERTION_COMMAND, type LexicalEditor } from 'lexical'
import { useState } from 'react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

// The Lexical composer sits behind ChatInput's React.lazy boundary
// (components/chat-input/engine.tsx). A cold chunk import on a loaded CI
// shard can outlast findBy*'s 1 s default, so every wait for the composer
// to mount names this timeout.
const LAZY_COMPOSER_MOUNT = { timeout: 5000 }

/**
 * Undo in the Lexical composer after a programmatic value change.
 *
 * The composer is controlled: every value the parent pushes in (an optimize
 * result, ↑/↓ recall) replaces the editor content and clears Lexical's own
 * history. The textarea path keeps an explicit snapshot history for exactly
 * that reason, and the Lexical path must use the same one. Otherwise, with the
 * Style Markdown While Typing setting on, Ctrl/Cmd+Z after Optimize could not
 * bring the original draft back.
 */

const ORIGINAL = 'my prompt'
const OPTIMIZED = 'a much better prompt'

function Host() {
  const [value, setValue] = useState(ORIGINAL)
  return (
    <>
      <ChatInput value={value} onChange={setValue} onSend={vi.fn()} connected={true} lexicalComposer />
      <output data-testid="value">{value}</output>
    </>
  )
}

const shown = () => screen.getByTestId('value').textContent

/** A loaded machine makes React's scheduler yield between committing a
 *  render and running that render's passive effects. Advancing the clock the
 *  scheduler reads on every call forces that yield on every run, so anything
 *  a passive effect records is still missing when its commit is on screen. */
function yieldingSchedulerClock() {
  const realNow = performance.now.bind(performance)
  let skew = 0
  return vi.spyOn(performance, 'now').mockImplementation(() => realNow() + (skew += 50))
}

/** Outside act, as in the browser: React renders through its scheduler
 *  rather than flushing each update synchronously. */
function leaveActEnvironment() {
  const scope = globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }
  const previous = scope.IS_REACT_ACT_ENVIRONMENT
  scope.IS_REACT_ACT_ENVIRONMENT = false
  return { restore: () => { scope.IS_REACT_ACT_ENVIRONMENT = previous } }
}

describe('ChatInput Lexical composer: undo uses the snapshot history', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (typeof url === 'string' && url.includes('/api/optimizer/optimize')) {
        return Promise.resolve({ ok: true, json: async () => ({ changed: true, optimized: OPTIMIZED }) })
      }
      return Promise.resolve({ ok: true, json: async () => [] })
    }))
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('Ctrl+Z after Optimize restores the original draft, and Ctrl+Shift+Z redoes it', async () => {
    renderWithProviders(<Host />)
    const input = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    expect(input).toHaveAttribute('data-lexical-composer')
    fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' }))
    await waitFor(() => expect(shown()).toBe(OPTIMIZED))

    fireEvent.keyDown(input, { key: 'z', code: 'KeyZ', ctrlKey: true })
    await waitFor(() => expect(shown()).toBe(ORIGINAL))

    fireEvent.keyDown(input, { key: 'z', code: 'KeyZ', ctrlKey: true, shiftKey: true })
    await waitFor(() => expect(shown()).toBe(OPTIMIZED))
  })

  it('Ctrl+Z after typing into the Lexical composer undoes the typed text through the same history', async () => {
    renderWithProviders(<Host />)
    const input = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    const editor = (input as unknown as { __lexicalEditor: LexicalEditor }).__lexicalEditor
    act(() => { editor.update(() => $getRoot().selectEnd(), { discrete: true }) })
    // One bulk insert is its own undo boundary in the snapshot history.
    act(() => { editor.dispatchCommand(CONTROLLED_TEXT_INSERTION_COMMAND, ' plus a long typed tail') })
    await waitFor(() => expect(shown()).toBe(`${ORIGINAL} plus a long typed tail`))

    fireEvent.keyDown(input, { key: 'z', code: 'KeyZ', ctrlKey: true })
    await waitFor(() => expect(shown()).toBe(ORIGINAL))

    fireEvent.keyDown(input, { key: 'z', code: 'KeyZ', ctrlKey: true, shiftKey: true })
    await waitFor(() => expect(shown()).toBe(`${ORIGINAL} plus a long typed tail`))
  })

  it('a Ctrl+Z pressed the moment the optimize rewrite is on screen already finds the optimize step', async () => {
    renderWithProviders(<Host />)
    const input = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    const clock = yieldingSchedulerClock()
    const actEnvironment = leaveActEnvironment()
    let pressed = false
    // Press Ctrl+Z from the first DOM mutation that shows the rewrite with no
    // optimize overlay: the earliest moment undo is offered again.
    const observer = new MutationObserver(() => {
      if (pressed || shown() !== OPTIMIZED || screen.queryByText(/Optimizing prompt/)) return
      pressed = true
      fireEvent.keyDown(input, { key: 'z', code: 'KeyZ', ctrlKey: true })
    })
    observer.observe(document.body, { childList: true, characterData: true, subtree: true })
    try {
      fireEvent.click(screen.getByRole('button', { name: 'Optimize prompt' }))
      await waitFor(() => expect(pressed).toBe(true))
      await waitFor(() => expect(shown()).toBe(ORIGINAL))
    } finally {
      observer.disconnect()
      clock.mockRestore()
      actEnvironment.restore()
    }
  })

  it('a Ctrl+Z pressed the moment the typed text is on screen already finds its history entry', async () => {
    renderWithProviders(<Host />)
    const input = await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    const editor = (input as unknown as { __lexicalEditor: LexicalEditor }).__lexicalEditor
    act(() => { editor.update(() => $getRoot().selectEnd(), { discrete: true }) })
    const typed = `${ORIGINAL} plus a long typed tail`
    const clock = yieldingSchedulerClock()
    const actEnvironment = leaveActEnvironment()
    // Press Ctrl+Z from the first DOM mutation that shows the typed text:
    // the earliest moment a user could see it and undo it.
    let pressed = false
    const observer = new MutationObserver(() => {
      if (pressed || shown() !== typed) return
      pressed = true
      fireEvent.keyDown(input, { key: 'z', code: 'KeyZ', ctrlKey: true })
    })
    observer.observe(screen.getByTestId('value'), { childList: true, characterData: true, subtree: true })
    try {
      editor.dispatchCommand(CONTROLLED_TEXT_INSERTION_COMMAND, ' plus a long typed tail')
      await waitFor(() => expect(pressed).toBe(true))
      await waitFor(() => expect(shown()).toBe(ORIGINAL))
    } finally {
      observer.disconnect()
      clock.mockRestore()
      actEnvironment.restore()
    }
  })
})

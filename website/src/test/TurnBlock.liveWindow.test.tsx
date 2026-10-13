import { describe, it, expect, vi, beforeEach } from 'vitest'
import { forwardRef, type ReactNode } from 'react'
import { render, screen, fireEvent } from '@testing-library/react'
import type { DisplayItem, TurnItem } from '../pages/chat/types'

// Replace motion.div with a plain div that records the motion props it got,
// and drive the reduced-motion signal, so "which target, which transition" is
// observable without a real browser.
const motion = vi.hoisted(() => ({ reduce: false }))
vi.mock('framer-motion', async (orig) => {
  const actual = await orig<typeof import('framer-motion')>()
  type P = { initial?: unknown; animate?: unknown; exit?: unknown; transition?: unknown; children?: ReactNode } & Record<string, unknown>
  const div = forwardRef<HTMLDivElement, P>(({ initial, animate, exit: _exit, transition, children, ...rest }, ref) => (
    <div ref={ref} data-initial={JSON.stringify(initial ?? null)} data-animate={JSON.stringify(animate ?? null)}
      data-transition={JSON.stringify(transition ?? null)} {...rest}>{children}</div>
  ))
  return { ...actual, motion: { ...actual.motion, div }, useReducedMotion: () => motion.reduce }
})
import TurnBlock from '../pages/chat/TurnBlock'
import SearchHighlightContext from '../hooks/SearchHighlightContext'

const tool = (n: number): TurnItem => ({ kind: 'single', msg: { role: 'tool', content: `🔧 Running: step${n}`, ts: `t${n}` }, idx: n })
const hidden = (n: number): TurnItem => ({ kind: 'single', msg: { role: 'tool', content: `✅ step${n}`, ts: `h${n}` }, idx: 200 + n })
const think = (content: string, ts: string, idx: number): TurnItem => ({ kind: 'single', msg: { role: 'thinking', content, ts }, idx })
const done = { kind: 'single', msg: { role: 'assistant', content: 'Done.', ts: 'z' }, idx: 50 } as TurnItem
const turnOf = (items: TurnItem[], complete = false): Extract<DisplayItem, { kind: 'turn' }> => ({ kind: 'turn', items, complete })
const renderItem = (it: TurnItem, i: number) => (
  <div data-testid={`item-${i}`}>{it.kind === 'single' ? it.msg.content : 'group'}</div>
)
const wrap = (i: number) => screen.getByTestId(`item-${i}`).parentElement!
/** A row is folded when its keyed wrapper is the height-0 marked one. */
const isFolded = (i: number) => wrap(i).getAttribute('data-collapsed') === 'true'
const motionOf = (i: number) => ({
  initial: JSON.parse(wrap(i).getAttribute('data-initial')!),
  animate: JSON.parse(wrap(i).getAttribute('data-animate')!),
  transition: JSON.parse(wrap(i).getAttribute('data-transition')!),
})
const tools = (n: number) => Array.from({ length: n }, (_, i) => tool(i))

beforeEach(() => { motion.reduce = false })

describe('TurnBlock — running turn shows only the last 5 actions', () => {
  it('7 actions: 5 open, 2 behind one fold row', () => {
    render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} />)
    const fold = screen.getByRole('button', { name: '2 earlier steps' })
    expect(fold).toHaveAttribute('aria-expanded', 'false')
    expect([0, 1].every(isFolded)).toBe(true)
    expect([2, 3, 4, 5, 6].some(isFolded)).toBe(false)
    // Folded rows are hidden from assistive tech and the tab order.
    expect(wrap(0)).toHaveAttribute('aria-hidden', 'true')
    expect(wrap(0)).toHaveAttribute('inert')
  })

  it('5 actions or fewer: no fold row', () => {
    render(<TurnBlock turn={turnOf(tools(5))} renderItem={renderItem} />)
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('a hidden completion row folds with the older steps but is not counted', () => {
    // tool0, its hidden ✅ row, then tool1..tool6: 7 counted calls.
    const items = [tool(0), hidden(0), ...tools(7).slice(1)]
    render(<TurnBlock turn={turnOf(items)} renderItem={renderItem} />)
    expect(screen.getByRole('button', { name: '2 earlier steps' })).toBeInTheDocument()
    expect([0, 1, 2].every(isFolded)).toBe(true)
    expect([3, 4, 5, 6, 7].some(isFolded)).toBe(false)
  })

  it('the reasoning row never folds, even while it streams', () => {
    const items = [think('plan', 'th', 99), ...tools(7), think('more', 'th2', 100)]
    render(<TurnBlock turn={turnOf(items)} renderItem={renderItem} />)
    // mergeTurnThinking hoists both bursts into row 0.
    expect(isFolded(0)).toBe(false)
    expect(screen.getByRole('button', { name: '2 earlier steps' })).toBeInTheDocument()
    expect([1, 2].every(isFolded)).toBe(true)
  })

  it('a tool row the user expanded is never folded and is not counted', () => {
    const pinned = (it: TurnItem) => it.kind === 'single' && it.msg.ts === 't0'
    render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} isRowPinned={pinned} />)
    expect(isFolded(0)).toBe(false)
    expect(isFolded(1)).toBe(true)
    expect(screen.getByRole('button', { name: '1 earlier step' })).toBeInTheDocument()
  })

  it('never folds answer text', () => {
    const items: TurnItem[] = [
      { kind: 'single', msg: { role: 'assistant', content: 'Here is what I found so far.', ts: 'a' }, idx: 100 },
      ...tools(7),
    ]
    render(<TurnBlock turn={turnOf(items)} renderItem={renderItem} />)
    expect(isFolded(0)).toBe(false)
    expect([1, 2].every(isFolded)).toBe(true)
  })

  it('expanding shows every row and the state holds as new actions arrive', () => {
    const { rerender } = render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} />)
    fireEvent.click(screen.getByRole('button', { name: '2 earlier steps' }))
    const open = screen.getByRole('button', { name: 'Hide earlier steps' })
    expect(open).toHaveAttribute('aria-expanded', 'true')
    expect(tools(7).some((_, i) => isFolded(i))).toBe(false)
    rerender(<TurnBlock turn={turnOf(tools(8))} renderItem={renderItem} />)
    expect(screen.getByRole('button', { name: 'Hide earlier steps' })).toHaveAttribute('aria-expanded', 'true')
    expect(tools(8).some((_, i) => isFolded(i))).toBe(false)
    fireEvent.click(open)
    expect(screen.getByRole('button', { name: '3 earlier steps' })).toBeInTheDocument()
  })

  it('folds by animating height to 0, clipped, and opens back to auto; no entry animation', () => {
    const { rerender } = render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} />)
    expect(motionOf(0).animate).toMatchObject({ height: 0, opacity: 0, overflow: 'hidden' })
    expect(motionOf(6).animate).toMatchObject({ height: 'auto', opacity: 1 })
    expect(motionOf(6).initial).toBe(false)
    expect(motionOf(0).transition).toMatchObject({ type: 'spring', bounce: 0 })
    rerender(<TurnBlock turn={turnOf(tools(8))} renderItem={renderItem} />)
    expect(motionOf(7).initial).toBe(false) // a new row mounts in place, no fade-in
    expect(motionOf(2).animate).toMatchObject({ height: 0 })
  })

  it('reduced motion: the same fold, with an instant transition', () => {
    motion.reduce = true
    render(<TurnBlock turn={turnOf(tools(8))} renderItem={renderItem} />)
    expect(isFolded(2)).toBe(true)
    expect(motionOf(2).transition).toEqual({ duration: 0 })
  })

  it('a row keeps its element across fold and unfold, and an in-place row across the completion flip', () => {
    const note: TurnItem = { kind: 'single', msg: { role: 'error', content: 'a step failed', ts: 'e' }, idx: 90 }
    const base = [note, ...tools(7)]
    const { rerender } = render(<TurnBlock turn={turnOf(base)} renderItem={renderItem} />)
    const tool2 = wrap(3)
    const noteRow = wrap(0)
    rerender(<TurnBlock turn={turnOf([...base, tool(7)])} renderItem={renderItem} />)
    expect(isFolded(3)).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: '3 earlier steps' }))
    expect(wrap(3)).toBe(tool2) // folded and unfolded in place, never remounted
    rerender(<TurnBlock turn={turnOf([...base, tool(7), done], true)} renderItem={renderItem} />)
    expect(wrap(0)).toBe(noteRow) // the settled render reuses the same row element
  })

  it('a host-owned fold state survives a remount', () => {
    const onChange = vi.fn()
    const { unmount } = render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} disclosureKey="k" onDisclosureChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: '2 earlier steps' }))
    expect(onChange).toHaveBeenCalledWith('k:live', true)
    unmount()
    render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} disclosureKey="k" onDisclosureChange={onChange} liveDisclosure />)
    expect(screen.getByRole('button', { name: 'Hide earlier steps' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('a search match in a folded row opens the fold', () => {
    const search = { term: 'step0', caseSensitive: false, currentMessageIdx: 0, currentOccurrenceIdx: 0 }
    render(
      <SearchHighlightContext.Provider value={search}>
        <TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} />
      </SearchHighlightContext.Provider>,
    )
    expect(screen.getByRole('button', { name: 'Hide earlier steps' })).toHaveAttribute('aria-expanded', 'true')
    expect(isFolded(0)).toBe(false)
  })

  it('an opened live fold stays open when the turn finishes', () => {
    const { rerender } = render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} />)
    fireEvent.click(screen.getByRole('button', { name: '2 earlier steps' }))
    rerender(<TurnBlock turn={turnOf([...tools(7), done], true)} renderItem={renderItem} />)
    expect(screen.getByRole('button', { name: 'Hide tool calls' })).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('item-0')).toBeInTheDocument()
  })

  it('an opened live fold is handed to the host at completion, so a remount keeps it open', () => {
    const onChange = vi.fn()
    const props = { renderItem, disclosureKey: 'k', onDisclosureChange: onChange }
    const { rerender, unmount } = render(<TurnBlock turn={turnOf(tools(7))} {...props} liveDisclosure />)
    rerender(<TurnBlock turn={turnOf([...tools(7), done], true)} {...props} liveDisclosure />)
    expect(onChange).toHaveBeenCalledWith('k', true)
    unmount()
    // The host now holds disclosure=true for the turn; a cold remount reads it.
    render(<TurnBlock turn={turnOf([...tools(7), done], true)} {...props} disclosure liveDisclosure />)
    expect(screen.getByRole('button', { name: 'Hide tool calls' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('an unopened live fold settles into the existing collapse', () => {
    const onChange = vi.fn()
    const { rerender } = render(<TurnBlock turn={turnOf(tools(7))} renderItem={renderItem} disclosureKey="k" onDisclosureChange={onChange} />)
    rerender(<TurnBlock turn={turnOf([...tools(7), done], true)} renderItem={renderItem} disclosureKey="k" onDisclosureChange={onChange} />)
    expect(screen.getByRole('button', { name: '7 tool calls' })).toHaveAttribute('aria-expanded', 'false')
    expect(onChange).not.toHaveBeenCalled()
  })

  it('a finished turn keeps the existing collapse, with no live fold', () => {
    render(<TurnBlock turn={turnOf([...tools(7), done], true)} renderItem={renderItem} />)
    expect(screen.getByRole('button', { name: '7 tool calls' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /earlier step/ })).toBeNull()
  })
})

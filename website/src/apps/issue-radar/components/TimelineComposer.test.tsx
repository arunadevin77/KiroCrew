/**
 * TimelineComposer + lib/commentBatch (#19505).
 *
 * Covers: posting one comment now; a threaded reply written INLINE under its
 * comment and carrying its `thread_id`; a quote-reply pre-fill when the comment has
 * none; inline replies and the head box feeding one pending batch that survives a
 * remount; editing a pending reply reopening it inline; submitting the batch; a
 * partial failure keeping only the unposted items; and the read-only gate.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { CommentBatchResponse, TimelineEvent } from '../api'
import {
  anchorKeyOf, loadPending, pendingStorageKey, quoteReply, remainingAfter, resetBatchState,
  type PendingComment,
} from '../lib/commentBatch'

const postComments = vi.hoisted(() => vi.fn())
const ctx = vi.hoisted(() => ({
  active: { owner: 'zzq', repo: 'fabric', provider: 'github', host: 'github.com' },
  canWrite: true,
}))

vi.mock('../context', () => ({ useIssueRadar: () => ctx }))
vi.mock('../api', async (importOriginal) => {
  const mod = await importOriginal<typeof import('../api')>()
  return { ...mod, issueRadarApi: { ...mod.issueRadarApi, postComments } }
})

import TimelineComposer from './TimelineComposer'
import CommentBatchProvider from './CommentBatchProvider'
import InlineReply from './InlineReply'
import ReplyButton from './ReplyButton'
import { repoScopeKey } from '../lib/links'

const KEY = () => pendingStorageKey(repoScopeKey(ctx.active as never), 'pull', 7)

function ok(n: number): CommentBatchResponse {
  return {
    owner: 'zzq', repo: 'fabric', number: 7, target: 'pull',
    posted: Array.from({ length: n }, (_, index) => ({ index, id: index + 1, url: null })),
    failed: null,
  }
}

const THREADED: TimelineEvent = { kind: 'review_comment', actor: 'kim', created_at: '2026-10-01T00:00:00Z', body: 'why?', thread_id: '44' }
const FLAT: TimelineEvent = { kind: 'comment', actor: 'lee', created_at: '2026-10-02T00:00:00Z', body: 'how?', thread_id: null }

/** The head box plus two timeline comments, each with its Reply button and inline slot. */
function renderComposer(props: Partial<{ onPosted: () => void }> = {}) {
  const onPosted = props.onPosted ?? vi.fn()
  const events = [THREADED, FLAT]
  const view = render(
    <CommentBatchProvider target="pull" number={7} events={events} onPosted={onPosted}>
      <TimelineComposer />
      {events.map((ev) => (
        <div key={anchorKeyOf(ev)} data-testid={`card-${ev.actor}`}>
          <ReplyButton ev={ev} />
          <InlineReply ev={ev} />
        </div>
      ))}
    </CommentBatchProvider>,
  )
  return { ...view, onPosted }
}

const replyTo = (actor: string) =>
  fireEvent.click(within(screen.getByTestId(`card-${actor}`)).getByRole('button', { name: /reply|quote/i }))
const inlineBox = (actor: string) =>
  within(screen.getByTestId(`card-${actor}`)).getByRole('textbox') as HTMLTextAreaElement

const box = () => within(screen.getByTestId('timeline-composer-box')).getByRole('textbox') as HTMLTextAreaElement
const type = (text: string) => fireEvent.change(box(), { target: { value: text } })
const button = (name: RegExp) => screen.getByRole('button', { name })

beforeEach(() => {
  localStorage.clear()
  resetBatchState()
  postComments.mockReset()
  ctx.canWrite = true
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { cb(0); return 0 })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('commentBatch helpers', () => {
  it('quotes the original under a mention and cuts long bodies', () => {
    const body = ['a', '', 'b', 'c', 'd', 'e', 'f', 'g'].join('\n')
    expect(quoteReply({ author: 'kim', body, thread_id: null }))
      .toBe('@kim\n\n> a\n>\n> b\n> c\n> d\n> e\n> …\n\n')
  })

  it('keeps only the items the server did not post', () => {
    const items = ['a', 'b', 'c'].map((body, i) => ({ id: String(i), body, thread_id: null, reply_to: null }))
    expect(remainingAfter(items, items, { posted: [{ index: 0, id: 1, url: null }] }).map((p) => p.body))
      .toEqual(['b', 'c'])
  })

  it('keeps an item added while the submit was in flight', () => {
    const sent = ['a', 'b'].map((body, i) => ({ id: String(i), body, thread_id: null, reply_to: null }))
    const current = [...sent, { id: 'new', body: 'added meanwhile', thread_id: null, reply_to: null }]
    const result = { posted: [{ index: 0, id: 1, url: null }, { index: 1, id: 2, url: null }] }
    expect(remainingAfter(current, sent, result).map((p) => p.body)).toEqual(['added meanwhile'])
  })

  it('drops corrupt stored entries instead of throwing', () => {
    localStorage.setItem('k', JSON.stringify([{ id: '1', body: 'ok', thread_id: null, reply_to: null }, { nope: 1 }]))
    expect(loadPending('k').map((p) => p.body)).toEqual(['ok'])
    localStorage.setItem('k', '{not json')
    expect(loadPending('k')).toEqual([])
  })
})

describe('TimelineComposer', () => {
  it('posts one comment now and clears the box', async () => {
    postComments.mockResolvedValue(ok(1))
    const { onPosted } = renderComposer()
    type('  looks good  ')
    fireEvent.click(button(/^comment$/i))
    await waitFor(() => expect(onPosted).toHaveBeenCalled())
    expect(postComments).toHaveBeenCalledWith(
      ctx.active, 7, 'pull', [{ body: 'looks good', thread_id: null }],
    )
    expect(box().value).toBe('')
  })

  it('keeps the draft when the post fails', async () => {
    postComments.mockRejectedValue(new Error('locked'))
    renderComposer()
    type('hello')
    fireEvent.click(button(/^comment$/i))
    expect(await screen.findByText('locked')).toBeTruthy()
    expect(box().value).toBe('hello')
  })

  it('opens the reply box inline under the comment and sends it into its thread', async () => {
    postComments.mockResolvedValue(ok(1))
    const { onPosted } = renderComposer()
    expect(screen.queryByTestId('inline-reply')).toBeNull()
    replyTo('kim')
    const reply = inlineBox('kim')
    expect(reply.value).toBe('')
    expect(box().value).toBe('')
    fireEvent.change(reply, { target: { value: 'because' } })
    fireEvent.click(within(screen.getByTestId('card-kim')).getByRole('button', { name: /^reply$/i }))
    await waitFor(() => expect(onPosted).toHaveBeenCalled())
    expect(postComments.mock.calls[0][3]).toEqual([{ body: 'because', thread_id: '44' }])
    // The inline box closes once the reply is on the forge.
    expect(screen.queryByTestId('inline-reply')).toBeNull()
  })

  it('pre-fills a quote inline when the comment cannot be threaded', () => {
    renderComposer()
    replyTo('lee')
    expect(inlineBox('lee').value).toBe('@lee\n\n> how?\n\n')
  })

  it('closes an empty inline box on Escape or Cancel', () => {
    renderComposer()
    replyTo('kim')
    fireEvent.keyDown(inlineBox('kim'), { key: 'Escape' })
    expect(screen.queryByTestId('inline-reply')).toBeNull()
    replyTo('lee')
    fireEvent.click(within(screen.getByTestId('card-lee')).getByRole('button', { name: /cancel/i }))
    expect(screen.queryByTestId('inline-reply')).toBeNull()
  })

  it('batches an inline reply under its comment and reopens it there on Edit', () => {
    renderComposer()
    replyTo('kim')
    fireEvent.change(inlineBox('kim'), { target: { value: 'later' } })
    fireEvent.click(within(screen.getByTestId('card-kim')).getByRole('button', { name: /add to batch/i }))
    const card = screen.getByTestId('card-kim')
    expect(within(card).getByTestId('inline-pending').textContent).toContain('later')
    expect(screen.getByTestId('pending-batch').textContent).toContain('later')
    expect(loadPending(KEY())[0]).toMatchObject({ body: 'later', thread_id: '44', anchor: anchorKeyOf(THREADED) })

    fireEvent.click(within(within(card).getByTestId('inline-pending')).getByRole('button', { name: /edit/i }))
    expect(inlineBox('kim').value).toBe('later')
    expect(screen.queryByTestId('pending-batch')).toBeNull()
  })

  it('keeps a pending comment stored while it is edited, and saves the edit in its place', () => {
    localStorage.setItem(KEY(), JSON.stringify(['a', 'b', 'c'].map((body, i) => ({
      id: String(i), body, thread_id: null, reply_to: null,
    }))))
    const first = renderComposer()
    const rows = () => within(screen.getByTestId('pending-batch'))
    fireEvent.click(rows().getAllByRole('button', { name: /edit/i })[1])
    expect(box().value).toBe('b')
    // Opening the edit did not take it out of the stored batch.
    expect(loadPending(KEY()).map((p) => p.body)).toEqual(['a', 'b', 'c'])

    // A reload mid-edit still has it.
    first.unmount()
    renderComposer()
    expect(screen.getByTestId('pending-batch').textContent).toContain('b')
    fireEvent.click(rows().getAllByRole('button', { name: /edit/i })[1])
    type('b, reworded')
    fireEvent.click(button(/add to batch/i))
    expect(loadPending(KEY()).map((p) => p.body)).toEqual(['a', 'b, reworded', 'c'])
  })

  it('keeps comments batched in two panes open on the same item', () => {
    const paneA = render(
      <CommentBatchProvider target="pull" number={7} events={[]} onPosted={vi.fn()}><TimelineComposer /></CommentBatchProvider>,
    )
    const paneB = render(
      <CommentBatchProvider target="pull" number={7} events={[]} onPosted={vi.fn()}><TimelineComposer /></CommentBatchProvider>,
    )
    const add = (pane: typeof paneA, text: string) => {
      const scope = within(pane.container)
      fireEvent.change(scope.getByRole('textbox'), { target: { value: text } })
      fireEvent.click(scope.getByRole('button', { name: /add to batch/i }))
    }
    add(paneB, 'from the sheet')
    add(paneA, 'from the workspace')
    expect(loadPending(KEY()).map((p) => p.body)).toEqual(['from the sheet', 'from the workspace'])
    // Each pane lists both, so neither can write back a stale copy.
    expect(within(paneB.container).getByTestId('pending-batch').textContent).toContain('from the workspace')
    act(() => {
      fireEvent.click(within(within(paneB.container).getByTestId('pending-batch')).getAllByRole('button', { name: /remove/i })[0])
    })
    expect(loadPending(KEY()).map((p) => p.body)).toEqual(['from the workspace'])
  })

  it('wraps the inline footer rather than clipping its buttons', () => {
    renderComposer()
    replyTo('kim')
    const add = within(screen.getByTestId('card-kim')).getByRole('button', { name: /add to batch/i })
    expect(add.parentElement?.className).toContain('flex-wrap')
  })

  it('holds comments as a batch that survives a remount, then submits them in order', async () => {
    postComments.mockResolvedValue(ok(2))
    const first = renderComposer()
    type('one')
    fireEvent.click(button(/add to batch/i))
    type('two')
    // Once a batch is pending, Ctrl+Enter adds to it rather than posting.
    fireEvent.keyDown(box(), { key: 'Enter', ctrlKey: true })
    expect(postComments).not.toHaveBeenCalled()
    expect(loadPending(KEY()).map((p) => p.body)).toEqual(['one', 'two'])
    first.unmount()

    const { onPosted } = renderComposer()
    expect(screen.getByTestId('pending-batch').textContent).toContain('one')
    fireEvent.click(button(/submit batch/i))
    await waitFor(() => expect(onPosted).toHaveBeenCalled())
    expect(postComments.mock.calls[0][3]).toEqual([
      { body: 'one', thread_id: null }, { body: 'two', thread_id: null },
    ])
    expect(screen.queryByTestId('pending-batch')).toBeNull()
    expect(localStorage.getItem(KEY())).toBeNull()
  })

  it('keeps only the unposted items after a partial failure', async () => {
    const stored: PendingComment[] = ['a', 'b', 'c'].map((body, i) => ({
      id: String(i), body, thread_id: null, reply_to: null,
    }))
    localStorage.setItem(KEY(), JSON.stringify(stored))
    postComments.mockResolvedValue({ ...ok(1), failed: { index: 1, error: 'rate limited' } })
    renderComposer()
    fireEvent.click(button(/submit batch/i))
    expect(await screen.findByText(/rate limited/)).toBeTruthy()
    expect(loadPending(KEY()).map((p) => p.body)).toEqual(['b', 'c'])
  })

  it('discards the batch only on a second click', () => {
    localStorage.setItem(KEY(), JSON.stringify([{ id: '1', body: 'x', thread_id: null, reply_to: null }]))
    renderComposer()
    fireEvent.click(button(/discard all/i))
    expect(screen.getByTestId('pending-batch')).toBeTruthy()
    act(() => { fireEvent.click(button(/click again/i)) })
    expect(screen.queryByTestId('pending-batch')).toBeNull()
  })

  it('reconciles a batch that lands after the pane was left', async () => {
    let resolve!: (r: CommentBatchResponse) => void
    postComments.mockReturnValue(new Promise<CommentBatchResponse>((r) => { resolve = r }))
    localStorage.setItem(KEY(), JSON.stringify(['a', 'b'].map((body, i) => ({
      id: String(i), body, thread_id: null, reply_to: null,
    }))))
    const first = renderComposer()
    fireEvent.click(button(/submit batch/i))
    // The user switches to another item while the request is in flight.
    first.unmount()
    await act(async () => { resolve(ok(2)) })
    expect(localStorage.getItem(KEY())).toBeNull()
    renderComposer()
    expect(screen.queryByTestId('pending-batch')).toBeNull()
  })

  it('does not offer Submit again in a pane reopened while its batch is in flight', async () => {
    let resolve!: (r: CommentBatchResponse) => void
    postComments.mockReturnValue(new Promise<CommentBatchResponse>((r) => { resolve = r }))
    localStorage.setItem(KEY(), JSON.stringify(['a', 'b'].map((body, i) => ({
      id: String(i), body, thread_id: null, reply_to: null,
    }))))
    const first = renderComposer()
    fireEvent.click(button(/submit batch/i))
    first.unmount()
    // Back on the item before the request lands: the batch is shown but locked.
    renderComposer()
    const submit = within(screen.getByTestId('pending-batch')).getAllByRole('button')
      .find((b) => /submit|submitting|posting|publishing/i.test(b.textContent ?? ''))!
    expect((submit as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(submit)
    expect(postComments).toHaveBeenCalledTimes(1)
    await act(async () => { resolve(ok(2)) })
    expect(screen.queryByTestId('pending-batch')).toBeNull()
  })

  it('keeps the unposted comments after a partial failure with storage disabled', async () => {
    vi.stubGlobal('localStorage', {
      getItem: () => { throw new Error('denied') },
      setItem: () => { throw new Error('denied') },
      removeItem: () => { throw new Error('denied') },
      clear: () => {},
    })
    postComments.mockResolvedValue({ ...ok(1), failed: { index: 1, error: 'rate limited' } })
    renderComposer()
    for (const body of ['alpha-1', 'bravo-2', 'charlie-3']) {
      type(body)
      fireEvent.click(button(/add to batch/i))
    }
    fireEvent.click(button(/submit batch/i))
    expect(await screen.findByText(/rate limited/)).toBeTruthy()
    const shown = screen.getByTestId('pending-batch').textContent ?? ''
    expect(shown).not.toContain('alpha-1')
    expect(shown).toContain('bravo-2')
    expect(shown).toContain('charlie-3')
  })

  it('keeps the box read-only while its post is in flight', async () => {
    let resolve!: (r: CommentBatchResponse) => void
    postComments.mockReturnValue(new Promise<CommentBatchResponse>((r) => { resolve = r }))
    renderComposer()
    type('sent')
    fireEvent.click(button(/^comment$/i))
    expect(box().readOnly).toBe(true)
    // A change event while busy is ignored, so success cannot erase unsent text.
    type('sent plus more')
    expect(box().value).toBe('sent')
    await act(async () => { resolve(ok(1)) })
    expect(box().readOnly).toBe(false)
  })

  it('renders nothing on a read-only repo', () => {
    ctx.canWrite = false
    renderComposer()
    expect(screen.queryByTestId('timeline-composer')).toBeNull()
  })
})

import { describe, it, expect } from 'vitest'
import reducer, {
  appendMessage, endLocalTurn, refreshSlot, selectContinuable, setActiveSlot,
  sseChatMessage, startLocalTurn, syncSlotRunningFromServer,
} from '../store/chatSlice'
import type { ChatMessage } from '../pages/chat/types'
import './mockApiClient'

/**
 * A send made WHILE a refresh fetch is in flight. The refresh fires on socket
 * reconnect and on `chat_done`; the page it fetches is read BEFORE the send,
 * so it carries neither the user's new row nor the turn that row opened.
 *
 * `refreshSlot.fulfilled` rebuilds `messages` from the page and keeps only the
 * head sitting ABOVE the page's first row (`olderHeadAbovePage`). The live send
 * is NEWER than the page, so that cut never keeps it -- the row is dropped from
 * the view until the next refresh. The same stale page carries `running: false`,
 * which idled the slot (`slotRunning`, `pendingTurnSlot`) under the turn the send
 * opened; "user row last, slot idle" is what `selectContinuable` offers Resume
 * on, so the composer swapped its placeholder for the interrupted-turn one until
 * the first chunk. The existing `liveFrameSeq` guard protects only `slotState`,
 * never `slotRunning` / `pendingTurnSlot`.
 *
 * This mirrors `switchSlot.fulfilled` (fix #18658 for the deep-link open): keep
 * the sends past the dispatch-time baseline (`liveFromAtDispatch`), do not double
 * one the page caught, and skip the page's idle run verdict when such rows exist.
 */

const SLOT = 's1'
const init = () => reducer(undefined, { type: '@@INIT' })

const user = (content: string, meta: Record<string, unknown>, ts = '2026-10-09T13:40:00.000Z'): ChatMessage =>
  ({ role: 'user', content, cls: '', ts, meta })

const assistant = (content: string, mid: string, ts: string): ChatMessage =>
  ({ role: 'assistant', content, cls: '', ts, meta: { mid } })

/** Activate the slot and seat whatever history was already on screen. */
function openSlot(messages: ChatMessage[] = []): ReturnType<typeof init> {
  let s = reducer(init(), setActiveSlot(SLOT))
  if (messages.length) s = { ...s, messages }
  return s
}

/** The refresh page lands. `liveFromAtDispatch` is the row count when the fetch
 *  went out -- captured in the thunk before any live send. `running: false` by
 *  default: the stale idle page the bug is about. */
function pageLands(
  state: ReturnType<typeof init>,
  messages: ChatMessage[],
  liveFromAtDispatch: number,
  extra: Record<string, unknown> = {},
) {
  return reducer(state, {
    type: refreshSlot.fulfilled.type,
    meta: { arg: SLOT, requestId: 'rr', requestStatus: 'fulfilled' },
    payload: {
      key: SLOT, messages, running: false, hasMore: false, queue: [],
      nextBefore: null, total: messages.length, stopping: false,
      refreshSeq: 1, liveFrameSeqAtDispatch: 0, liveFromAtDispatch, ...extra,
    },
  })
}

/** `selectContinuable` reads the dashboard's slot list; this slot has none. */
const continuable = (chat: ReturnType<typeof init>) =>
  selectContinuable({ chat, dashboard: { slots: [] } } as never)

describe('refreshSlot.fulfilled keeps a send that landed while the refresh fetch was in flight', () => {
  it('keeps the still-optimistic bubble when the page predates the send', () => {
    // The slot already shows a completed turn; the refresh fetch goes out with
    // one row on screen, then the user sends.
    let s = openSlot([assistant('earlier reply', 'm-0', '2026-10-09T13:00:00.000Z')])
    const liveFrom = s.messages.length // 1 -- captured at dispatch
    s = reducer(s, appendMessage(user('Explain the next step for this project.', { sendId: 's-1' })))
    expect(s.messages).toHaveLength(2)

    // The page, read before the send, carries only the earlier reply.
    s = pageLands(s, [assistant('earlier reply', 'm-0', '2026-10-09T13:00:00.000Z')], liveFrom)
    expect(s.messages.map(m => [m.role, m.content])).toEqual([
      ['assistant', 'earlier reply'],
      ['user', 'Explain the next step for this project.'],
    ])
    expect(s.messages[1].meta?.sendId).toBe('s-1')
  })

  it('keeps the bubble after its echo gave it the server mid', () => {
    let s = openSlot()
    const liveFrom = s.messages.length // 0
    s = reducer(s, appendMessage(user('hello', { sendId: 's-2' })))
    // The server echo lands first (it is fast); the stale page lands after.
    s = reducer(s, sseChatMessage({
      slot: SLOT, role: 'user', content: 'hello', ts: '2026-10-09T13:40:01.000Z',
      meta: { sendId: 's-2', mid: 'm-server', human: true },
    }))
    expect(s.messages).toHaveLength(1)
    expect(s.messages[0].meta?.mid).toBe('m-server')

    s = pageLands(s, [], liveFrom)
    expect(s.messages.map(m => m.content)).toEqual(['hello'])
    expect(s.messages[0].meta?.mid).toBe('m-server')
  })

  it('does not double a send the page already caught', () => {
    let s = openSlot()
    const liveFrom = s.messages.length
    s = reducer(s, appendMessage(user('hello', { sendId: 's-3' })))
    // The page was read after the send persisted: it carries the row by mid.
    s = pageLands(s, [user('hello', { sendId: 's-3', mid: 'm-3', human: true }, '2026-10-09T13:40:01.000Z')], liveFrom)
    expect(s.messages.map(m => m.content)).toEqual(['hello'])
    expect(s.messages[0].meta?.mid).toBe('m-3')
  })

  it('leaves a row already present at dispatch out of the rescue', () => {
    // A send that was on screen BEFORE the fetch went out is part of the page's
    // own history, not a live send: a page that no longer carries it (a server
    // truncation) must not have it re-added by this rescue.
    const prior = [user('old send', { sendId: 's-old', mid: 'm-old' }, '2026-10-09T13:00:00.000Z')]
    let s = openSlot(prior)
    const liveFrom = s.messages.length // 1 -- the old send predates the fetch
    s = pageLands(s, [], liveFrom)
    expect(s.messages.filter(m => m.content === 'old send')).toHaveLength(0)
  })
})

describe('refreshSlot.fulfilled leaves the run state of a send that landed while the refresh fetch was in flight', () => {
  it('keeps the slot running when an idle page predates the send', () => {
    let s = openSlot()
    const liveFrom = s.messages.length
    s = reducer(s, appendMessage(user('Explain the next step for this project.', { sendId: 's-10' })))
    s = reducer(s, startLocalTurn(SLOT))
    expect(s.slotRunning).toBe(true)
    expect(s.pendingTurnSlot).toBe(SLOT)
    expect(continuable(s)).toBe(false)

    s = pageLands(s, [], liveFrom)
    expect(s.messages.map(m => m.content)).toEqual(['Explain the next step for this project.'])
    expect(s.slotRunning).toBe(true)
    expect(s.pendingTurnSlot).toBe(SLOT)
    // No Resume offer: the slot is still busy with the turn just sent.
    expect(continuable(s)).toBe(false)
  })

  it('keeps the slot running after a live slots frame confirmed the turn before the page landed', () => {
    let s = openSlot()
    const liveFrom = s.messages.length
    s = reducer(s, appendMessage(user('hello', { sendId: 's-11' })))
    s = reducer(s, startLocalTurn(SLOT))
    s = reducer(s, syncSlotRunningFromServer({ slot: SLOT, running: true, stopping: false }))
    expect(s.slotRunning).toBe(true)
    expect(s.pendingTurnSlot).toBeNull()

    s = pageLands(s, [], liveFrom)
    expect(s.slotRunning).toBe(true)
    expect(s.slotStopping).toBe(false)
    expect(continuable(s)).toBe(false)
  })

  it('keeps the chunk floor a reply raised during the fetch', () => {
    let s = openSlot()
    const liveFrom = s.messages.length
    s = reducer(s, appendMessage(user('ping', { sendId: 's-12' })))
    s = reducer(s, startLocalTurn(SLOT))
    s = reducer(s, sseChatMessage({ slot: SLOT, role: 'chunk', content: 'pong', seq: 3 }))
    expect(s.lastChunkSeq).toBe(3)
    expect(s.slotState).toBe('streaming')

    s = pageLands(s, [], liveFrom)
    expect(s.lastChunkSeq).toBe(3)
    expect(s.slotState).toBe('streaming')
    expect(s.slotRunning).toBe(true)
  })

  it('does not revive a turn the send itself failed to open', () => {
    let s = openSlot()
    const liveFrom = s.messages.length
    s = reducer(s, appendMessage(user('hello', { sendId: 's-13' })))
    s = reducer(s, startLocalTurn(SLOT))
    // The POST came back refused before the page landed: nothing is running.
    s = reducer(s, endLocalTurn(SLOT))
    expect(s.slotRunning).toBe(false)
    expect(s.pendingTurnSlot).toBeNull()

    s = pageLands(s, [], liveFrom)
    // Kept, not forced: the local verdict (idle) stands, the page adds nothing.
    expect(s.slotRunning).toBe(false)
    expect(s.pendingTurnSlot).toBeNull()
  })

  it('takes the run verdict of a page that caught the send', () => {
    let s = openSlot()
    const liveFrom = s.messages.length
    s = reducer(s, appendMessage(user('hello', { sendId: 's-14' })))
    s = reducer(s, startLocalTurn(SLOT))
    // Read after the send: the page carries the row and the finished reply.
    s = pageLands(s, [
      user('hello', { sendId: 's-14', mid: 'm-14', human: true }, '2026-10-09T13:40:01.000Z'),
      assistant('done', 'm-14-a', '2026-10-09T13:40:02.000Z'),
    ], liveFrom)
    expect(s.messages.map(m => m.content)).toEqual(['hello', 'done'])
    expect(s.slotRunning).toBe(false)
    expect(s.pendingTurnSlot).toBeNull()
    expect(s.slotState).toBe('idle')
  })

  it('takes a running page as before, live send or not', () => {
    let s = openSlot()
    const liveFrom = s.messages.length
    s = reducer(s, appendMessage(user('hello', { sendId: 's-15' })))
    s = reducer(s, startLocalTurn(SLOT))
    s = pageLands(s, [], liveFrom, { running: true, stopping: true })
    // The guard only skips the IDLE verdict of a stale page; a running page
    // lands as before. (`refreshSlot` writes slotRunning/slotStopping/
    // pendingTurnSlot on this path, not slotState -- unlike `switchSlot`.)
    expect(s.slotRunning).toBe(true)
    expect(s.slotStopping).toBe(true)
    expect(s.pendingTurnSlot).toBeNull()
  })
})

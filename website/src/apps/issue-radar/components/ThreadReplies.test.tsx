/**
 * Timeline threads (#19505): grouping replies under their root, the marker each
 * reply leaves at its own time, and the marker revealing a folded reply.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import type { TimelineEvent } from '../api'
import { groupThreads } from '../lib/threads'
import { ReplyMarker, ThreadReplies } from './ThreadReplies'

const c = (actor: string, day: number, thread_id: string | null, body = `${actor} says`): TimelineEvent => ({
  kind: 'comment', actor, created_at: `2026-10-${String(day).padStart(2, '0')}T00:00:00Z`, body, thread_id,
})

beforeEach(() => {
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { cb(0); return 0 })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('groupThreads', () => {
  it('nests replies under the root and leaves a marker at each reply', () => {
    const root = c('kim', 1, 't1')
    const reply1 = c('lee', 5, 't1')
    const reply2 = c('kim', 9, 't1')
    const other = c('max', 7, null)
    const label: TimelineEvent = { kind: 'labeled', actor: 'max', created_at: '2026-10-03T00:00:00Z' }
    // Newest first, the way the detail panes hold it.
    const items = groupThreads([reply2, other, reply1, label, root])
    expect(items.map((it) => it.type)).toEqual(['reply-marker', 'event', 'reply-marker', 'event', 'thread'])
    const thread = items[4]
    expect(thread.type === 'thread' && thread.replies).toEqual([reply1, reply2])
    expect(items[0].type === 'reply-marker' && items[0].root).toBe(root)
  })

  it('leaves a single-comment thread as a plain event', () => {
    // Every GitLab note has a discussion id, so a lone note is a thread of one.
    const items = groupThreads([c('kim', 2, 'solo'), c('lee', 1, null)])
    expect(items.map((it) => it.type)).toEqual(['event', 'event'])
  })
})

describe('ThreadReplies', () => {
  const replies = [2, 3, 4, 5, 6].map((d) => c(`u${d}`, d, 't'))
  const renderReply = (ev: TimelineEvent) => <p>{ev.body}</p>

  it('folds all but the newest replies, and unfolds on demand', () => {
    render(<ThreadReplies replies={replies} renderReply={renderReply} />)
    expect(screen.queryByText('u2 says')).toBeNull()
    expect(screen.getByText('u6 says')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /earlier/i }))
    expect(screen.getByText('u2 says')).toBeTruthy()
  })

  it('reveals a folded reply when its marker is clicked', () => {
    render(
      <>
        <ReplyMarker ev={replies[0]} root={c('kim', 1, 't')} />
        <ThreadReplies replies={replies} renderReply={renderReply} />
      </>,
    )
    expect(screen.getByTestId('reply-marker').textContent).toContain('u2')
    fireEvent.click(screen.getByTestId('reply-marker'))
    expect(screen.getByText('u2 says').parentElement?.className).toContain('ring-accent')
  })
})

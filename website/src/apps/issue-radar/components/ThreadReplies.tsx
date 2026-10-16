// ThreadReplies + ReplyMarker — how a forge thread reads on the timeline.
//
// ThreadReplies draws a thread's replies indented under its root card, oldest
// first. A long thread shows its newest few and folds the rest behind "Show N
// earlier replies". ReplyMarker is the compact row a reply leaves at its own place
// in time (see lib/threads); clicking it reveals the reply in its thread, unfolding
// it if needed, scrolls there and briefly highlights it.

import { useEffect, useRef, useState, type ReactNode } from 'react'
import { ChevronDown, CornerDownRight } from 'lucide-react'

import { i18nT } from '../../../i18n/t'
import type { TimelineEvent } from '../api'
import { REPLY_ATTR, replyDomKey } from '../lib/threads'

const REVEAL_EVENT = 'issue-radar:reveal-reply'
/** Replies shown before the rest fold away. */
const VISIBLE_TAIL = 3
const FLASH = ['ring-2', 'ring-accent', 'rounded-lg']

function flash(el: Element) {
  el.scrollIntoView?.({ block: 'center', behavior: 'smooth' })
  el.classList.add(...FLASH)
  window.setTimeout(() => el.classList.remove(...FLASH), 1600)
}

function findReply(root: ParentNode, key: string): Element | null {
  for (const el of root.querySelectorAll(`[${REPLY_ATTR}]`)) {
    if (el.getAttribute(REPLY_ATTR) === key) return el
  }
  return null
}

export function ThreadReplies({
  replies, renderReply,
}: {
  replies: TimelineEvent[]
  renderReply: (ev: TimelineEvent) => ReactNode
}) {
  const [expanded, setExpanded] = useState(false)
  // The reply a marker asked for; flashed once the unfold has rendered.
  const [reveal, setReveal] = useState<string | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const hidden = expanded ? 0 : Math.max(0, replies.length - VISIBLE_TAIL)

  useEffect(() => {
    const keys = new Set(replies.map(replyDomKey))
    const onReveal = (e: Event) => {
      const key = (e as CustomEvent<string>).detail
      if (!keys.has(key)) return
      setExpanded(true)
      setReveal(key)
    }
    window.addEventListener(REVEAL_EVENT, onReveal)
    return () => window.removeEventListener(REVEAL_EVENT, onReveal)
  }, [replies])

  useEffect(() => {
    if (!reveal) return
    const el = rootRef.current && findReply(rootRef.current, reveal)
    if (el) flash(el)
    setReveal(null)
  }, [reveal, expanded])

  return (
    <div ref={rootRef} className="mt-2 ml-4 space-y-2 border-l-2 border-border pl-3" data-testid="thread-replies">
      {hidden > 0 && (
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="inline-flex items-center gap-1 text-[12px] text-muted hover:text-text focus-ring rounded px-1"
        >
          <ChevronDown size={12} />
          {i18nT('apps.issueRadar.components.threads.show_earlier', { count: hidden })}
        </button>
      )}
      {replies.slice(hidden).map((ev) => (
        <div key={replyDomKey(ev)} data-thread-reply={replyDomKey(ev)} className="transition-shadow">
          {renderReply(ev)}
        </div>
      ))}
    </div>
  )
}

/** First line of a reply, for its marker. */
function firstLine(body: string | undefined): string {
  const line = (body ?? '').trim().split('\n').find((l) => l.trim() && !l.startsWith('>')) ?? ''
  return line.length > 90 ? `${line.slice(0, 89)}…` : line
}

export function ReplyMarker({ ev, root }: { ev: TimelineEvent; root: TimelineEvent }) {
  const label = i18nT('apps.issueRadar.components.threads.replied_in_thread', {
    author: ev.actor ?? 'ghost', root: root.actor ?? 'ghost',
  })
  return (
    <button
      type="button"
      onClick={() => window.dispatchEvent(new CustomEvent(REVEAL_EVENT, { detail: replyDomKey(ev) }))}
      title={i18nT('apps.issueRadar.components.threads.jump_to_reply')}
      className="group flex w-full min-w-0 items-center gap-1.5 pt-1 text-left text-[12.5px] text-muted hover:text-text focus-ring rounded"
      data-testid="reply-marker"
    >
      <CornerDownRight size={12} className="shrink-0" />
      <span className="min-w-0 truncate">{label}</span>
      {firstLine(ev.body) && (
        <span className="min-w-0 truncate opacity-80 group-hover:opacity-100">: {firstLine(ev.body)}</span>
      )}
    </button>
  )
}

// Timeline comments: the pending batch and the reply helpers behind TimelineComposer.
//
// A maintainer can post a comment straight away, or hold several comments and
// replies as a PENDING batch and submit them together, the way a forge's own
// "Start a review" works. The batch lives in localStorage, one key per item, so a
// reload or a pane switch does not throw away prose that has not been sent yet.
//
// A reply goes INTO a thread when the comment carries a `thread_id` (GitHub inline
// review comments, every GitLab note, Azure DevOps PR threads). Without one, for
// example a GitHub conversation comment, the reply is a new comment that quotes
// the original, which reads the same on every provider.

import type { CommentBatchItem, CommentBatchResponse, TimelineEvent } from '../api'

/** Matches the backend's `MAX_BATCH_ITEMS`. */
export const MAX_PENDING = 25

export type CommentTarget = 'issue' | 'pull'

/** What a Reply button hands the composer. */
export interface ReplyTarget {
  author: string | null
  body: string
  thread_id: string | null
}

/** One held comment. `id` is local, only for React keys and removal. */
export interface PendingComment extends CommentBatchItem {
  id: string
  /** Who the reply answers, shown on the pending row. Null for a top-level comment. */
  reply_to: string | null
  /** The timeline comment this answers (see `anchorKeyOf`), so the pending reply
   * shows under it and Edit reopens its inline box there. Null for a top-level
   * comment; absent on batches stored before inline replies. */
  anchor?: string | null
}

/** A stable key for one timeline comment. Events carry no comment id, so the key
 * is what identifies the comment on the page: its kind, author, time and thread. */
export function anchorKeyOf(ev: TimelineEvent): string {
  return [ev.kind, ev.actor ?? '', ev.created_at, ev.thread_id ?? ''].join('|')
}

export function replyTargetOf(ev: TimelineEvent): ReplyTarget {
  return { author: ev.actor, body: ev.body ?? '', thread_id: ev.thread_id ?? null }
}

const PARAGRAPH_BREAK = '\n\n'

/** The text a quote-reply starts with: a mention, then the original as a block
 * quote. Long comments are cut, because a reply quotes the point it answers, not the
 * whole essay. */
export function quoteReply(target: ReplyTarget, maxLines = 6): string {
  const lines = target.body.trim().split('\n')
  const kept = lines.slice(0, maxLines)
  if (lines.length > maxLines) kept.push('…')
  const quoted = kept.map((line) => (line ? `> ${line}` : '>')).join('\n')
  // Markdown structure, not prose: the mention and the quote are separate paragraphs,
  // and the trailing blank line leaves the cursor below the quote.
  const parts = target.author ? [`@${target.author}`, quoted] : [quoted]
  return [...parts, ''].join(PARAGRAPH_BREAK)
}

export function pendingStorageKey(
  scopeKey: string, target: CommentTarget, number: number,
): string {
  return `issue-radar:pending-comments:${scopeKey}:${target}:${number}`
}

function isPending(value: unknown): value is PendingComment {
  if (!value || typeof value !== 'object') return false
  const v = value as Record<string, unknown>
  return typeof v.id === 'string'
    && typeof v.body === 'string'
    && (v.thread_id === null || typeof v.thread_id === 'string')
    && (v.reply_to === null || typeof v.reply_to === 'string')
    && (v.anchor === undefined || v.anchor === null || typeof v.anchor === 'string')
}

/** The stored batch, or `[]`. A corrupt or foreign entry is dropped rather than
 * thrown: the composer must still open. */
export function loadPending(key: string): PendingComment[] {
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return []
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter(isPending).slice(0, MAX_PENDING) : []
  } catch {
    return []
  }
}

export function savePending(key: string, items: PendingComment[]): void {
  try {
    if (items.length) localStorage.setItem(key, JSON.stringify(items))
    else localStorage.removeItem(key)
  } catch {
    // Storage full or disabled: the batch still works for this page's lifetime.
  }
}

// The page's own copy of every item's batch, and the items whose batch submit is
// still in flight. Both outlive a pane's mount, because the user can leave an item
// and come back before its submit lands. The copy is what makes a batch hold when
// storage is disabled or full: storage then reads back empty, and reconciling
// against that would drop the comments the forge did not take.
const latestBatches = new Map<string, PendingComment[]>()
const submitting = new Set<string>()

/** The item's batch: this page's copy when it has one, else the stored batch. */
export function readBatch(key: string): PendingComment[] {
  return latestBatches.get(key) ?? loadPending(key)
}

/** Record the item's batch in this page's copy and in storage. */
export function writeBatch(key: string, items: PendingComment[]): void {
  latestBatches.set(key, items)
  savePending(key, items)
}

export function isSubmitting(key: string): boolean {
  return submitting.has(key)
}

export function setSubmitting(key: string, on: boolean): void {
  if (on) submitting.add(key)
  else submitting.delete(key)
}

/** Tests only: forget the page-lifetime state between cases. */
export function resetBatchState(): void {
  latestBatches.clear()
  submitting.clear()
}

let counter = 0
export function newPendingId(): string {
  counter += 1
  return `${Date.now().toString(36)}-${counter}`
}

/** The request items for a batch, in order. */
export function toBatchItems(items: PendingComment[]): CommentBatchItem[] {
  return items.map(({ body, thread_id }) => ({ body, thread_id }))
}

/** The pending comments still to send after a submit of `sent`: everything in
 * `current` except the sent items the server reported as posted. Posted ones must
 * never be resent, because they are already on the forge. Matching by id rather
 * than by position keeps anything added to `current` while the submit was in
 * flight. */
export function remainingAfter(
  current: PendingComment[],
  sent: PendingComment[],
  result: Pick<CommentBatchResponse, 'posted'>,
): PendingComment[] {
  const posted = new Set(result.posted.map((p) => sent[p.index]?.id).filter(Boolean))
  return current.filter((item) => !posted.has(item.id))
}

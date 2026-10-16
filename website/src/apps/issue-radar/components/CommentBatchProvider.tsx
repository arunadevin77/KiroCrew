// CommentBatchProvider — the shared state behind the timeline's comment boxes.
//
// One issue or PR timeline can have several boxes open at once: the box at its
// head for a new comment, and an inline box under every comment being replied
// to. They all feed ONE pending batch, so this provider owns the drafts, the
// batch (persisted per item, see lib/commentBatch) and the forge calls, and the
// boxes are thin views over it.
//
// Mount it with `key` set to the item so switching items starts from that item's
// stored batch and drops the drafts of the item the pane just left.

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

import { i18nT } from '../../../i18n/t'
import { issueRadarApi, type TimelineEvent } from '../api'
import { useIssueRadar } from '../context'
import { isGitlab, repoScopeKey } from '../lib/links'
import {
  MAX_PENDING, anchorKeyOf, isSubmitting, newPendingId, pendingStorageKey, quoteReply,
  readBatch, remainingAfter, replyTargetOf, setSubmitting, toBatchItems, writeBatch,
  type CommentTarget, type PendingComment,
} from '../lib/commentBatch'

/** The draft key of the box at the head of the timeline. */
export const TOP = '__top__'

export interface Draft {
  text: string
  /** The thread the reply goes into, when the forge has one for this comment. */
  thread_id: string | null
  /** Who it answers, for the pending row. Null for a new top-level comment. */
  reply_to: string | null
  /** Bumped whenever the box should take focus again (a second Reply click). */
  focusSeq: number
  busy: boolean
  error: string | null
  /** The pending comment this box is editing. It stays in the stored batch until
   * the edit is added back, posted or the box is closed, so a reload mid-edit
   * keeps the saved text. */
  editing: string | null
}

/** Fired with a storage key whenever that item's batch changed, so every mounted
 * pane showing the item re-reads it. */
const PENDING_UPDATED_EVENT = 'issue-radar:pending-updated'

const EMPTY_TOP: Draft = {
  text: '', thread_id: null, reply_to: null, focusSeq: 0, busy: false, error: null, editing: null,
}

interface CommentBatchValue {
  canWrite: boolean
  nativeReview: boolean
  pending: PendingComment[]
  batchFull: boolean
  batchBusy: boolean
  batchError: string | null
  clearBatchError: () => void
  drafts: Record<string, Draft>
  /** Open (or refocus) the inline reply box under a timeline comment. */
  openReply: (ev: TimelineEvent) => void
  setText: (key: string, text: string) => void
  clearError: (key: string) => void
  /** Close an inline box, or clear the thread off the top box. */
  close: (key: string) => void
  post: (key: string) => Promise<void>
  addToBatch: (key: string) => void
  editPending: (item: PendingComment) => void
  removePending: (id: string) => void
  discardAll: () => void
  submitBatch: () => Promise<void>
}

const Ctx = createContext<CommentBatchValue | null>(null)

export function useCommentBatch(): CommentBatchValue {
  const value = useContext(Ctx)
  if (!value) throw new Error('useCommentBatch must be used inside CommentBatchProvider')
  return value
}

/** Same as `useCommentBatch`, but null outside a provider (cards rendered alone). */
export function useOptionalCommentBatch(): CommentBatchValue | null {
  return useContext(Ctx)
}

export default function CommentBatchProvider({
  target, number, events, onPosted, children,
}: {
  target: CommentTarget
  number: number
  /** The timeline on screen. A pending reply whose comment is no longer in it is
   * edited in the top box instead, so it can never become unreachable. */
  events: readonly TimelineEvent[]
  /** Called after anything reached the forge, so the pane re-reads its timeline. */
  onPosted: () => void
  children: ReactNode
}) {
  const { active, canWrite } = useIssueRadar()
  const storageKey = pendingStorageKey(repoScopeKey(active), target, number)
  const nativeReview = target === 'pull' && isGitlab(active)
  const anchors = useMemo(
    () => new Set(events.filter((ev) => ev.kind === 'comment' || ev.kind === 'review_comment').map(anchorKeyOf)),
    [events],
  )

  // This mount's view of the item's batch. The page's copy (lib/commentBatch) is
  // the authority: the same item can be open in two panes at once (the workspace
  // and the reference sheet), so every change reads the current batch, writes it
  // back and tells the other mounts, instead of saving this mount's snapshot.
  const [batch, setBatch] = useState<PendingComment[]>(() => readBatch(storageKey))
  const [drafts, setDrafts] = useState<Record<string, Draft>>({ [TOP]: EMPTY_TOP })
  // Read from the page, not this mount: a pane reopened while its batch is still
  // being sent must not offer Submit, or the same comments would post twice.
  const [batchBusy, setBatchBusy] = useState(() => isSubmitting(storageKey))
  const [batchError, setBatchError] = useState<string | null>(null)

  useEffect(() => {
    const onUpdate = (e: Event) => {
      if ((e as CustomEvent<string>).detail !== storageKey) return
      setBatch(readBatch(storageKey))
      setBatchBusy(isSubmitting(storageKey))
    }
    window.addEventListener(PENDING_UPDATED_EVENT, onUpdate)
    return () => window.removeEventListener(PENDING_UPDATED_EVENT, onUpdate)
  }, [storageKey])

  const announce = useCallback(() => {
    window.dispatchEvent(new CustomEvent(PENDING_UPDATED_EVENT, { detail: storageKey }))
  }, [storageKey])

  const mutate = useCallback((change: (items: PendingComment[]) => PendingComment[]) => {
    const next = change(readBatch(storageKey))
    writeBatch(storageKey, next)
    setBatch(next)
    announce()
  }, [storageKey, announce])

  // A comment being edited stays stored but is listed in its box, not as a row.
  const editingIds = useMemo(
    () => new Set(Object.values(drafts).map((d) => d.editing).filter((id): id is string => !!id)),
    [drafts],
  )
  const pending = useMemo(() => batch.filter((p) => !editingIds.has(p.id)), [batch, editingIds])

  const patch = useCallback((key: string, change: Partial<Draft>) => {
    setDrafts((all) => (all[key] ? { ...all, [key]: { ...all[key], ...change } } : all))
  }, [])

  /** After a post or an add: the top box empties, an inline box goes away. */
  const finish = useCallback((key: string) => {
    setDrafts((all) => {
      if (key === TOP) return { ...all, [TOP]: { ...EMPTY_TOP, focusSeq: all[TOP].focusSeq } }
      const { [key]: _gone, ...rest } = all
      return rest
    })
  }, [])

  const openReply = useCallback((ev: TimelineEvent) => {
    const key = anchorKeyOf(ev)
    const reply = replyTargetOf(ev)
    setDrafts((all) => {
      const existing = all[key]
      if (existing) return { ...all, [key]: { ...existing, focusSeq: existing.focusSeq + 1 } }
      return {
        ...all,
        [key]: {
          // Without a thread the reply is a new comment quoting this one.
          text: reply.thread_id ? '' : quoteReply(reply),
          thread_id: reply.thread_id,
          reply_to: reply.author ?? 'ghost',
          focusSeq: 1,
          busy: false,
          error: null,
          editing: null,
        },
      }
    })
  }, [])

  const toPending = (key: string, d: Draft): PendingComment => ({
    id: d.editing ?? newPendingId(),
    body: d.text.trim(),
    thread_id: d.thread_id,
    reply_to: d.reply_to,
    anchor: key === TOP ? null : key,
  })

  /** Whether the box's text replaces a stored comment rather than adding one. */
  const replacesStored = (d: Draft) => d.editing !== null && batch.some((p) => p.id === d.editing)

  /** The box's text into the batch: in the edited comment's place, else at the end. */
  const putBack = (items: PendingComment[], key: string, d: Draft): PendingComment[] => {
    const item = toPending(key, d)
    const at = d.editing ? items.findIndex((p) => p.id === d.editing) : -1
    if (at < 0) return [...items, item]
    return [...items.slice(0, at), item, ...items.slice(at + 1)]
  }

  const post = async (key: string) => {
    const d = drafts[key]
    if (!d || d.busy || !d.text.trim()) return
    patch(key, { busy: true, error: null })
    try {
      await issueRadarApi.postComments(active, number, target, [
        { body: d.text.trim(), thread_id: d.thread_id },
      ])
      // An edited pending comment was just posted, so it leaves the batch.
      if (d.editing) mutate((items) => items.filter((p) => p.id !== d.editing))
      finish(key)
      onPosted()
    } catch (e) {
      // The draft stays: a transient failure must not cost the user a paragraph.
      patch(key, { busy: false, error: (e as Error).message })
    }
  }

  const addToBatch = (key: string) => {
    const d = drafts[key]
    if (!d || d.busy || !d.text.trim()) return
    if (!replacesStored(d) && batch.length >= MAX_PENDING) return
    mutate((items) => putBack(items, key, d))
    finish(key)
  }

  const editPending = (item: PendingComment) => {
    const key = item.anchor && anchors.has(item.anchor) ? item.anchor : TOP
    const current = drafts[key]
    // A box whose post is in flight is not replaced: its text is what was sent.
    if (current?.busy) return
    // Whatever is already in that box goes back to the batch, not overwritten.
    if (current?.text.trim()) {
      if (!replacesStored(current) && batch.length >= MAX_PENDING) return
      mutate((items) => putBack(items, key, current))
    }
    // The comment stays stored while it is edited, so a reload or an item switch
    // before the edit is added back keeps it.
    setDrafts((all) => ({
      ...all,
      [key]: {
        text: item.body,
        thread_id: item.thread_id,
        reply_to: item.reply_to,
        focusSeq: (all[key]?.focusSeq ?? 0) + 1,
        busy: false,
        error: null,
        editing: item.id,
      },
    }))
  }

  const submitBatch = async () => {
    if (!pending.length || batchBusy || isSubmitting(storageKey)) return
    const sent = pending
    setSubmitting(storageKey, true)
    setBatchBusy(true)
    setBatchError(null)
    try {
      const result = await issueRadarApi.postComments(active, number, target, toBatchItems(sent))
      // Reconciled into the item's batch, not this component's state: the user may
      // have left the item while the request was in flight, and this mount's state
      // is gone then. Every mount of the item re-reads the batch on the event.
      writeBatch(storageKey, remainingAfter(readBatch(storageKey), sent, result))
      if (result.failed) {
        setBatchError(i18nT('apps.issueRadar.components.timelineComposer.batch_partial', {
          posted: result.posted.length, total: sent.length, error: result.failed.error,
        }))
      }
      if (result.posted.length) onPosted()
    } catch (e) {
      // Nothing posted: the server reports a partial batch as a 200 naming the
      // failed item, and raises only when no item is known to have reached the forge.
      setBatchError((e as Error).message)
    } finally {
      setSubmitting(storageKey, false)
      setBatchBusy(false)
      announce()
    }
  }

  const value: CommentBatchValue = {
    canWrite,
    nativeReview,
    pending,
    // Counts comments being edited too: each one returns to its own slot.
    batchFull: batch.length >= MAX_PENDING,
    batchBusy,
    batchError,
    clearBatchError: () => setBatchError(null),
    drafts,
    openReply,
    // Text typed while a post is in flight would be cleared by its success
    // without having been sent, so the box is read-only until the post settles.
    setText: (key, text) => setDrafts((all) => (
      all[key] && !all[key].busy ? { ...all, [key]: { ...all[key], text } } : all
    )),
    clearError: (key) => patch(key, { error: null }),
    close: (key) => {
      if (key === TOP) patch(TOP, { thread_id: null, reply_to: null })
      else finish(key)
    },
    post,
    addToBatch,
    editPending,
    removePending: (id) => mutate((items) => items.filter((p) => p.id !== id)),
    discardAll: () => mutate(() => []),
    submitBatch,
  }

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

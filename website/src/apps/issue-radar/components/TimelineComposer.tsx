// TimelineComposer — the head of an issue's or a PR's timeline: the box for a new
// comment, and the pending batch with Submit / Discard.
//
// Replies are written INLINE, under the comment they answer (see InlineReply);
// every box feeds the same batch, owned by CommentBatchProvider. Submitting:
// on a GitLab merge request the forge publishes the batch as ONE review (draft
// notes); elsewhere items post in order and stop at the first failure, and
// whatever did not post stays pending so nothing is lost and nothing is sent twice.

import { useState } from 'react'
import { Check, CornerDownRight, Layers, Loader2, Pencil, Trash2, X } from 'lucide-react'

import { Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import { i18nT } from '../../../i18n/t'
import type { PendingComment } from '../lib/commentBatch'
import CommentBox from './CommentBox'
import { TOP, useCommentBatch } from './CommentBatchProvider'

/** First line of a pending comment, for its row. */
export function preview(body: string): string {
  const line = body.trim().split('\n').find((l) => l.trim() && !l.startsWith('>')) ?? body.trim()
  return line.length > 120 ? `${line.slice(0, 119)}…` : line
}

/** One pending comment: who it answers, its first line, Edit and Remove. */
export function PendingRow({ item, showTarget = true }: { item: PendingComment; showTarget?: boolean }) {
  const batch = useCommentBatch()
  return (
    <li className="flex items-center gap-2 text-[12.5px] min-w-0">
      {showTarget && (
        <span className="shrink-0 text-muted">
          {item.reply_to
            ? <><CornerDownRight size={11} className="lucide-inline" /> {item.reply_to}</>
            : i18nT('apps.issueRadar.components.timelineComposer.new_comment')}
        </span>
      )}
      <span className="min-w-0 truncate text-text">{preview(item.body)}</span>
      <span className="ml-auto flex shrink-0 gap-0.5">
        <Btn
          onClick={() => batch.editPending(item)}
          disabled={batch.batchBusy}
          aria-label={i18nT('apps.issueRadar.components.timelineComposer.edit')}
          title={i18nT('apps.issueRadar.components.timelineComposer.edit')}
          className="px-1 py-0"
        >
          <Pencil className="lucide-inline" />
        </Btn>
        <Btn
          onClick={() => batch.removePending(item.id)}
          disabled={batch.batchBusy}
          aria-label={i18nT('apps.issueRadar.components.timelineComposer.remove')}
          title={i18nT('apps.issueRadar.components.timelineComposer.remove')}
          className="px-1 py-0"
        >
          <X className="lucide-inline" />
        </Btn>
      </span>
    </li>
  )
}

export default function TimelineComposer() {
  const batch = useCommentBatch()
  const [confirmDiscard, setConfirmDiscard] = useState(false)

  if (!batch.canWrite) return null
  const { pending } = batch

  const discardAll = () => {
    if (!confirmDiscard) { setConfirmDiscard(true); return }
    batch.discardAll()
    setConfirmDiscard(false)
  }

  return (
    <div className="mb-5 space-y-2" data-testid="timeline-composer">
      <CommentBox draftKey={TOP} />

      {pending.length > 0 && (
        <div className="rounded-lg border border-border bg-card overflow-hidden" data-testid="pending-batch">
          <div className="flex items-center gap-1.5 px-3 pt-2 text-[11px] uppercase tracking-wider text-muted font-medium">
            <Layers size={12} /> {i18nT('apps.issueRadar.components.timelineComposer.pending_heading')}
            <span className="normal-case tracking-normal">· {pending.length}</span>
          </div>
          <ul className="px-3 py-1.5 space-y-1">
            {pending.map((item) => <PendingRow key={item.id} item={item} />)}
          </ul>
          {/* No hand-off: the pending batch above, and the comment draft below. */}
          <ErrorNotice message={batch.batchError ?? undefined} onDismiss={batch.clearBatchError} className="mx-3 mb-2" />
          <div className="flex items-center gap-1.5 px-3 pb-2 flex-wrap">
            <span className="text-[11px] text-muted mr-auto">
              {batch.nativeReview
                ? i18nT('apps.issueRadar.components.timelineComposer.native_hint')
                : i18nT('apps.issueRadar.components.timelineComposer.sequential_hint')}
            </span>
            <Btn onClick={discardAll} disabled={batch.batchBusy}>
              <Trash2 className="lucide-inline" />
              {confirmDiscard
                ? i18nT('apps.issueRadar.components.timelineComposer.confirm_discard')
                : i18nT('apps.issueRadar.components.timelineComposer.discard_all')}
            </Btn>
            <Btn
              primary
              onClick={() => { setConfirmDiscard(false); void batch.submitBatch() }}
              disabled={batch.batchBusy}
            >
              {batch.batchBusy
                ? <Loader2 className="lucide-inline animate-spin" />
                : <Check className="lucide-inline" />}
              {i18nT('apps.issueRadar.components.timelineComposer.submit_batch')}
            </Btn>
          </div>
        </div>
      )}
    </div>
  )
}

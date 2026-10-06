import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { ComposerBox } from './SelectionToolbar'
import { i18nT } from '../i18n/t'

/** Gap between the point and the box, and the minimum margin to the viewport edge. */
const GAP = 12
const EDGE = 8

/**
 * Where a box hung off a point goes: below the point, flipped above it when the
 * viewport runs out, and clamped horizontally so it never leaves the screen.
 * Measured before paint, so a box near an edge never flashes at the unclamped
 * spot. `tick` re-runs it when the box changes size without the point moving.
 */
function useAnchoredPosition(rootRef: React.RefObject<HTMLDivElement | null>, x: number, y: number, tick: unknown) {
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)
  useLayoutEffect(() => {
    const el = rootRef.current
    if (!el) return
    const w = el.offsetWidth
    const h = el.offsetHeight
    const left = Math.max(EDGE, Math.min(x - w / 2, window.innerWidth - w - EDGE))
    const below = y + GAP
    const top = below + h > window.innerHeight - EDGE ? Math.max(EDGE, y - GAP - h) : below
    setPos(prev => (prev && prev.left === left && prev.top === top ? prev : { left, top }))
  }, [rootRef, x, y, tick])
  // Hidden, not absent, for the one measuring pass: the box must lay out to be
  // measured, and must not flash at the unclamped spot.
  return pos ? { left: pos.left, top: pos.top } : { left: 0, top: 0, visibility: 'hidden' as const }
}

/**
 * The composer for a comment on a box drawn over an image. It renders the same
 * box a text selection opens (`ComposerBox`): the input, the labelled "Add
 * comment" button and the close control, so the two ways of commenting look and
 * behave alike. A box has no selected text, so the composer carries no Copy
 * action.
 *
 * It hangs below the point the drag ended at and flips above it when the
 * viewport runs out, clamped horizontally so it never leaves the screen. Typed
 * text is guarded the way the text composer guards it: a press outside closes
 * an empty box but never one holding text, a scroll of `scrollRef` moves the
 * box with the image instead of closing it, and Escape or the close button
 * over typed text asks `confirmDiscard` first. The text is mirrored to the host
 * through `onTextChange` and comes back as `initialText`, so a remount (the tab
 * suspended, a view switch and back) does not lose it.
 */
export function ImageCommentComposer({ x, y, onSubmit, onCancel, scrollRef, initialText = '', onTextChange, confirmDiscard }: {
  /** Viewport coordinates the composer hangs off. */
  x: number
  y: number
  onSubmit: (text: string) => void
  onCancel: () => void
  scrollRef?: React.RefObject<HTMLElement | null>
  /** The draft the host kept for this box, restored on mount. */
  initialText?: string
  /** Every change to the typed text, so the host can keep it and guard it. */
  onTextChange?: (text: string) => void
  /** Asked before Escape / the close button discards typed text; resolve `true`
   *  to discard. Omit to discard without asking. */
  confirmDiscard?: () => Promise<boolean>
}) {
  const [text, setText] = useState(initialText)
  const [growTick, setGrowTick] = useState(0)
  // How far `scrollRef` has scrolled since the box opened, so the box follows
  // the image it names rather than staying fixed over other content.
  const [scrolled, setScrolled] = useState({ dx: 0, dy: 0 })
  const rootRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const hintIdBase = useId()
  const onCancelRef = useRef(onCancel)
  useEffect(() => { onCancelRef.current = onCancel }, [onCancel])
  const textRef = useRef(initialText)
  const onTextChangeRef = useRef(onTextChange)
  useEffect(() => { onTextChangeRef.current = onTextChange }, [onTextChange])
  const changeText = useCallback((next: string) => {
    textRef.current = next
    setText(next)
    onTextChangeRef.current?.(next)
  }, [])

  // Re-placed as the input grows, and as the image scrolls.
  const style = useAnchoredPosition(rootRef, x - scrolled.dx, y - scrolled.dy, growTick)

  // A viewport resize (a window resize, a rotation, the on-screen keyboard)
  // moves the edges the box is clamped to, so it is clamped again, as the
  // text composer re-measures on resize, rather than left off screen.
  useEffect(() => {
    const remeasure = () => setGrowTick(t => t + 1)
    window.addEventListener('resize', remeasure)
    window.visualViewport?.addEventListener('resize', remeasure)
    return () => {
      window.removeEventListener('resize', remeasure)
      window.visualViewport?.removeEventListener('resize', remeasure)
    }
  }, [])

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current || rootRef.current.contains(e.target as Node)) return
      // A typed comment is not discarded by a stray click: the box stays until
      // it is submitted or closed deliberately (Escape, the close button).
      if (textRef.current.trim()) return
      onCancelRef.current()
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [])

  useEffect(() => {
    const target = scrollRef?.current
    if (!target) return
    const left0 = target.scrollLeft
    const top0 = target.scrollTop
    let frame: number | null = null
    const onScroll = () => {
      if (frame != null) return
      frame = requestAnimationFrame(() => {
        frame = null
        setScrolled({ dx: target.scrollLeft - left0, dy: target.scrollTop - top0 })
      })
    }
    target.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      target.removeEventListener('scroll', onScroll)
      if (frame != null) cancelAnimationFrame(frame)
    }
  }, [scrollRef])

  // Escape and the close button: a deliberate close, confirmed over typed text.
  const requestCancel = useCallback(async () => {
    if (textRef.current.trim() && confirmDiscard && !(await confirmDiscard())) {
      inputRef.current?.focus()
      return
    }
    onCancelRef.current()
  }, [confirmDiscard])

  const submit = useCallback((comment: string) => {
    const trimmed = comment.trim()
    if (trimmed) onSubmit(trimmed)
  }, [onSubmit])
  const onGrow = useCallback(() => setGrowTick(t => t + 1), [])

  return (
    <div
      ref={rootRef}
      data-image-comment-composer=""
      className="fixed z-50 pointer-events-auto"
      style={style}
    >
      <ComposerBox
        inputRef={inputRef}
        autoFocus
        actions={[]}
        copiedId={null}
        hintIdBase={hintIdBase}
        onAction={() => {}}
        onSubmit={submit}
        onEscape={requestCancel}
        // Nothing selected to copy: the shortcut stays the textarea's own.
        onCopyShortcut={async () => false}
        onGrow={onGrow}
        text={text}
        onTextChange={changeText}
        copyFailed={false}
        onDismissCopyFailed={() => {}}
        // The comment lands in the pending list synchronously, so there is
        // never a host round trip to wait on or a refusal to report.
        submitting={false}
        submitFailed={false}
        onDismissSubmitFailed={() => {}}
        // Not the text composer's "Comment on the selected text": nothing is selected.
        inputLabel={i18nT('components.commentOverlay.add_a_comment')}
      />
    </div>
  )
}

import { useState, useRef, useCallback, useEffect } from 'react'
import { useAppSelector, type AppDispatch, type RootState } from '../store'
import { selectSlotRunEpoch, requestStop, syncSlotRunningFromServer } from '../store/chatSlice'
import { handleStopPress, isEscalationState } from '../utils/stopDebounce'
import { i18nT } from '../i18n/t'

/**
 * `ChatPane`'s hold on the running turn. `useChatPaneStop` is the Stop press
 * protocol for the pane's slot: the cooperative cancel, then the hard kill, the
 * optimistic `soft_pending` until the server speaks, and the in-pane failure
 * notice. `useChatPaneRunSync` settles the slot idle on an OBSERVED running
 * true->false transition of the server snapshot, for a turn whose end this tab
 * missed. Both reset when the pane is re-pointed at another slot.
 */

type PaneSlot = RootState['dashboard']['slots'][number] | undefined

export function useChatPaneStop({ slotKey, busy, paneSlot, dispatch }: {
  slotKey: string
  busy: boolean
  paneSlot: PaneSlot
  dispatch: AppDispatch
}) {
  const [stopError, setStopError] = useState('')
  // Stop mirrors ChatPage's press protocol (ChatPage.onStop): the first press
  // is the cooperative cancel, a second press while the slot reports
  // `soft_pending` (or a stalled `killing`) escalates to the hard kill, and a
  // double-tap inside the arming window is ignored. Before this the pane sent
  // a bare soft stop on every press and passed the composer no `stopState`,
  // so a pending cancel looked exactly like an un-pressed Stop: nothing said
  // "stopping", nothing warned that the next press discards the queue — the
  // backend escalates on ANY second press — and the button read as dead
  // (#9547). `handleStopPress` is the shared decision; the ref is per pane
  // because the arming window is measured against THIS slot's soft press.
  const softStopAtRef = useRef(0)
  const serverStopState = paneSlot?.stop_state
  // The server's `soft_pending` arrives a WS round-trip after the first
  // press, and that round-trip has no upper bound on a slow link. Until it
  // lands, the snapshot still says `idle`, so a second press would read as a
  // fresh soft press — and the backend escalates ANY second press to a
  // queue-clearing hard kill, silently. Hold our own soft press as an
  // OPTIMISTIC `soft_pending` instead: the composer shows the pending state
  // (pulse + "click again to force stop") the instant the press is made, and
  // `handleStopPress` sees the escalation state for as long as the server
  // has not spoken, so the second press is a deliberate force (or an ignored
  // double-tap inside the arming window) — never a second soft. The flag is
  // released the moment the server reports a stop state of its own, when the
  // turn ends, when the press fails on the wire, or when the pane is
  // re-pointed; from then on the snapshot is authoritative.
  const [optimisticSoftPending, setOptimisticSoftPending] = useState(false)
  useEffect(() => {
    if (!optimisticSoftPending) return
    if (!busy || isEscalationState(serverStopState)) setOptimisticSoftPending(false)
  }, [optimisticSoftPending, busy, serverStopState])
  // Everything the press protocol remembers is about ONE slot: the arming
  // stamp, the optimistic pending state and the failure notice all reset when
  // the pane is re-pointed, and a completion that comes back for the slot the
  // pane USED to show is dropped (`stopSlotRef` below).
  const stopSlotRef = useRef(slotKey)
  useEffect(() => {
    stopSlotRef.current = slotKey
    softStopAtRef.current = 0
    setOptimisticSoftPending(false)
    setStopError('')
  }, [slotKey])
  const paneStopState: typeof serverStopState =
    optimisticSoftPending && !isEscalationState(serverStopState) ? 'soft_pending' : serverStopState
  // A press that fails on the wire must say so: a silently swallowed
  // rejection leaves exactly the dead-looking button this fix removes. The
  // notice clears on the next press, so a retry that succeeds retires it.
  const stop = useCallback((force: boolean) => {
    setStopError('')
    void dispatch(requestStop({ slotId: slotKey, force })).then((res) => {
      // A reply that lands after the pane was re-pointed is about the slot
      // it was sent for, not the one now shown: acting on it here would zero
      // the CURRENT slot's arming stamp and pending state (its next press
      // would read as a fresh soft, which the backend escalates) and show it
      // a notice about a stop it never pressed (GPT round 9).
      if (stopSlotRef.current !== slotKey) return
      const failure = requestStop.fulfilled.match(res) ? res.payload : null
      if (!failure) return
      // A transport failure carries only the browser's own phrase ("Failed to
      // fetch", "NetworkError…", "Load failed"), which means nothing to a
      // reader; say what happened instead. It is also AMBIGUOUS: the request
      // may have reached the backend and armed the cancel with only the reply
      // lost, and the backend escalates ANY second soft press to a
      // queue-clearing hard kill. So the optimistic pending state and the
      // arming stamp stay — the composer keeps saying "click again to force
      // stop", and the retry the notice asks for is a deliberate force, not a
      // second soft the backend may read as one (GPT round 8). Only a
      // DEFINITE refusal (the backend answered, and said no) armed nothing:
      // that retry must read as a fresh press, so the stamp and the pending
      // promise are reset.
      const ambiguous = /fetch|network|load failed/i.test(failure.error)
      if (!ambiguous) {
        softStopAtRef.current = 0
        setOptimisticSoftPending(false)
      }
      setStopError(
        ambiguous
          ? i18nT('components.chatPane.stop_failed_network')
          : i18nT('components.chatPane.stop_failed', { error: failure.error }),
      )
    })
  }, [dispatch, slotKey])
  const onStop = useCallback(() => {
    handleStopPress(
      isEscalationState(paneStopState),
      Date.now(),
      softStopAtRef,
      () => { setOptimisticSoftPending(true); stop(false) },
      () => stop(true),
    )
  }, [stop, paneStopState])

  return { stopError, setStopError, paneStopState, onStop }
}

export function useChatPaneRunSync({ slotKey, paneSlot, dispatch }: {
  slotKey: string
  paneSlot: PaneSlot
  dispatch: AppDispatch
}) {
  // Reconcile this pane's run state from the server's slot snapshot, the way
  // ChatPage does for the active slot. The reducer only takes the idle
  // direction for a background slot; the running direction stays with the
  // live frames.
  //
  // Only on an OBSERVED running true->false transition, never on the value a
  // snapshot happens to hold when the pane mounts: a pane opened mid-turn can
  // hold a snapshot fetched before the turn started (`running: false`) while
  // live frames already mark it busy, and settling on that would idle the
  // composer and finalize an in-flight reply until the next chunk re-promotes
  // it. The `/stop`-reply settlement covers the stuck-pane press; this path
  // exists for the turn that ends while the tab misses its `_done`, and that
  // end is a transition this pane sees.
  const hasPaneSlot = !!paneSlot
  const paneRunning = !!paneSlot?.running
  const paneStopping = !!paneSlot?.stopping
  const paneRunEpoch = useAppSelector((s) => selectSlotRunEpoch(s, slotKey))
  // The observation and the slot it was made on. Reset together, inside this
  // effect, the moment `slotKey` changes: a pane re-pointed A -> B -> A must
  // not carry A's old observation back (it would settle A on a stale snapshot
  // while A's live frames mark it busy), and a separate reset effect would
  // run AFTER this one on mount and erase a fresh observation instead.
  const observedSlotRef = useRef<string | null>(null)
  const sawRunningRef = useRef(false)
  // WHICH turn was observed running: the slot's `runEpoch` as of the last
  // render in which the snapshot said `running`. The idle snapshot answers
  // about that turn only. A newer turn's first live frame bumps the epoch —
  // and it can land in the same render as the lagging idle snapshot, so the
  // epoch current at settle time may already be the new turn's. The reducer
  // compares against the observed one and drops a settlement that would idle
  // (and split the streaming reply of) a turn this effect never saw.
  const observedEpochRef = useRef(0)
  useEffect(() => {
    if (observedSlotRef.current !== slotKey) {
      observedSlotRef.current = slotKey
      sawRunningRef.current = false
      observedEpochRef.current = 0
    }
    if (!hasPaneSlot) return
    if (paneRunning) { sawRunningRef.current = true; observedEpochRef.current = paneRunEpoch; return }
    if (!sawRunningRef.current) return
    sawRunningRef.current = false
    dispatch(syncSlotRunningFromServer({ slot: slotKey, running: false, stopping: paneStopping, epoch: observedEpochRef.current }))
  }, [dispatch, slotKey, hasPaneSlot, paneRunning, paneStopping, paneRunEpoch])
}

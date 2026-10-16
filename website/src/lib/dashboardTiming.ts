// Browser-side step timers for the crewmate Dashboard tab's load path (issue
// #20952). The tab draws in three serial requests, each carrying the whole
// ~180 KB page, and "Drawing the dashboard…" stays until the second returns:
//
//   step1  GET  /api/members/{slug}/dashboard   — the composed page comes down
//   step2  POST /api/sandbox-doc                 — the browser hands it back up
//   step3  frame GET /sandbox-doc/{id}/{token}   — the iframe loads it
//
// The server costs were measured on the gateway (compose+scrub ~12 ms; the
// field read is memoised for an unchanged page). What is NOT yet measured is
// the browser's own timing and the network between the app and a REMOTE
// gateway — which, for an unchanged page, is structurally the three serial
// transfers above. This records those, so the reporter has the numbers that
// decide whether a future round-trip-elimination change is worth it.
//
// It is deliberately NOT a new telemetry channel: it emits `performance.mark`
// / `performance.measure` (so the three steps show up in the browser
// Performance panel and User Timing) plus one `console.debug` summary line,
// the same shape `useRefreshScheduler` uses for its `[refresh]` logs. Nothing
// is sent to the gateway.

/** performance.now() when available, else Date.now() — matches useCrewAvatarState. */
function nowMs(): number {
  return typeof performance !== 'undefined' && typeof performance.now === 'function'
    ? performance.now()
    : Date.now()
}

/** Mark the start of a Dashboard-tab load and return a stepper that times each
 *  of the three steps against that start. One stepper per load (per fresh
 *  step-1 fetch); the step-2 and step-3 timers are called from the hook and the
 *  frame as those requests resolve.
 *
 *  `label` scopes the User Timing entries so two open dashboards do not collide
 *  (the crewmate slug is a natural label). Safe to call in any environment: the
 *  `performance.*` calls are feature-detected and a failure is swallowed, since
 *  a timing probe must never break the page it measures. */
export function startDashboardLoadTiming(label: string): {
  step: (name: 'step1-get' | 'step2-sandbox-post' | 'step3-frame-get', detail?: { bytes?: number }) => void
} {
  const t0 = nowMs()
  const marks: Record<string, number> = {}
  const safe = (fn: () => void) => {
    try {
      fn()
    } catch {
      /* a timing probe must never throw into the page */
    }
  }
  safe(() => performance.mark?.(`mc-dash:${label}:start`))
  return {
    step(name, detail) {
      const dt = nowMs() - (marks[name] ? marks[name] : t0)
      const sinceStart = nowMs() - t0
      marks[name] = nowMs()
      safe(() => {
        performance.mark?.(`mc-dash:${label}:${name}`)
        performance.measure?.(`mc-dash:${label}:${name}`, `mc-dash:${label}:start`)
      })
      // The downloaded-page size for step 1, as a diagnostic suffix. Built HERE,
      // in this diagnostics-only module, so the caller passes only a number and
      // holds no console literal of its own.
      const bytes = detail?.bytes
      const sizeNote = typeof bytes === 'number' ? (bytes ? `html=${bytes}B` : 'no-html') : ''
      // One line per step, like `[refresh]` in useRefreshScheduler. The number
      // that matters for #20952 is `sinceStart`: cumulative time from the load
      // beginning to this step completing, since the three steps are serial.
      // eslint-disable-next-line no-console
      console.debug(
        `[dashboard-timing] ${label} ${name} +${Math.round(dt)}ms (t=${Math.round(sinceStart)}ms)`
        + (sizeNote ? ` ${sizeNote}` : ''),
      )
    },
  }
}

export type DashboardLoadTiming = ReturnType<typeof startDashboardLoadTiming>

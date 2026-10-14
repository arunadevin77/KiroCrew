/**
 * Opt-in native OS toast for "a background chat finished".
 *
 * The caller first checks conversation attention in `notificationEvent.ts`.
 * This helper applies the separate native-toast preference, permission, and
 * away checks. Unlike a chime, a toast persists in the OS notification centre
 * and names WHICH session needs the user, so it stays default-OFF and fires
 * only while the user is away from the window.
 *
 * The preference lives in localStorage rather than gateway config because it is
 * a per-device browser capability — one machine may have OS notifications muted
 * and another not — exactly like the sound settings it sits beside in
 * Settings > Notifications.
 *
 * That storage is PER-ORIGIN, which for an embedded instance pane (a remote
 * crew's dashboard in a cross-origin iframe) means the hub's toggle could
 * never reach it: the pane reads its own origin's localStorage, the Settings
 * panel writes the hub's. So the host relays its value down inside the
 * `mc-host-model` handshake it already posts (`InstancesViewport.buildModelFor`
 * → `EmbeddedHostBridge` → `setHostChatCompleteNotify`), and a pane resolves
 * the effective opt-in from BOTH realms — see `loadChatCompleteNotify`.
 */
import { useSyncExternalStore } from 'react'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import { isWindowAway } from './windowAway'
import { nativeNotificationPermitted } from '../lib/nativeNotify'
import { isEmbeddedPane } from '../lib/embedded'

/** localStorage key holding the opt-in. Absent — or anything but `'1'` —
 *  means off, so a corrupt or half-written value degrades to the default
 *  rather than to a surprise toast. */
export const CHAT_COMPLETE_NOTIFY_KEY = 'mc-notify-chat-complete'

/** The hub's opt-in, relayed down by the latest `mc-host-model`. Tri-state:
 *  `null` is "no opinion" — not embedded, or an older host whose model predates
 *  the field — and the pane then falls back to its own localStorage alone. */
let hostChatCompleteNotify: boolean | null = null

/** Whether the user opted in, from whichever realm governs this frame.
 *
 *  Top-level: this origin's localStorage, exactly as before. Embedded pane, in
 *  order: an explicit pane-local `'1'` still wins (it is a working opt-in and
 *  predates the relay); an explicit `'0'` still vetoes (turning the toggle off
 *  inside a pane must keep working); otherwise the host's relayed value
 *  governs — that is the fix for the hub toggle never reaching a cross-origin
 *  pane's localStorage. Everything else stays default OFF. */
export function loadChatCompleteNotify(): boolean {
  const local = safeGetItem(CHAT_COMPLETE_NOTIFY_KEY)
  if (local === '1') return true
  if (isEmbeddedPane()) {
    if (local === '0') return false
    if (hostChatCompleteNotify !== null) return hostChatCompleteNotify
  }
  return false
}

/**
 * Persist the opt-in and, when enabling, ask for the OS permission.
 *
 * The permission request is the load-bearing half. `useNativeNotification` is
 * the only other place that asks, and only when an unacked FEED notification
 * arrives — so a user who enables this toggle without ever having received a
 * feed note would sit at `Notification.permission === 'default'` and the
 * feature would be a silent no-op. Flipping the toggle IS the user gesture
 * browsers require for the prompt, so ask here.
 */
export function saveChatCompleteNotify(on: boolean): void {
  safeSetItem(CHAT_COMPLETE_NOTIFY_KEY, on ? '1' : '0')
  notifyChatCompleteListeners()
  if (!on || typeof Notification === 'undefined') return
  if (Notification.permission !== 'default') return
  try {
    void Notification.requestPermission()
  } catch {
    /* unsupported platform (a callback-only implementation returns nothing) */
  }
}

// ── Cross-realm relay + reactivity ─────────────────────────────────────────
// localStorage has no change event within a realm, and a module-level read is
// invisible to React, so the writer (Settings) notifies subscribers the same
// way the crew-pin/stable-order preference stores do: the hub's viewport
// subscribes, and a flip re-broadcasts `mc-host-model` to every warm pane —
// a remote crew's gate follows the hub toggle without waiting for an unrelated
// unread/inset change to trigger the next broadcast.

const chatCompleteListeners = new Set<() => void>()

function notifyChatCompleteListeners(): void {
  chatCompleteListeners.forEach(l => l())
}

/** Record the hub opt-in relayed by the latest `mc-host-model`; called by
 *  `EmbeddedHostBridge` for every model from the parent, and with `null` on
 *  unmount so a later top-level mount reads this realm alone. */
export function setHostChatCompleteNotify(v: boolean | null): void {
  if (hostChatCompleteNotify === v) return
  hostChatCompleteNotify = v
  notifyChatCompleteListeners()
}

function subscribeChatCompleteNotify(cb: () => void): () => void {
  chatCompleteListeners.add(cb)
  return () => {
    chatCompleteListeners.delete(cb)
  }
}

/** Reactive read for relaying surfaces: the hub's viewport renders this into
 *  `mc-host-model`, so a Settings flip reaches every pane on the next
 *  broadcast. */
export function useChatCompleteNotify(): boolean {
  return useSyncExternalStore(subscribeChatCompleteNotify, loadChatCompleteNotify, loadChatCompleteNotify)
}

/**
 * Whether a finished turn warrants a native toast.
 *
 * Policy, in the order the checks read: a real turn (slot-bearing) that is not
 * a reconnect catch-up replay — mirroring the chime's two suppressions, since
 * replayed completions must not toast-storm — for a user who opted in, on a
 * platform that can show a toast, while that user is away from the window.
 *
 * The capability checks (`Notification` present, permission granted) live here
 * rather than at the call site so the whole gate is one testable predicate; the
 * caller is left with the construction the platform may still refuse. In an
 * embedded instance pane the permission is denied by design and the parent
 * frame posts on the pane's behalf, so the capability check defers to
 * `nativeNotificationPermitted()` rather than reading the pane's own verdict.
 */
export function shouldNotifyOnChatComplete(opts: {
  slot: string | undefined | null
  reconnecting: boolean
}): boolean {
  if (!opts.slot || opts.reconnecting) return false
  if (!loadChatCompleteNotify()) return false
  if (!nativeNotificationPermitted()) return false
  // The shared "away" predicate (both axes: hidden, and visible-but-unfocused);
  // see windowAway.ts for why one axis is not enough.
  return isWindowAway()
}

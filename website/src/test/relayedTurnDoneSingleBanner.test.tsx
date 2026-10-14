/**
 * One relayed remote turn-done constructs exactly ONE OS notification on the
 * hub, with the hub window away — the exact surface the #20281 reviews blocked
 * on. Before the `clientOnly` marker, the relay handler posted the crew banner
 * (tag `cd-1:kirocrew-chat-done:<slot>`) AND dispatched a bell row whose count
 * increase made `useNativeNotification` post a second, generically-tagged
 * banner (`kirocrew-notif`); the OS collapses only equal tags, so both stayed
 * up. Both surfaces must be mounted here — the cov80 tests render
 * `InstancesViewport` alone and could never see the second constructor.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { createElement } from 'react'
import { renderWithProviders, createTestStore } from './helpers'
import InstancesViewport from '../components/InstancesViewport'
import { useNativeNotification } from '../hooks/useNativeNotification'

vi.mock('../lib/embedded', () => ({ isEmbeddedPane: vi.fn(() => false) }))

vi.mock('../api/client', () => ({
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
  api: {
    listInstances: vi.fn().mockResolvedValue({
      instances: [
        {
          id: 'cd-1',
          name: 'Zzq One',
          ssh_host: 'cd-1-alias',
          remote_port: 7777,
          local_port: 7778,
          ttl: '20h',
          remote_bin: '',
          status: { instance_id: 'cd-1', state: 'connected', local_port: 7778, remote_port: 7777 },
        },
      ],
      warm_set_cap: 5,
    }),
    connectInstance: vi.fn().mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok' }),
    disconnectInstance: vi.fn().mockResolvedValue({}),
    refreshInstanceToken: vi.fn().mockResolvedValue({ state: 'connected', local_port: 7778, token: 'tok2' }),
  },
}))
import { api } from '../api/client'

const ORIGIN = 'http://127.0.0.1:7778'
const TURN_DONE = { type: 'mc-native-notify', v: 1, title: 'Ship blog', body: 'Response ready', tag: 'kirocrew-chat-done:slot-9', silent: false }

const CONSTRUCTED: Array<{ title: string; options: NotificationOptions | undefined }> = []
class RecordingNotification {
  static permission = 'granted'
  static requestPermission = vi.fn()
  constructor(title: string, options?: NotificationOptions) {
    CONSTRUCTED.push({ title, options })
  }
}

function setHidden(hidden: boolean): void {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden })
}

describe('one relayed turn-done builds exactly one OS notification (hub away)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    CONSTRUCTED.length = 0
    vi.stubGlobal('Notification', RecordingNotification)
    setHidden(true)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    delete (document as { hidden?: boolean }).hidden
  })

  function renderHubWithBellWatcher() {
    const store = createTestStore({
      instances: {
        warm: { 'cd-1': { port: 7778, token: 'tok' } },
        activeId: 'cd-1',
        mru: ['cd-1'],
        unread: {},
        ready: {},
        host: null,
      },
    })
    // Both surfaces, as the hub window mounts them: InstancesViewport owns the
    // relay handler, App's useNativeNotification watches the feed.
    function Hub() {
      return createElement(
        'div',
        null,
        createElement(InstancesViewport),
        createElement(BellWatcher),
      )
    }
    function BellWatcher() {
      useNativeNotification('Kiro Crew', '/avatar.png')
      return null
    }
    renderWithProviders(createElement(Hub), { store })
    return store
  }

  it('the relayed banner is the one constructor, and the bell row is clientOnly', async () => {
    const store = renderHubWithBellWatcher()
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())
    await waitFor(() => expect(api.listInstances).toHaveBeenCalled())

    act(() => {
      window.dispatchEvent(new MessageEvent('message', { data: TURN_DONE, origin: ORIGIN }))
    })

    // Exactly one Notification, and it is the relayed one: crew-prefixed
    // title, per-instance tag, and the pane's own silent choice — not the
    // hook's generic 'kirocrew-notif' fallback.
    await waitFor(() => expect(CONSTRUCTED).toHaveLength(1))
    expect(CONSTRUCTED[0].title).toBe('Zzq One: Ship blog')
    expect(CONSTRUCTED[0].options?.tag).toBe('cd-1:kirocrew-chat-done:slot-9')
    expect(CONSTRUCTED[0].options?.body).toBe('Response ready')

    // The bell still records the turn — that is the point of the PR — but as a
    // browser-owned row the toast hook skips.
    const row = store.getState().notifications.items.find(n => n.kind === 'turn')
    expect(row?.title).toBe('Zzq One: Ship blog')
    expect(row?.clientOnly).toBe(true)
  })

  it('a non-turn relayed note still builds exactly one notification and no bell row', async () => {
    const store = renderHubWithBellWatcher()
    await waitFor(() => expect(document.querySelector('iframe')).not.toBeNull())
    await waitFor(() => expect(api.listInstances).toHaveBeenCalled())

    act(() => {
      window.dispatchEvent(new MessageEvent('message', {
        data: { ...TURN_DONE, title: 'Approval required', tag: 'kirocrew-approval', silent: true },
        origin: ORIGIN,
      }))
    })

    await waitFor(() => expect(CONSTRUCTED).toHaveLength(1))
    expect(CONSTRUCTED[0].options?.tag).toBe('cd-1:kirocrew-approval')
    expect(store.getState().notifications.items).toHaveLength(0)
  })
})

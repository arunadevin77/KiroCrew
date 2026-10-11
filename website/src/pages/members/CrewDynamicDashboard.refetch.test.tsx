import { describe, it, expect, vi, beforeEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/**
 * A GAP RECOVERY IS A ROUND TRIP, and the frame under it can be replaced while it is
 * open: the ordinary dashboard read remounts the document when the package changes.
 *
 * Posting a late answer into the document that replaced the asker hands it values
 * computed for a page it is not -- and the reply looks current to that bootstrap, which
 * sees only that the generation differs and accepts it. So the asking window and the
 * package it asked for are captured when the request goes out and compared on delivery.
 */

const memberDashboard = vi.fn()
const memberDashboardRefetch = vi.fn()

vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      memberDashboard: (...a: unknown[]) => memberDashboard(...a),
      memberDashboardRefetch: (...a: unknown[]) => memberDashboardRefetch(...a),
    },
  }
})

vi.mock('../../hooks/websocket/dashboardFields', () => ({
  subscribeDashboardFields: () => () => {},
}))

// The mint state machine owns its own tests, and it is a round trip this one does not
// need: a stable document url is all the frame wants here.
vi.mock('../../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (srcdoc: string | null) => ({
    url: srcdoc ? 'https://localhost/sandbox/doc' : null,
    failed: false,
    pending: false,
    stalled: false,
    retry: () => {},
  }),
}))

import CrewDynamicDashboard from './CrewDynamicDashboard'

/* The two wire types, spelled here the way the component spells them: they are module
   constants rather than exports, and `test_dashboard_frame_subscribe.py` already pins
   the component's own copy against `dashboard_frame`'s. */
const DATA_MESSAGE_TYPE = 'kirocrew-dashboard:data'
const REFETCH_MESSAGE_TYPE = 'kirocrew-dashboard:refetch'

/** The page asking for a whole read, as a message from THIS frame's window.
 *
 *  `source` is assigned after construction: jsdom validates the option against a real
 *  Window and silently drops a stand-in, and the handler's first test is that the
 *  sender is the frame it minted. */
const askForAWholeRead = (frame: HTMLIFrameElement) => {
  const event = new MessageEvent('message', { data: { type: REFETCH_MESSAGE_TYPE, held: 1 } })
  Object.defineProperty(event, 'source', { value: frame.contentWindow })
  window.dispatchEvent(event)
}

/** The read the route answers with. `html` is the crewmate's stored copy and
 *  `rendered_html` is what the gateway composed: the promote effect wants both, and
 *  only the composed one is ever mounted. */
const answer = (dashboard: string) => ({
  slug: 'atlas',
  member: 'Atlas',
  dashboard,
  state: 'ok',
  html: '<p>stored</p>',
  rendered_html: '<p>page</p>',
  manifest: { fields: {} },
  instance_version: 1,
  template: { id: 'project-report', version: 1 },
  read: { fields: { boards: 1 }, version: 1, generation: 'gen-a' },
})

describe('a late refetch reply', () => {
  beforeEach(() => {
    memberDashboard.mockReset()
    memberDashboardRefetch.mockReset()
  })

  it('is not posted into the document that replaced the one that asked', async () => {
    memberDashboard.mockResolvedValue(answer('page-one'))
    // The answer never settles until the test releases it, which is the window the
    // document is replaced in.
    let release: (value: unknown) => void = () => {}
    memberDashboardRefetch.mockReturnValue(
      new Promise((resolve) => {
        release = resolve
      }),
    )
    const { container } = renderWithProviders(
      <CrewDynamicDashboard slug="atlas" member="Atlas" displayName="Atlas" />,
    )
    const frame = await waitFor(() => {
      const found = container.querySelector('iframe')
      if (!found) throw new Error('the frame never mounted')
      return found as HTMLIFrameElement
    })
    const posted: unknown[] = []
    Object.defineProperty(frame, 'contentWindow', {
      configurable: true,
      value: { postMessage: (message: unknown) => posted.push(message) },
    })

    // The page cannot place a patch and asks for the whole read.
    askForAWholeRead(frame)
    await waitFor(() => expect(memberDashboardRefetch).toHaveBeenCalledTimes(1))

    // THE REPLACEMENT: a different window is under the frame by the time the answer
    // arrives, which is what a remount leaves behind.
    const replacement: unknown[] = []
    Object.defineProperty(frame, 'contentWindow', {
      configurable: true,
      value: { postMessage: (message: unknown) => replacement.push(message) },
    })
    release({ read: { fields: { boards: 99 }, version: 7, generation: 'gen-a' } })

    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(replacement).toEqual([])
    expect(posted).toEqual([])
  })

  it('is posted when the same document is still there', async () => {
    memberDashboard.mockResolvedValue(answer('page-one'))
    memberDashboardRefetch.mockResolvedValue({
      read: { fields: { boards: 7 }, version: 7, generation: 'gen-a' },
    })
    const { container } = renderWithProviders(
      <CrewDynamicDashboard slug="atlas" member="Atlas" displayName="Atlas" />,
    )
    const frame = await waitFor(() => {
      const found = container.querySelector('iframe')
      if (!found) throw new Error('the frame never mounted')
      return found as HTMLIFrameElement
    })
    const posted: Array<{ type?: string }> = []
    Object.defineProperty(frame, 'contentWindow', {
      configurable: true,
      value: { postMessage: (message: { type?: string }) => posted.push(message) },
    })
    askForAWholeRead(frame)
    await waitFor(() => expect(posted.map((m) => m.type)).toEqual([DATA_MESSAGE_TYPE]))
  })
})

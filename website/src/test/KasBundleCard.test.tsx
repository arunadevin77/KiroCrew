/**
 * KasBundleCard — Settings > Agent Harness > KAS: run Kiro Crew's own engine.
 *
 * What is pinned:
 * - the switch can only be turned ON once the pinned engine is installed (an "on"
 *   with nothing installed refuses every KAS session), and OFF is always allowed;
 * - the switch writes `agent.kas_bundle` through the ordinary config PATCH;
 * - "Install" starts the gateway's install, and the card shows its progress from
 *   the status the server returns;
 * - an install failure renders the server's remedy text.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { api, type KasBundleStatus } from '../api/client'
import { KAS_BUNDLE_CONFIG_KEY, KasBundleCard } from '../pages/developer/KasBundleCard'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kasBundleStatus: vi.fn(),
      kasBundleInstall: vi.fn(),
      patchConfig: vi.fn().mockResolvedValue({}),
    },
  }
})

// The restart pill reads the schema; its own behaviour is tested with the schema.
vi.mock('../components/settingRef/RestartRequiredBadge', () => ({
  SchemaRestartBadge: ({ configKey }: { configKey: string }) => (
    <span data-testid="restart-badge" data-key={configKey} />
  ),
}))

const kasBundleStatus = vi.mocked(api.kasBundleStatus)
const kasBundleInstall = vi.mocked(api.kasBundleInstall)
const patchConfig = vi.mocked(api.patchConfig)

const BASE: KasBundleStatus = {
  enabled: false,
  pinned_version: '0.66.33',
  runs_version: '0.66.33',
  installed: null,
  installing: false,
  progress: { done: 0, total: 0 },
  error: '',
}

const INSTALLED: KasBundleStatus = {
  ...BASE,
  installed: { version: '0.66.33', node_version: '22.23.3' },
}

function toggle(): HTMLElement {
  return screen.getByRole('switch')
}

describe('KasBundleCard', () => {
  beforeEach(() => {
    kasBundleStatus.mockReset()
    kasBundleInstall.mockReset()
    patchConfig.mockClear()
  })

  it('offers Install and holds the switch off while nothing is installed', async () => {
    kasBundleStatus.mockResolvedValue(BASE)
    renderWithProviders(<KasBundleCard />)
    await screen.findByTestId('kas-bundle-install')
    expect(screen.getByText('KAS 0.66.33 is not installed.')).toBeInTheDocument()
    expect(toggle()).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByTestId('restart-badge')).toHaveAttribute('data-key', KAS_BUNDLE_CONFIG_KEY)
  })

  it('lets the switch be turned on once installed, through the config PATCH', async () => {
    kasBundleStatus.mockResolvedValue(INSTALLED)
    renderWithProviders(<KasBundleCard />)
    await screen.findByText('KAS 0.66.33 installed (Node 22.23.3)')
    expect(screen.queryByTestId('kas-bundle-install')).toBeNull()
    expect(toggle()).not.toHaveAttribute('aria-disabled')
    fireEvent.click(toggle())
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('agent.kas_bundle', true))
  })

  it('always lets an enabled switch be turned off, even with nothing installed', async () => {
    // A broken or removed install must never trap anyone on this route.
    kasBundleStatus.mockResolvedValue({ ...BASE, enabled: true })
    renderWithProviders(<KasBundleCard />)
    await screen.findByTestId('kas-bundle-install')
    expect(toggle()).not.toHaveAttribute('aria-disabled')
    fireEvent.click(toggle())
    await waitFor(() => expect(patchConfig).toHaveBeenCalledWith('agent.kas_bundle', false))
  })

  it('starts the install and shows the progress the server reports', async () => {
    kasBundleStatus.mockResolvedValueOnce(BASE)
    kasBundleInstall.mockResolvedValue({
      ...BASE,
      installing: true,
      progress: { done: 50 * 1024 * 1024, total: 100 * 1024 * 1024 },
    })
    renderWithProviders(<KasBundleCard />)
    fireEvent.click(await screen.findByTestId('kas-bundle-install'))
    await waitFor(() => expect(kasBundleInstall).toHaveBeenCalledTimes(1))
    const live = await screen.findByRole('status')
    expect(live.textContent).toContain('Installing KAS 0.66.33')
    expect(live.textContent).toContain('50')
    expect(toggle()).toHaveAttribute('aria-disabled', 'true')
  })

  it("renders the server's remedy when the last install failed", async () => {
    kasBundleStatus.mockResolvedValue({
      ...BASE,
      error: 'publishes no Node runtime for linux-riscv64',
    })
    renderWithProviders(<KasBundleCard />)
    expect(await screen.findByText('publishes no Node runtime for linux-riscv64')).toBeInTheDocument()
  })
})

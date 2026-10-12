import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import CrewVersionStatus from '../pages/settings/CrewVersionStatus'

vi.mock('../api/client', () => ({
  api: {
    instancesVersions: vi.fn(),
  },
}))
import { api } from '../api/client'

const VERSIONS = {
  local_version: '0.9.1',
  instances: [
    { id: 'old', name: 'Old box', version: '0.9.0', behind: true },
    { id: 'same', name: 'Same box', version: '0.9.1', behind: false },
    { id: 'quiet', name: 'Quiet box', version: '', behind: false },
  ],
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.instancesVersions).mockResolvedValue(VERSIONS)
})

describe('CrewVersionStatus', () => {
  it('shows the exact versions on a crew that runs an older build', async () => {
    renderWithProviders(<CrewVersionStatus instanceId="old" connected />)
    expect(await screen.findByText('Behind: remote 0.9.0, local 0.9.1')).toBeInTheDocument()
  })

  it('shows nothing on a crew that runs the same build', async () => {
    renderWithProviders(<CrewVersionStatus instanceId="same" connected />)
    await waitFor(() => expect(api.instancesVersions).toHaveBeenCalled())
    expect(screen.queryByTestId('remote-behind-badge')).toBeNull()
  })

  it('shows nothing when the crew did not report a version', async () => {
    renderWithProviders(<CrewVersionStatus instanceId="quiet" connected />)
    await waitFor(() => expect(api.instancesVersions).toHaveBeenCalled())
    expect(screen.queryByTestId('remote-behind-badge')).toBeNull()
  })

  it('reads nothing for a crew that is not connected', () => {
    renderWithProviders(<CrewVersionStatus instanceId="old" connected={false} />)
    expect(api.instancesVersions).not.toHaveBeenCalled()
    expect(screen.queryByTestId('remote-behind-badge')).toBeNull()
  })

  it('shares one versions read between every row', async () => {
    renderWithProviders(
      <>
        <CrewVersionStatus instanceId="old" connected />
        <CrewVersionStatus instanceId="same" connected />
      </>,
    )
    await screen.findByText('Behind: remote 0.9.0, local 0.9.1')
    expect(api.instancesVersions).toHaveBeenCalledTimes(1)
  })
})

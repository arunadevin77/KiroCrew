/**
 * The version line on a CONNECTED Remote Crew row: the "Behind" badge when the
 * crew runs an older build than this machine.
 *
 * Every row reads the SAME `['instances', 'versions']` query, so a list of N
 * crews costs one `GET /api/instances/versions` rather than N capability reads;
 * react-query shares the result between rows. The key sits under
 * `['instances']`, so the invalidation every connect, disconnect and edit
 * already issues re-reads the versions too.
 */
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import RemoteBehindBadge from '../../components/RemoteBehindBadge'

export const INSTANCE_VERSIONS_KEY = ['instances', 'versions'] as const

/** How long a versions read stays fresh: a remote's version only moves when it
 *  is updated and restarted, which the reconnect invalidation already covers. */
const VERSIONS_STALE_MS = 60_000

export function useInstanceVersions(enabled: boolean) {
  return useQuery({
    queryKey: INSTANCE_VERSIONS_KEY,
    queryFn: () => api.instancesVersions(),
    enabled,
    staleTime: VERSIONS_STALE_MS,
    retry: false,
  })
}

export default function CrewVersionStatus({ instanceId, connected }: { instanceId: string; connected: boolean }) {
  const versionsQ = useInstanceVersions(connected)
  if (!connected) return null
  const row = versionsQ.data?.instances.find(r => r.id === instanceId)
  if (!row) return null
  return (
    <RemoteBehindBadge
      behind={row.behind}
      remote={row.version}
      local={versionsQ.data?.local_version || ''}
    />
  )
}

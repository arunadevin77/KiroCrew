import { useAppSelector } from '../store'
import { selectSlotSubagents } from '../store/chatSlice'

/**
 * Whether a Reload of this slot would be refused right now. The backend 409s
 * a reload while a turn runs or while sub-agent children are attached (the
 * reset would tear down their shared runtime), so every Reload control
 * disables on the same answer instead of offering a click that fails.
 */
export function useReloadBlocked(slotKey: string): { isRunning: boolean; reloadBlocked: boolean } {
  const isRunning = useAppSelector(s => !!s.dashboard.slots.find(x => x.key === slotKey)?.running)
  const slotSubagents = useAppSelector(s => selectSlotSubagents(s, slotKey))
  const hasActiveSubagents = Object.values(slotSubagents).some(
    a => a.status === 'pending' || a.status === 'running' || a.status === 'tool',
  )
  return { isRunning, reloadBlocked: isRunning || hasActiveSubagents }
}

import { useId, useState, type ComponentType, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, ChevronDown, ChevronRight, Timer } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { api } from '../api/client'
import type { SlotTurnTimeout } from '../api/client/chatSlotSettings'
import { fmtDuration } from '../i18n/format'
import ErrorNotice from './ErrorNotice'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuLabel, DropdownMenuRadioGroup, DropdownMenuRadioItem,
  DropdownMenuSub, DropdownMenuSubContent, DropdownMenuSubTrigger, DropdownMenuTrigger,
} from './ui/dropdown-menu'
import {
  ContextMenuRadioGroup, ContextMenuRadioItem, ContextMenuSub, ContextMenuSubContent, ContextMenuSubTrigger,
} from './ui/context-menu'

/** The limits offered as one click each, in seconds. Every one sits inside the
 *  global setting's 300–86,400 s range; any other whole number of seconds is
 *  still accepted by the API and shows up here as its own checked row. */
export const TURN_LIMIT_PRESETS_SECS: readonly number[] = [1800, 3600, 7200, 14400, 28800, 43200, 86400]

/** Radio value for "follow the global setting". Never a number, so it cannot
 *  collide with a preset. */
const FOLLOW_GLOBAL = 'global'

export const turnLimitQueryKey = (slotKey: string) => ['slot-turn-timeout', slotKey] as const

/** A limit in seconds as a compact duration: `30m`, `4h`, `1h 30m`. */
export function fmtTurnLimit(secs: number): string {
  const totalMinutes = Math.max(1, Math.round(secs / 60))
  return fmtDuration([[Math.floor(totalMinutes / 60), 'hour'], [totalMinutes % 60, 'minute']], { dropZero: true })
}

/** The rows a limit picker offers: the presets in range, plus the session's
 *  current limit when it is not one of them, in ascending order. */
export function turnLimitOptions(data: Pick<SlotTurnTimeout, 'secs' | 'min' | 'max'>): number[] {
  const presets = TURN_LIMIT_PRESETS_SECS.filter(secs => secs >= data.min && secs <= data.max)
  if (data.secs == null || presets.includes(data.secs)) return presets
  return [...presets, data.secs].sort((a, b) => a - b)
}

type RadioGroupComponent = ComponentType<{
  value?: string
  onValueChange?: (value: string) => void
  children?: ReactNode
}>
type RadioItemComponent = ComponentType<{
  value: string
  disabled?: boolean
  onSelect?: (event: Event) => void
  children?: ReactNode
}>

/**
 * The picker itself, shared by the session menu's submenu and the timeout
 * card's dropdown. Reads the session's limit when it mounts (a menu's content
 * only mounts while open) and writes a pick straight through.
 *
 * The menu stays open on a pick (`preventDefault` on select; Radix still
 * delivers the radio value change). The check moving to the new row is the
 * confirmation that it saved, and a refused write stays on screen as an
 * `ErrorNotice` instead of vanishing with a closed menu.
 */
function TurnLimitChoices({ slotKey, RadioGroup, RadioItem }: {
  slotKey: string
  RadioGroup: RadioGroupComponent
  RadioItem: RadioItemComponent
}) {
  const { t: i18nT } = useTranslation()
  const queryClient = useQueryClient()
  const errorId = useId()
  const [saveError, setSaveError] = useState('')
  const query = useQuery({
    queryKey: turnLimitQueryKey(slotKey),
    queryFn: () => api.chatSlotTurnTimeout(slotKey),
    staleTime: 30_000,
  })
  const mutation = useMutation({
    mutationFn: (secs: number | null) => api.setChatSlotTurnTimeout(slotKey, secs),
    onMutate: () => { setSaveError('') },
    onSuccess: (r) => {
      queryClient.setQueryData<SlotTurnTimeout>(turnLimitQueryKey(slotKey), {
        secs: r.secs, global_secs: r.global_secs, min: r.min, max: r.max,
      })
    },
    onError: (err) => {
      setSaveError(err instanceof Error && err.message ? err.message : i18nT('components.sessionTurnLimit.save_failed'))
      queryClient.invalidateQueries({ queryKey: turnLimitQueryKey(slotKey) })
    },
  })
  const data = query.data
  if (!data) {
    if (query.isError) {
      return (
        <div className="max-w-[260px] px-2 py-1.5">
          <ErrorNotice
            variant="inline"
            className="flex-wrap"
            title={i18nT('components.sessionTurnLimit.load_failed')}
            message={query.error instanceof Error ? query.error.message : String(query.error)}
            messagePlacement="below"
            testId="session-turn-limit-load-error"
          />
        </div>
      )
    }
    return <div className="px-3 py-1.5 text-[12px] text-muted">{i18nT('components.sessionTurnLimit.loading')}</div>
  }
  const value = data.secs == null ? FOLLOW_GLOBAL : String(data.secs)
  const keepOpen = (event: Event) => { event.preventDefault() }
  const choose = (next: string) => {
    if (next === value || mutation.isPending) return
    mutation.mutate(next === FOLLOW_GLOBAL ? null : Number(next))
  }
  const mark = (checked: boolean) => (checked
    ? <Check size={13} className="ml-auto shrink-0 text-accent" aria-hidden="true" />
    : null)
  return (
    <>
      <RadioGroup value={value} onValueChange={choose}>
        <RadioItem value={FOLLOW_GLOBAL} disabled={mutation.isPending} onSelect={keepOpen}>
          <span className="truncate">
            {i18nT('components.sessionTurnLimit.follow_global', { limit: fmtTurnLimit(data.global_secs) })}
          </span>
          {mark(value === FOLLOW_GLOBAL)}
        </RadioItem>
        {turnLimitOptions(data).map(secs => (
          <RadioItem key={secs} value={String(secs)} disabled={mutation.isPending} onSelect={keepOpen}>
            <span className="truncate">{fmtTurnLimit(secs)}</span>
            {mark(value === String(secs))}
          </RadioItem>
        ))}
      </RadioGroup>
      {saveError && (
        <div className="max-w-[260px] px-2 py-1.5">
          <ErrorNotice
            id={errorId}
            variant="inline"
            className="flex-wrap"
            title={i18nT('components.sessionTurnLimit.save_failed')}
            message={saveError}
            messagePlacement="below"
            testId="session-turn-limit-save-error"
          />
        </div>
      )}
    </>
  )
}

/** "Turn time limit ▸" in the session menu, on every surface that menu renders on. */
export function SessionTurnLimitSubmenu({ slotKey, variant }: {
  slotKey: string
  variant: 'dropdown' | 'context'
}) {
  const { t: i18nT } = useTranslation()
  const Sub = variant === 'context' ? ContextMenuSub : DropdownMenuSub
  const SubTrigger = variant === 'context' ? ContextMenuSubTrigger : DropdownMenuSubTrigger
  const SubContent = variant === 'context' ? ContextMenuSubContent : DropdownMenuSubContent
  return (
    <Sub>
      <SubTrigger title={i18nT('components.sessionTurnLimit.hint')} data-testid="session-turn-limit-submenu">
        <Timer size={13} className="shrink-0 text-muted" />
        <span className="flex-1">{i18nT('components.sessionTurnLimit.label')}</span>
        <ChevronRight size={12} className="text-muted" />
      </SubTrigger>
      <SubContent className="min-w-[200px]">
        <TurnLimitChoices
          slotKey={slotKey}
          RadioGroup={variant === 'context' ? ContextMenuRadioGroup : DropdownMenuRadioGroup}
          RadioItem={variant === 'context' ? ContextMenuRadioItem : DropdownMenuRadioItem}
        />
      </SubContent>
    </Sub>
  )
}

/** The timeout card's button: the same picker, opened from the card itself. */
export function SessionTurnLimitButton({ slotKey, className }: { slotKey: string; className?: string }) {
  const { t: i18nT } = useTranslation()
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          className={className}
          title={i18nT('components.sessionTurnLimit.card_button_hint')}
          data-testid="error-card-session-turn-limit"
        >
          <Timer size={12} className="lucide-inline shrink-0" aria-hidden="true" />
          {i18nT('components.sessionTurnLimit.card_button')}
          <ChevronDown size={12} className="lucide-inline shrink-0" aria-hidden="true" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="min-w-[200px]">
        <DropdownMenuLabel>{i18nT('components.sessionTurnLimit.label')}</DropdownMenuLabel>
        <TurnLimitChoices slotKey={slotKey} RadioGroup={DropdownMenuRadioGroup} RadioItem={DropdownMenuRadioItem} />
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

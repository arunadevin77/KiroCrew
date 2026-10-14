import { useState, useRef, useEffect, memo, useMemo, useCallback, useId, Fragment } from 'react'
import { createPortal } from 'react-dom'
import { LayoutGroup, AnimatePresence, motion } from 'framer-motion'
import { Plus, X, Monitor, ArrowUpDown, Eye, EyeOff, VenetianMask, FolderPlus, FolderX, CornerUpRight, MessageSquare, MessageSquarePlus, Folder, ChevronRight, ChevronDown, Clock, Pencil, BrushCleaning, Link2, MoreVertical, Tag as TagIcon, Columns3, GripVertical, Check, List, ListTree, Loader2, Settings, RotateCcw, Cpu, Users, Server, Pause, Play } from 'lucide-react'
import { FolderBody } from '../components/FolderBody'
import { FolderRail } from '../components/FolderRail'
import ErrorNotice, { ErrorNoticeMenuItem } from '../components/ErrorNotice'
import FolderGlyph from '../components/FolderGlyph'
import { DndContext, DragOverlay, MeasuringStrategy, type Modifier } from '@dnd-kit/core'
import { SortableContext, verticalListSortingStrategy } from '@dnd-kit/sortable'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useLocation, useNavigate } from 'react-router-dom'
import { usePreviewFlag } from '../hooks/usePreviewFlag'
import { PREVIEW_FOLDER_CONDUCTOR } from '../utils/previewFlags'
import { useStore } from 'react-redux'
import { useAppDispatch, useAppSelector } from '../store'
import type { RootState } from '../store'
import { useConnected } from '../hooks/useConnected'
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuSub, DropdownMenuSubTrigger, DropdownMenuSubContent } from '../components/ui/dropdown-menu'
import { ContextMenu, ContextMenuTrigger, ContextMenuContent, ContextMenuItem, ContextMenuSeparator } from '../components/ui/context-menu'
import { offlineProps } from '../utils/offline'
import { switchSlot, deleteSlot, fetchHistory, resumeFromHistory, deleteHistorySession, setCloseRefused } from '../store/chatSlice'
import { api, SEARCH_MIN_CHARS } from '../api/client'
import { isInstancesDisabledError } from '../utils/instancesDisabled'
import { errMessage } from '../utils/thunkError'
import { computeRecentRank, clampTintCount } from '../utils/recencyTint'
import { folderOffersHide } from '../utils/folderVisibility'
import { groupHistoryByFolder } from '../utils/groupHistoryByFolder'
import { highlightText } from '../utils/highlightText'
import { slotChannelLabel, slotChannelNamespace } from '../utils/channelOrigin'
import { sessionRefBlockReason } from '../utils/sessionRefs'
import { SearchInput, Input, Btn } from '../components/ui'
import SimpleSelect from '../components/SimpleSelect'
import FolderConfigModal from '../components/FolderConfigModal'
import ModelDropdownList from '../components/ModelDropdownList'
import { useAvailableModelsQuery } from '../hooks/useAvailableModels'
import { useListboxKeyboard } from '../hooks/useListboxKeyboard'
import { useDndSensors } from '../hooks/useDndSensors'
import { useSessionPalette } from '../hooks/useSessionPalette'
import { ancestorsOf, closedCreatorCitation, descendantsOf, orphanCitation } from '../lib/sessionLineage'
import { partitionBulkSwitch } from '../lib/bulkModelSwitch'
import { isEmbeddedPane } from '../lib/embedded'
import { setCrewEnabled } from '../lib/crewEnabled'
import { useReducedMotion } from '../hooks/useReducedMotion'
import { useSessionActions } from '../hooks/useSessionActions'
import { useChatPopouts } from '../hooks/useChatPopouts'
import { useImeGuard } from '../hooks/useImeGuard'
import { useIsMobile } from '../hooks/useIsMobile'
import { useIsTouchDevice } from '../hooks/useIsTouchDevice'
import ResizeHandle from '../components/ResizeHandle'
import { SearchFilterBar, FilterMenuButton, FilterChip, FILTER_CHIP_ROW_CLS, FilterMenuLabel, FilterMenuContent } from '../components/SearchFilterBar'
import { ListDock } from '../components/ListDock'
import { LIST_SHELL_CLS, LIST_HEADER_CLS, LIST_TITLE_CLS, LIST_BODY_CLS } from '../components/listShell'
import { safeSetItem } from '../utils/safeStorage'
import FolderMoveSubmenu from '../components/FolderMoveSubmenu'
import MoveUndoBar from '../components/MoveUndoBar'
import ImportSessionItem from '../components/ImportSessionItem'
import { ChannelBrandIcon, hasChannelBrandIcon } from '../components/ChannelBrandIcon'
import { RemoteCrewChip } from '../components/RemoteCrewChip'
import TagManagerList from '../components/TagManagerList'
import { DndActiveProbe, DndDroppable } from '../components/dnd'
import { FOLDER_SORT_MODES, collectFolderSubtreeIds, folderHasCreatedStamp, folderNameText, type FolderSortMode } from '../utils/folderTree'
import type { ChatFolder, ChatTag } from '../types'
import { SESSION_LANES } from './chat/sessionLane'
import {
  type RecentUnit,
  RECENT_WINDOW_PRESETS,
  formatRecentWindow,
} from './recentWindow'
import { loadChatConfig, saveChatConfig } from './chat/ChatSettings'
import { SessionRowWindowContext, SessionRowWindowScroller, WindowedSessionRow, useSessionRowWindowRoot } from './chat/sessionRowWindow'
import { compareBySort, fmtRelativeTime, readSessionSortKey, SESSION_SORT_STORAGE_KEY, slotActivityTs } from './chat/sessionOrder'
import { STALE_COLLAPSE_PRESETS_MS } from './staleCollapse'
import type { StaleSplit } from './staleCollapse'
import type { SortKey } from './chat/sessionOrder'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'

import { i18nT } from '../i18n/t'
import { uiLocation } from '../uiLocations/uiLocation'
import { useGuideDisclosureScope } from '../guide/GuideRevealScope'
import { useGuide } from '../guide/GuideContext'
import { agentOrDefaultLabel } from '../utils/agentLabel'
import { useLaneScrollMemory } from '../hooks/useLaneScrollMemory'
import { useScrollEdges } from '../hooks/useScrollEdges'
import { compareText, fmtDateFields, fmtList } from '../i18n/format'
import { sidebarCollision } from './chat-sidebar/dnd/collision'
export { sidebarCollision, isFolderNestBand } from './chat-sidebar/dnd/collision'
export { boardSidebarWidth } from './chat-sidebar/board'
import { PrivateChatRow } from './chat-sidebar/PrivateChatRow'
import { ChatPaneDropZone, ParentFolderDropTarget, RootDropHint, BoardUnfileDropStrip, SortableFolderBlock, SortableSubfolderBlock, SortableColumnFolder, FolderDragGhost, SessionDragGhost } from './chat-sidebar/dnd/targets'
import type { Slot, HistoryItem, AgentInfo, SessionFilterKey } from './chat-sidebar/types'
import { FOLDERS_SHELVED_LS_KEY, FLAT_VIEW_LS_KEY } from './chat-sidebar/persistence'
import { SESSION_FILTERS, useSessionFilterState, useSessionStatusFilters } from './chat-sidebar/filters'
import { useDebouncedSessionSearch, useSearchMatches } from './chat-sidebar/search'
import { openCrewWindow, useCrewWindow } from './chat/crew-window/crewWindowStore'
import { useStableCallbackProps } from './chat/useStableCallbackProps'
import { sessionRowIdentity, historyRowIdentity } from './chat-sidebar/rowIdentity'
import { buildSidebarRows, guideStepOnRowControl, type ConductorRowView, type RowScene, type RowShell, type SidebarRows } from './chat-sidebar/rows'
import { useSessionSources } from './chat-sidebar/sessionSources'
import { CrewGroupSection, LocalGroupHeader, crewSpaceChain, useCollapsedCrews, useCollapsedCrewSpaces, useCrewEnable } from './chat-sidebar/CrewGroups'
import { crewOf, type CrewGroup } from '../hooks/useInstanceSessions'
import { useSessionRename, useSessionAutoTitle, useFolderRename } from './chat-sidebar/rename'
import { useSidebarLane, useLaneCycle, renderedLane } from './chat-sidebar/lanes'
import { useHistoryPane } from './chat-sidebar/history'
import { usePinnedSessionOrder, usePinnedOrderAuthority, usePinnedKeyboardReorder } from './chat-sidebar/pinnedOrder'
import { useStaleCollapse, useStaleMoveWatcher, useStaleNarrowBridge } from './chat-sidebar/stale'
import { useFolderSort, useFolderVisibility, useFolderFilterReveal, useFolderFilterRows, useFolderMutations, useFolderTree, useRootFolderLanes } from './chat-sidebar/folders'
import FolderCleanupPanel from './chat-sidebar/FolderCleanupPanel'
import { useSidebarResize } from './chat-sidebar/resize'
import { useSidebarScrollPeek, useFullTitlesOnScrollSetting } from './chat-sidebar/scrollPeek'
import ScrollPeekLayer from './chat-sidebar/ScrollPeekLayer'
import { useSidebarTags } from './chat-sidebar/tags'
import { useSidebarModels, modelFilterLabel } from './chat-sidebar/models'
import { useBoardColumns, useColumnPopover, useBoardColumnMutations, useColumnMatches, useBoardFolderCollapse } from './chat-sidebar/board'
import { useHoverHold, useHoverPinLiveness } from './chat-sidebar/hoverHold'
import { useLineageAvailable, useConductorLane, citedCreatorOf } from './chat-sidebar/conductor'
import { detectFolderConductor } from './chat-sidebar/folderConductor'
import { useShortcutOrder } from './chat-sidebar/shortcuts'
import { useFolderDropOps, useSidebarMoveUndo, useSidebarDragHandlers, useNativeSessionDrag } from './chat-sidebar/dnd/useSidebarDrag'
import { useSidebarReveal } from './chat-sidebar/reveal'
import { useHoldPinnedHeaderOnCollapse } from './chat-sidebar/stickyCollapse'
import { COLLAPSE_FLOOR_ATTR } from './chat-sidebar/collapseFloor'
import { useFolderChatCreate, useSessionCreate } from './chat-sidebar/create'
import { BOARD_FOLDER_BODY_CLS, DisclosureChevron, FOLDER_BODY_CLS, FOLDER_BODY_OPEN_PADDING, FOLDER_ROW_STICKY_Z, PinnedSessionDivider, SIDEBAR_TEXT_CONTROL_CLS, STALE_TOGGLE_CLS, isOpenInTabModifierClick } from './chat-sidebar/chrome'
export { FOLDER_BODY_INSET_PX, FOLDER_BODY_CLS, BOARD_FOLDER_BODY_CLS, SIDEBAR_TEXT_CONTROL_CLS, STALE_TOGGLE_CLS } from './chat-sidebar/chrome'
import { SessionRow } from './chat-sidebar/SessionRow'
export { connectedChannelLinks } from './chat-sidebar/SessionRow'

/** Rows at or past this paint ordinal share ONE `orderStamp`, so an insertion
 *  or reorder above them does not re-render them: they snap into their new
 *  position instead of springing there.
 *
 *  `orderStamp` exists so a displaced row re-renders and framer measures it
 *  (see `RowPlacement.orderStamp` in ./chat-sidebar/rows). Stamped as a plain ordinal, a New Chat landing at the
 *  top of a 160-session sidebar shifts every ordinal by one and voids all 160
 *  memo boundaries in one commit — 160 row bodies, 160 layout measurements and
 *  a group-wide spring — for a change whose visible effect is a handful of rows
 *  sliding down by one slot. The rows below the fold are displaced too, but
 *  nobody sees them move, so their spring buys nothing.
 *
 *  48 rows is roughly two sidebar viewports of 56px rows: the visible
 *  displacement stays continuous (the persistent-element rule in
 *  website/AGENTS.md is about what the user can see move), while the per-insert
 *  cost is bounded by this constant instead of growing with the session count.
 *  The deliberate casualty: a user scrolled deep into the list sees rows beyond
 *  the window snap rather than slide when something above them moves. The
 *  ordinal-bump above the window still has its usual cost, so this is a bound,
 *  not a fix for rows inside it. Pinned by ChatSidebar.rowMemo.test.tsx. */
export const SIDEBAR_DISPLACEMENT_WINDOW = 48

/** Stable empty group list for the board, which groups nothing. */
const NO_CREW_GROUPS: CrewGroup[] = []

/**
 * A pill for one duration choice, shared by the Recent window presets and — on a
 * phone, where those options render inline rather than in a flyout — the
 * dormant-collapse thresholds. One component so the two lists cannot drift, and
 * so the duplicated class/style pair does not read as copy-paste.
 */
function DurationChip({ label, selected, onSelect }: { label: string; selected: boolean; onSelect: () => void }) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      className="px-2 py-0.5 rounded-full text-[11px] cursor-pointer border transition-colors"
      style={selected
        ? { background: 'color-mix(in srgb, var(--ok) 12%, transparent)', color: 'var(--ok)', borderColor: 'color-mix(in srgb, var(--ok) 35%, transparent)' }
        : { background: 'transparent', color: 'var(--muted)', borderColor: 'var(--border)' }}
      // A plain button is not a menu item, so clicking it leaves the host menu
      // open on its own — Radix dismisses on an item select or an outside
      // pointer-down, neither of which this is (verified in a browser).
      onClick={onSelect}
    >
      {label}
    </button>
  )
}

/**
 * Catalog keys for the filter rows, chips and tooltips.
 *
 * Keys, not copy: these tables are module-level, so an `i18nT()` call here would
 * resolve once at boot and never follow a language switch — the lookup happens
 * where each label renders. Shaped as flat `Record`s of full literal keys and
 * indexed inline at the `i18nT()` call, because that is the form
 * `scripts/check-i18n-keys.mjs` can resolve statically; a key it cannot resolve
 * is a key it cannot verify exists.
 */
export const FILTER_LABEL_KEY: Record<SessionFilterKey, string> = {
  unread: 'pages.chatSidebar.filter_unread',
  running: 'pages.chatSidebar.filter_running',
  pinned: 'pages.chatSidebar.filter_pinned',
  recent: 'pages.chatSidebar.filter_recent',
}
export const FILTER_DESCRIPTION_KEY: Record<SessionFilterKey, string> = {
  unread: 'pages.chatSidebar.filter_unread_description',
  running: 'pages.chatSidebar.filter_running_description',
  pinned: 'pages.chatSidebar.filter_pinned_description',
  recent: 'pages.chatSidebar.filter_recent_description',
}

/** Compute a date segment label for a session timestamp. Mirrors ChatGPT/Claude.
 *  Accepts either a Unix epoch (seconds) from backend `modified` or an ISO `created` string. */
function dateSegment(ts: number | string | undefined): string {
  if (ts == null) return i18nT('pages.chatSidebar.older')
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts)
  if (isNaN(d.getTime())) return i18nT('pages.chatSidebar.older')
  const now = new Date()
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const startOfYesterday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1)
  const daysAgo7 = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 7)
  const daysAgo30 = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 30)
  if (d >= startOfToday) return i18nT('pages.chatSidebar.today')
  if (d >= startOfYesterday) return i18nT('pages.chatSidebar.yesterday')
  if (d >= daysAgo7) return i18nT('pages.chatSidebar.last_7_days')
  if (d >= daysAgo30) return i18nT('pages.chatSidebar.last_30_days')
  if (d.getFullYear() === now.getFullYear()) return fmtDateFields(d, { month: 'long' })
  return fmtDateFields(d, { year: 'numeric', month: 'long' })
}

/** Move only the visual SESSION preview away from a pointer-owned destination
 * cue. Collision detection continues to use the real pointer coordinates; this
 * modifier belongs on DragOverlay, never DndContext. Keyboard drags keep the
 * preview aligned with their synthetic position. A pointer drag is moved only
 * while a `folder-drop` destination is under it — a folder block, a parent cue,
 * the root lane or the un-nest hint, each of which paints its cue in the lane
 * under the pointer; the chat pane anchors its cue to the composer, so there the
 * preview stays where dnd-kit put it. While a folder is targeted, prefer the
 * open space beside the sidebar so both the parent cue and the child row stay
 * readable; a narrow viewport with no side room moves the ghost below both rows
 * instead, stopping at the same gutter the side placements keep so it cannot
 * leave the viewport. */
const offsetSessionDragGhost: Modifier = ({
  transform, activatorEvent, activeNodeRect, overlayNodeRect, over, windowRect,
}) => {
  if (typeof KeyboardEvent !== 'undefined' && activatorEvent instanceof KeyboardEvent) return transform
  const overData = over?.data.current as { type?: string } | undefined
  if (overData?.type !== 'folder-drop' || !activeNodeRect || !overlayNodeRect || !windowRect) return transform
  const sideGap = 12
  const currentLeft = overlayNodeRect.left + transform.x
  const rightOffset = activeNodeRect.width + sideGap
  if (currentLeft + rightOffset + overlayNodeRect.width <= windowRect.right - sideGap) {
    return { ...transform, x: transform.x + rightOffset, y: transform.y + 8 }
  }
  const leftOffset = -(overlayNodeRect.width + sideGap)
  if (currentLeft + leftOffset >= windowRect.left + sideGap) {
    return { ...transform, x: transform.x + leftOffset, y: transform.y + 8 }
  }
  // Below both rows, but never past the bottom gutter. The overlay sits at its
  // initial rect plus the transform, so the room under it is measured from
  // that absolute bottom edge; a ghost already past the gutter is pulled back.
  const roomBelow = windowRect.bottom - sideGap - (overlayNodeRect.top + transform.y + overlayNodeRect.height)
  return { ...transform, x: transform.x + sideGap, y: transform.y + Math.min(96, roomBelow) }
}
const SESSION_DRAG_GHOST_MODIFIERS = [offsetSessionDragGhost]

/** The ONE notice for a failed ['instances'] read issued by the rows'
 *  runs-elsewhere chips, mounted only where InstanceTabBar is not: an embedded pane (the bar never
 *  polls there) and a top-level /embed/* route (that layout has no top bar).
 *  Everywhere else the bar shows the same failure, so a second notice would
 *  duplicate it. Both surfaces stay silent on exactly one denial, decided by
 *  the shared `isInstancesDisabledError`: the gateway's own `instances_disabled`
 *  403 (the feature is simply off). The route's other 403s (non-owner,
 *  Slack-origin) are real authorization failures and show on both. */
function RemoteCrewNamesError() {
  const { pathname } = useLocation()
  const topBarAbsent = isEmbeddedPane() || pathname.startsWith('/embed/')
  // Same key and fetcher as the chips, so this observer adds no request.
  const { error } = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    enabled: topBarAbsent,
  })
  if (!topBarAbsent || !error || isInstancesDisabledError(error)) return null
  // Lead with what failed in product terms; the server's text follows it.
  const detail = errMessage(error)
  return (
    <div className="mx-2 mt-2 shrink-0">
      <ErrorNotice
        variant="inline"
        className="flex-wrap w-full"
        title={detail ? i18nT('components.instanceTabBar.instances_load_failed') : undefined}
        message={detail || i18nT('components.instanceTabBar.instances_load_failed')}
        askAgent
        testId="remote-crew-names-error"
      />
    </div>
  )
}

interface ChatSidebarProps {
  slots: Slot[]
  activeSlot: string | null
  unreadSlots: string[]
  history: HistoryItem[]
  historyHasMore: boolean
  defaultAgent: string
  installedAgents: AgentInfo[]
  /** What row and history labels are TINTED from (agent colour, inherited
   *  session colour). The host passes the last loaded roster so a session
   *  switch, which empties `installedAgents` until the new fetch lands, does
   *  not flash every label to muted and back. Display only: pickers such as
   *  FolderConfigModal keep reading the scoped `installedAgents`. Defaults to it. */
  rowAgents?: AgentInfo[]
  mode?: string
  onWidthChange?: (w: number) => void
  onDragChange?: (dragging: boolean) => void
  /** The host stretches the sidebar to its own width (the mobile drawer, the
   *  sessions embed), so its paint reserves no chat pane beside it. */
  fillsHost?: boolean
  /** Optional callback fired when the user explicitly clicks a slot.
   *  When provided, this fires AFTER the switchSlot dispatch so consumers
   *  can react to user-driven selection (e.g. to navigate the URL). */
  onSelectSlot?: (key: string) => void
  /**
   * Render session rows WITHOUT Framer layout projection (`layout`/`layoutId`).
   *
   * Set by the mobile sessions drawer, whose slide runs on the COMPOSITOR
   * (WAAPI — see `registerDrawerTargets` in useDrawerSwipe). Projection only
   * stays correct while framer owns every animated ancestor transform: under a
   * compositor-driven ancestor it attributes the panel's travel to the rows
   * themselves and compounds a corrective transform per re-measure (measured
   * >4,000px — the rows visibly flew in from the panel's right edge). The rows
   * are the sidebar's ONLY projection nodes, so this one switch is the whole
   * containment. Costs on mobile: reorders/pin moves snap instead of glide,
   * and the flat↔tree toggle loses its row-morph continuity.
   */
  staticRows?: boolean
  /** Open a session as a TAB on the host surface instead of switching to it,
   *  bound to middle-click, modifier-click and the row menu's "Open in a session
   *  tab".
   *
   *  `background` is set only by middle-click, which QUEUES the session without
   *  moving the user, so several rows can be triaged in a row. Modifier-click
   *  and the menu item are a deliberate "take me there": they open in the
   *  foreground and also report the row through `onSelectSlot`, so the host
   *  leaves Split View as it does on a plain click.
   *
   *  Omitted on surfaces with no tab strip (the embed sessions list, a popped-out
   *  window), and an omitted callback leaves the gestures unbound rather than
   *  falling back to a plain switch — a middle-click that quietly navigated
   *  would be indistinguishable from a misfire. */
  onOpenSlotInNewTab?: (key: string, opts?: { background?: boolean }) => void
  /** Reveal a session's pull request / issue in the side panel instead of
   *  leaving for the provider's website.
   *
   *  Fires AFTER the row's own switchSlot dispatch, so the consumer can address
   *  the panel of the session the chip belongs to. Returns whether the panel took
   *  the link: FALSE (or an omitted callback) falls back to plain link
   *  navigation, which is the correct behaviour both on a surface with no side
   *  panel (the `/embed/sessions` list) and for a url the panel cannot resolve. */
  onOpenSource?: (slotKey: string, link: { url: string; kind: 'change' | 'issue' }) => boolean
  /** When true, ChatPage floats a hide-sidebar button over this header's
   *  top-left (open state), so the header reserves left space for it.
   *  Omitted in embed/sessions mode where the sidebar is the whole view. */
  collapsible?: boolean
  /** Element to portal the "drag a session into the chat" drop zone into —
   *  ChatPage's chat-pane wrapper. The zone renders inside this component's
   *  DndContext (so dnd-kit sees it) but measures against the pane's rect, which
   *  is what makes the whole pane a valid target rather than just the composer.
   *  Omit to disable the gesture (embed/sessions mode has no chat pane). */
  chatDropTarget?: HTMLElement | null
  /** Called when a session is dropped on the chat pane. Receives a snapshot,
   *  not a live slot, because the composer stages it until send. Never fired for
   *  incognito/temporary sessions or for the already-active session. */
  onDropSessionRef?: (ref: { key: string; title: string; messages?: number }) => void
  /** Where a crew row's window opens, for a host with no chat pane of its
   *  own (embed/sessions). Omit to open it over this page's pane. */
  onOpenPeerSession?: (instanceId: string, key: string) => void
  /** Opts the list into the scroll peek: while the user scrolls the session
   *  lane, titles the row clips are shown in full in a floating layer laid
   *  over them. Rows keep the stored width. Omit (mobile drawer, embedded
   *  view) to turn it off: a phone's near-full-width drawer leaves no room
   *  beside the pane for an overflowing title, and the embedded view has no
   *  chat beside it either. See chat-sidebar/scrollPeek. */
  scrollPeek?: boolean
}

/** Sort options, in menu order. The label lives in `SORT_LABEL_KEY`. */
const SORT_OPTIONS: { value: SortKey }[] = [
  { value: 'date-desc' },
  { value: 'date-asc' },
  { value: 'created-desc' },
  { value: 'created-asc' },
  { value: 'name-asc' },
  { value: 'name-desc' },
]
/** Catalog key per sort option — same resolvable shape as `FILTER_LABEL_KEY`. */
export const SORT_LABEL_KEY: Record<SortKey, string> = {
  'date-desc': 'pages.chatSidebar.sort_newest',
  'date-asc': 'pages.chatSidebar.sort_oldest',
  'created-desc': 'pages.chatSidebar.sort_created_newest',
  'created-asc': 'pages.chatSidebar.sort_created_oldest',
  'name-asc': 'pages.chatSidebar.sort_name_asc',
  'name-desc': 'pages.chatSidebar.sort_name_desc',
}
/** Catalog key per folder sort mode -- the "Folder order" rows in the same menu.
 *  Three rows because there are three modes; the list itself is
 *  `FOLDER_SORT_MODES`, so a fourth mode fails typing here rather than rendering
 *  with no label. */
export const FOLDER_SORT_LABEL_KEY: Record<FolderSortMode, string> = {
  custom: 'pages.chatSidebar.folder_order_custom',
  name: 'pages.chatSidebar.folder_order_name',
  created: 'pages.chatSidebar.folder_order_created',
}

import { SIDEBAR_MIN } from './chat/sidebarWidth'
export { SIDEBAR_MIN, SIDEBAR_MAX } from './chat/sidebarWidth'

/**
 * The Sessions sidebar: composes the owners under ./chat-sidebar/ (state, projections,
 * drag and drop, reveal, search) and renders what they produce.
 *
 * Owner hooks are called where their block used to sit, so React runs their effects in
 * the order the sidebar has always run them; ChatSidebar.ownerComposition.test.ts pins
 * that call order. Which rows pass the filters, their order, their lane and folder, and
 * what each one shows come from the row model (./chat-sidebar/rows), which reads local
 * key-indexed state for the rows it builds only after its one origin check. The row
 * itself (./chat-sidebar/SessionRow) and its chips are owners too. The row and folder
 * render closures, the idle-session cleanup, the bulk model switch and the JSX stay in
 * this file: source pins read them here (the switchSlot call-site count, the bulk
 * switcher, the restyle ratchet), and the render closures stamp rows in paint order.
 * The owner table and the pin list are in docs/system-specs/modules/history.md.
 */
function ChatSidebar({
  // Bound as `localSlots`, NOT `slots`. This component now holds TWO
  // collections — the caller's local tabs and `allRows`, which also carries
  // live peer rows — and a site that reads the wrong one fails silently: a
  // local-only read under-reports the rendered set, and a merged read leaks
  // local key-indexed state onto a colliding peer key. Neither shows up as a
  // crash, so the names are the guard. The prop itself keeps its public name;
  // only the binding is scoped, which forces every call site inside this file
  // to say which collection it means.
  slots: localSlots, activeSlot, unreadSlots, history, historyHasMore,
  defaultAgent, installedAgents, rowAgents, mode, onWidthChange, onDragChange, fillsHost, onSelectSlot, onOpenSlotInNewTab, onOpenSource, onOpenPeerSession, collapsible,
  chatDropTarget, onDropSessionRef, staticRows, scrollPeek,
}: ChatSidebarProps) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  const dispatch = useAppDispatch()
  const queryClient = useQueryClient()
  // Read-only store handle for point-in-time reads inside async callbacks (the
  // rename-recovery compare-and-set in ./chat-sidebar/rename). useAppSelector subscribes and would
  // re-render; useStore().getState() reads the live value without subscribing.
  const store = useStore<RootState>()
  const ime = useImeGuard()
  const isMobile = useIsMobile()
  const isTouchDevice = useIsTouchDevice()

  // Sidebar-only state
  const [seedError, setSeedError] = useState('')
  // The board/list toggle also persists a chat-config field (`tagColumnsEnabled`)
  // via `saveChatConfig`, which returns false and rolls back when storage cannot
  // hold the write (GPT 6.1, errors-use-error-notice). That reuses the seedError
  // notice slot below, but with a save-specific title — this flag picks it.
  const [toggleSaveFailed, setToggleSaveFailed] = useState(false)
  // Shared failure line for the board's column mutations (delete / reorder /
  // add-after / card drop) — one state, because they all edit the same strip and
  // a second banner per verb would stack. Server-side inputs only, so a failed
  // write leaves nothing to re-enter; the caches are re-synced alongside.
  const [boardError, setBoardError] = useState('')
  // Same shape for the folder mutations (create / delete / update): the optimistic
  // update already rolls the cache back, but a rolled-back rename with no message
  // reads as a dead click.
  const [folderActionError, setFolderActionError] = useState('')
  // A failed "New chat" (any local variant) used to be a silent no-op: the
  // react-query rejection was swallowed and nothing rendered. Mirrors
  // remoteCrewError below, but lives above the list rather than in the menu,
  // because the plain entries close the menu on select.
  const [newChatError, setNewChatError] = useState('')
  // Inline failure reason for "New chat on crew" — a crew create can 502 and
  // leave nothing behind, so its reason is shown in the submenu rather than lost.
  const remoteCrewErrorId = useId()
  const [remoteCrewError, setRemoteCrewError] = useState('')
  // Controlled open for the New-chat menu, so a successful crew create can close
  // it (the crew rows preventDefault to stay open on failure) and closing clears
  // any stale remoteCrewError.
  const [newChatMenuOpen, setNewChatMenuOpen] = useState(false)
  const [slotFilter, setSlotFilter] = useState('')
  const [historyFilter, setHistoryFilter] = useState('')
  // A resumed history row whose surface ChatPage cannot display used to succeed
  // on the wire and then silently bounce the user back to whatever slot was
  // already open, indistinguishable from a dead click (#3624). Neither the
  // check nor the notice lives here any more: `resumeFromHistory` records the
  // outcome on the chat slice and ChatPage renders it above the composer, so
  // the four sibling resume entry points get the same feedback (#5925).
  // Digest of session keys + titles (NOT status), fed to both searches as their
  // revalidate signal. Sorted+joined so reordering `slots` alone cannot refetch.
  const slotTitleDigest = useMemo(
    () => localSlots.map(s => s.key + '\u0000' + (s.title || '')).sort().join('\u0001'),
    [localSlots],
  )
  const {
    historySearchResults, instancesList, instanceSessions, remoteSessionsError, allRows, allLiveSlots,
    selectInstance, crewGroups,
  } = useSessionSources({ historyFilter, slotTitleDigest, localSlots })
  // Whether any row renders a runs-elsewhere chip, i.e. whether this sidebar
  // issues the ['instances'] read that RemoteCrewNamesError reports on.
  const hasRemoteExecutedRow = useMemo(
    () => allRows.some(r => r.executor === 'remote' && !!r.instance_id),
    [allRows],
  )
  // Opening a crew row is local and instant: the window reads the peer's slot
  // itself, so nothing is created here and there is nothing to wait on.
  const crewWindow = useCrewWindow()
  const onOpenPeerSessionRef = useRef(onOpenPeerSession)
  onOpenPeerSessionRef.current = onOpenPeerSession
  const openPeerSession = useCallback((instanceId: string, remoteSlot: string) => {
    // A host with no chat pane of its own (the embedded Sessions list) says
    // where the window opens; otherwise it opens over this page's pane.
    if (onOpenPeerSessionRef.current) onOpenPeerSessionRef.current(instanceId, remoteSlot)
    else openCrewWindow({ instanceId, key: remoteSlot })
  }, [])
  // Connected crews, for the "New chat on crew" entry. `warm` is the authority
  // on which peers hold a live tunnel (it holds the loopback port + minted
  // token); `instancesList` only supplies the display name, so a crew missing
  // from the query still offers its id rather than vanishing from the menu.
  //
  // Select the `warm` OBJECT, never a derived array. react-redux compares a
  // selector's result by reference, so returning `Object.keys(...)` allocates a
  // fresh array on every call, never equals the previous one, and re-renders the
  // sidebar in a loop until the heap dies. That is why `hasWarmInstances`
  // (./chat-sidebar/sessionSources) selects a primitive (`.length > 0`) instead. Deriving happens in the memo.
  const warmMap = useAppSelector(s => s.instances?.warm)
  const warmCrews = useMemo(
    () => Object.keys(warmMap ?? {})
      .map(id => ({ id, name: instancesList.find(i => i.id === id)?.name || id }))
      // compareText, not `localeCompare`: a bare localeCompare collates in the
      // HOST locale and ignores the app language entirely, so the crew list
      // would order itself differently from every other list on the page.
      .sort((a, b) => compareText(a.name, b.name)),
    [warmMap, instancesList],
  )
  // Which folder groups are collapsed in the grouped search-results view.
  // Ephemeral: reset on every query change so a fresh search shows all groups.
  const [collapsedHistoryGroups, setCollapsedHistoryGroups] = useState<Set<string>>(() => new Set())
  useEffect(() => { setCollapsedHistoryGroups(new Set()) }, [historyFilter])
  // Backend relevance rank per slot key (0 = best). A Map instead of a Set so
  // `filteredSlots` can ORDER matches by the backend's ranking (title matches
  // carry a strong field boost server-side) rather than re-sorting them by
  // date, which buries a title match below every fresher session that merely
  // mentions the query in its body. First-wins on canonical-key collisions so
  // a duplicate file cannot demote the better-ranked entry.
  const slotSearchRanks = useDebouncedSessionSearch(
    slotFilter,
    sessions => {
      const ranks = new Map<string, number>()
      sessions.forEach((s, i) => {
        const key = s.key.replace(/^dashboard_/, '')
        if (!ranks.has(key)) ranks.set(key, i)
      })
      return ranks
    },
    slotTitleDigest,
  )
  const {
    renamingSlot, renameScope, renameValue, renameError, setRenameError, renameInputRef,
    suppressMenuRestoreRef, onRenameStart, onRenameChange, onRenameCancel, onRenameCommit,
    onMenuCloseAutoFocus,
  } = useSessionRename({ dispatch, store, queryClient })
  const { autoTitleError, setAutoTitleError, onAutoTitle } = useSessionAutoTitle({ dispatch, store })
  // Folder create / settings modal target. One modal instance is rendered at the
  // sidebar root, so — unlike the inline inputs it replaced — it needs no column
  // scope: a folder rendered in several board columns can only have one modal.
  // `parentId` is the fixed destination for 'create' ('' = top level).
  const [folderModal, setFolderModal] = useState<
    { mode: 'create'; parentId: string } | { mode: 'edit'; folderId: string } | null
  >(null)
  const [sortKey, setSortKey] = useState<SortKey>(readSessionSortKey)
  const { lane, setLanePersisted, setFlatView } = useSidebarLane()
  /** Derived, so every existing `flatView` read site keeps its exact meaning. */
  const flatView = lane === 'flat'
  const conductorView = lane === 'conductor'
  const {
    activeFilters, filtersPaused, setAllFiltersPaused, clearAllFilters,
    filterHiddenFolders, setFilterHiddenFolders, toggleFolderFilter, unhideFolderChain,
    showAllFolders, filterTagIds, toggleTagFilter, clearTagFilter, foldersShelved, setFoldersShelved,
    toggleFoldersShelved, toggleFilter, disableFilter, enableFilter,
    hiddenFilters, toggleHideFilter, clearHiddenFilters,
    filterModelKeys, filterModelsExcluded, toggleModelFilter, setModelFilterExcluded, clearModelFilter,
  } = useSessionFilterState()
  const {
    slotsLoaded, workflowActiveSet, automationRunningSet, subagentCounts, subagentStartedCounts, subagentApprovalCounts, unreadSet,
    recentWindowMs, recentAmountDraft, setRecentAmountDraft, recentUnitDraft, selectRecentPreset,
    commitRecentAmount, changeRecentUnit, runningSet, recentSet,
  } = useSessionStatusFilters({ unreadSlots, activeFilters, filtersPaused, enableFilter, localSlots, disableFilter })
  const creatingSlot = useAppSelector(s => s.chat.creatingSlot)
  const connected = useConnected()
  const {
    historyOpen, setHistoryOpen, openHistoryPane, historyHeight, historyDragging, historyResize,
  } = useHistoryPane({ setHistoryFilter, slotFilter, dispatch })
  const [cleanupOpen, setCleanupOpen] = useState(false)
  const [folderCleanupOpen, setFolderCleanupOpen] = useState(false)  // header ⋮ → "Clean up empty folders" panel
  const [manageTagsOpen, setManageTagsOpen] = useState(false)  // header ⋮ → "Manage tags…" panel (list-view tag CRUD)
  const [filterSortOpen, setFilterSortOpen] = useState(false)
  const [cleanupDays, setCleanupDays] = useState(3)
  const [cleanupExpanded, setCleanupExpanded] = useState(false)
  const [cleanupError, setCleanupError] = useState('')
  const { data: cleanupPreviewData, isLoading: cleanupPreviewLoading, isError: cleanupPreviewError } = useQuery({
    queryKey: ['cleanup-preview', cleanupDays, activeSlot],
    queryFn: () => api.cleanupSessions(cleanupDays, activeSlot || '', true),
    enabled: cleanupOpen,
    gcTime: 0,
  })
  const cleanupPreview = cleanupPreviewData?.keys ?? null
  const activeIsStale = cleanupPreviewData?.active_is_stale ?? false
  const cleanupMutation = useMutation({
    mutationFn: () => api.cleanupSessions(cleanupDays, activeSlot || ''),
    onSuccess: (res) => {
      if (res.keys?.length) {
        for (const key of res.keys) dispatch(deleteSlot(key))
        dispatch(fetchHistory(false))
      }
      if (res.failed?.length) {
        setCleanupError(`${res.failed.length} session(s) failed to archive`)
      } else {
        setCleanupOpen(false)
      }
      queryClient.invalidateQueries({ queryKey: ['cleanup-preview'] })
    },
    onError: (e) => setCleanupError(e instanceof Error ? e.message : i18nT('pages.chatSidebar.archive_failed')),
  })

  // Bulk model switch — apply one model to every live session at once.
  const [bulkModelOpen, setBulkModelOpen] = useState(false)
  const [bulkModel, setBulkModel] = useState('')        // pending pick ('auto' = provider default)
  const [bulkSkipRunning, setBulkSkipRunning] = useState(true)
  const [bulkModelError, setBulkModelError] = useState('')
  // Per-instance id: ChatPage mounts a mobile-drawer sidebar and a desktop one, so a
  // literal id would collide and point one panel's checkbox at the other's label.
  const bulkSkipRunningLabelId = useId()
  const bulkModelsQuery = useAvailableModelsQuery({ enabled: bulkModelOpen })
  const bulkModelOptions = bulkModelsQuery.data
  // The roster failed to load when EITHER flag is up. The ACP adapter never
  // rejects: a 503 / network error / empty response resolves with the last-good
  // cached list or Auto alone and marks the provider degraded, so `isError`
  // alone would stay false through every real failure and the panel would show
  // a one-entry list as if that were the whole catalog. Same pair Settings >
  // Chat reads for its model selects.
  const bulkModelsFailed = bulkModelsQuery.isError || bulkModelsQuery.isDegraded
  // The pick counts only while the roster still lists it. A degraded roster is
  // the last-good CACHED list, so a model can be picked from it, Retry can then
  // succeed with a roster that no longer carries that model, and nothing else
  // would unpick it: the backend accepts any non-registry id, so Switch would
  // reset every session onto a model kiro-cli then refuses. Derived, not
  // stored, so there is no window between the roster changing and the pick
  // being cleared in which Switch could still fire with the stale id.
  const bulkModelPick = useMemo(
    () => (bulkModelOptions.some(m => m.name === bulkModel) ? bulkModel : ''),
    [bulkModelOptions, bulkModel],
  )
  // Count only slots that would actually change. Every slot left out lands in
  // a named bucket the panel renders (see partitionBulkSwitch), so the
  // "Switch N" label can be reconciled with the rows on screen.
  const bulkPartition = useMemo(
    () => partitionBulkSwitch(localSlots, bulkModelPick, bulkSkipRunning),
    [localSlots, bulkModelPick, bulkSkipRunning],
  )
  const bulkAffectedCount = bulkPartition.affected
  // Running sessions the switch could still touch. Before a pick every running
  // session counts; after one, a running session already on the pick is in the
  // "already use this model" line instead, so it is not counted twice.
  const bulkRunningCount = useMemo(
    () => (bulkModelPick ? bulkPartition.runningOffTarget : localSlots.filter(s => s.running).length),
    [bulkModelPick, bulkPartition, localSlots],
  )
  const bulkModelMutation = useMutation({
    // 'auto' goes on the wire verbatim (not collapsed to ''): '' doubles as the
    // "never chosen" state that every reader re-resolves to the agent template's
    // model, so it cannot express an explicit Auto pick.
    mutationFn: ({ model, skipRunning }: { model: string; skipRunning: boolean }) =>
      api.chatSlotsModel(model, skipRunning),
    onSuccess: (res) => {
      // The switched models refresh on the next authoritative sseSlots push;
      // this handler does not eagerly reflect them. The previous dead-key
      // `invalidateQueries(['chat-slots'])` was a no-op (no query is registered
      // on that key; slot.model lives in the Redux dashboard slice), and an
      // eager client-side patch here would need a per-field reconciliation
      // contract to avoid overwriting a reordered authoritative frame -- that
      // belongs to the whole-list applySlots reducer-contract work in #11149,
      // not this rename-recovery fix. Removing the no-op keeps the pre-existing
      // behaviour without carrying that contract into this PR.
      // Partial failure: the endpoint returns 200 with a non-empty `failed`
      // list when some slots' resets raised. Surface it and keep the panel
      // open instead of silently closing on a partial success.
      if (res.failed?.length) {
        setBulkModelError(i18nT('pages.chatSidebar.session_failed_to_switch', { count: res.failed.length }))
      } else {
        setBulkModelOpen(false)
        setBulkModel('')
        setBulkModelError('')
      }
    },
    onError: (e) => setBulkModelError(e instanceof Error ? e.message : i18nT('pages.chatSidebar.switch_failed')),
  })
  // Roving-focus keyboard nav for the model list (WAI-ARIA listbox). No filter
  // input here, so the hook moves focus into the list on open; Escape/Tab close.
  const bulkListRef = useRef<HTMLDivElement>(null)
  const bulkInputRef = useRef<HTMLInputElement>(null)
  const { onListKeyDown: bulkOnListKeyDown } = useListboxKeyboard({
    open: bulkModelOpen,
    dropdownRef: bulkListRef,
    inputRef: bulkInputRef,
    hasFilterInput: false,
    filteredCount: bulkModelOptions.length,
    onEnterSingleMatch: () => {},
    closeToTrigger: () => { setBulkModelOpen(false); setBulkModel(''); setBulkModelError('') },
  })

  const {
    pinned, pinnedOrder, pinnedRank, reorderPinned, orderState: pinnedOrderState,
  } = usePinnedSessionOrder({ localSlots, sortKey })

  const {
    staleCollapseMs, setStaleCollapseMs, staleExpanded, setStaleExpanded, setStaleRecentlyMoved,
    isStaleExempt,
  } = useStaleCollapse({ pinned, activeSlot, runningSet, subagentCounts, unreadSet })

  // The two halves render either side of the expander, so a held row crossing it
  // leaves the sub-list the anchor was measured against. Freeze the side instead.
  const holdStaleSide = (split: StaleSplit<Slot>): StaleSplit<Slot> => {
    const pin = hoverPinRef.current
    if (!pin || pin.scope !== 'list') return split
    const from = pin.staleSide ? split.fresh : split.stale
    // `pin.key` and `seenOrder` are origin-qualified (captured from
    // `data-session-row`), so every slot must be matched through
    // `sessionRowIdentity` — a raw `s.key` compare would miss a peer row and
    // collide two rows that share a raw key.
    const at = from.findIndex(s => sessionRowIdentity(s) === pin.key)
    // Absent from the side it does not belong on is the normal case, in every
    // container that does not hold the row as well as before it migrates.
    if (at < 0) return split
    const rank = new Map(pin.seenOrder.map((k, i) => [k, i]))
    const mine = rank.get(pin.key)
    if (mine == null) return split
    const to = pin.staleSide ? split.stale : split.fresh
    // Reseat by the CAPTURED order, so it returns to the position it was read at
    // rather than to the end of the half it is going back to.
    const seat = to.reduce((n, s) => {
      const r = rank.get(sessionRowIdentity(s))
      return n + (r != null && r < mine ? 1 : 0)
    }, 0)
    const seated = to.slice()
    seated.splice(Math.min(seat, seated.length), 0, from[at])
    const rest = from.filter(s => sessionRowIdentity(s) !== pin.key)
    return pin.staleSide ? { fresh: rest, stale: seated } : { fresh: seated, stale: rest }
  }

  // The row model's dormant split (inert while narrowed or under a non-date sort),
  // then the hover hold on top of it.
  const splitStale = (list: Slot[]): StaleSplit<Slot> =>
    holdStaleSide(sidebarRows.staleSplit(list, { collapseMs: staleCollapseMs, isExempt: isStaleExempt }))
  const renderStaleSection = (containerId: string, staleSlots: Slot[], depth: number, containerName?: string): React.ReactNode => {
    if (staleSlots.length === 0) return null
    const open = staleExpanded.has(containerId)
    const regionId = `stale-rows-${containerId}`
    const lblId = `stale-lbl-${containerId}`
    const ctxId = `stale-ctx-${containerId}`
    // One pluralised sentence carries the count AND says where the rows went,
    // so a folder badge of "2" over one visible row plus "1 dormant session
    // hidden" visibly adds up. A bare noun + count pill read as a category,
    // not as "the rest are in here" (gui-user-test friction on this row).
    const count = staleSlots.length
    const label = open
      ? i18nT('pages.chatSidebar.stale_collapse_row_shown', { count })
      : i18nT('pages.chatSidebar.stale_collapse_row_hidden', { count })
    // The threshold is otherwise only named in the sort/filter menu; the same
    // compact window label ("7d") ties the row back to that setting.
    const windowLabel = formatRecentWindow(staleCollapseMs)
    const hint = open
      ? i18nT('pages.chatSidebar.stale_collapse_row_hint_open', { window: windowLabel })
      : i18nT('pages.chatSidebar.stale_collapse_row_hint', { window: windowLabel })
    return (
      <Fragment key={`stale-${containerId}`}>
        {/* aria-labelledby composes the visible sentence + a visually-hidden
            container name, so AT announces "1 dormant session hidden <folder>"
            — an aria-label would override the button contents and re-composing
            it per locale is exactly the concatenation trap the i18n rules ban. */}
        <button type="button"
          aria-expanded={open}
          aria-controls={regionId}
          aria-labelledby={`${lblId} ${ctxId}`}
          title={hint}
          data-testid={`stale-expander-${containerId}`}
          onClick={() => setStaleExpanded(prev => {
            const next = new Set(prev)
            if (next.has(containerId)) next.delete(containerId); else next.add(containerId)
            return next
          })}
          data-stale-toggle="" className={STALE_TOGGLE_CLS}>
          <DisclosureChevron open={open} size={12} />
          <span id={lblId} className="tabular-nums">{label}</span>
          <span id={ctxId} className="sr-only">{containerName
            ? i18nT('pages.chatSidebar.stale_collapse_ctx_in_name', { name: containerName })
            : i18nT('pages.chatSidebar.stale_collapse_row_ungrouped')}</span>
        </button>
        {/* The controlled region always exists so aria-controls never dangles
            in the collapsed state; only the rows are conditionally mounted. */}
        <div id={regionId} data-stale-region={containerId} hidden={!open}>{open && staleSlots.map(s => renderSessionRow(s, depth, false))}</div>
      </Fragment>
    )
  }
  // ── end stale-session collapse ─────────────────────────────────────────────

  // In-flow discovery affordance for the Older Sessions pane: a text row that
  // follows the LAST session of a lane. It triggers the same action as the
  // persistent footer, but answers a different question. The footer is a
  // structural control pinned under the scroll area for a user who already
  // knows the pane exists; this row sits where a user scanning the list runs
  // out of rows without finding their chat — which is exactly where a session
  // evicted from the open-tab list (idle eviction, restart, cleanup, a closed
  // tab) has gone. A new user has no other cue that sessions move anywhere, so
  // the row is what connects "my chat is gone" to "it is one click below".
  // Hidden while the pane is open: the pane itself is then the continuation.
  const renderOlderSessionsHint = (lane: string): React.ReactNode => {
    if (historyOpen) return null
    return (
      <button
        type="button"
        data-testid={`older-sessions-hint-${lane}`}
        onClick={openHistoryPane}
        {...uiLocation('sessions.show-all-older')}
        className={`mt-1 mx-1 px-2 py-1.5 ${SIDEBAR_TEXT_CONTROL_CLS}`}
      >
        {i18nT('pages.chatSidebar.show_all_older_sessions')}
      </button>
    )
  }

  // Ranks up to the configured count of sessions by settled recency for the sidebar tint —
  // see ../utils/recencyTint. Count = server-side dashboard.recent_tint_count (shared
  // kirocrewConfig query); recomputes when the slots or the configured count change.
  const { data: mcCfg, status: mcCfgStatus, error: mcCfgError, errorUpdatedAt: mcCfgErrorUpdatedAt } = useQuery({ queryKey: ['kirocrewConfig'], queryFn: () => api.kirocrewConfig() })
  const recentTintCount = clampTintCount(mcCfg?.dashboard?.recent_tint_count)
  const recentRank = useMemo(() => computeRecentRank(localSlots, recentTintCount), [localSlots, recentTintCount])

  const {
    folderSortRead, folderSortMode, folderCompare, folderReorderable, folderDragWithheld, folderReorderHint,
    folderReorderHintRef, showFolderReorderHint, hideFolderReorderHint, folderSortMut,
  } = useFolderSort({ queryClient, mcCfg, mcCfgStatus, mcCfgError, mcCfgErrorUpdatedAt, setFolderActionError })

  const {
    folderEditInputRef, editingId, setEditingId, editScope, setEditScope, editName, setEditName,
  } = useFolderRename({ renamingSlot, suppressMenuRestoreRef })

  // Folders via React Query. `isSuccess` gates the stale-collapse move
  // watcher below: before folder data has actually ARRIVED `folders` is the
  // [] default, so every filed slot would read as "just moved" the moment
  // real data lands — hydration is not user movement. isSuccess (not
  // isFetched, which is also true after a FAILED first fetch) stays false
  // through an error window until the websocket seed or a retry backfills.
  const { data: folders = [], isSuccess: foldersLoaded, isError: foldersFailed, error: foldersError, refetch: refetchFolders } = useQuery<ChatFolder[]>({ queryKey: ['chat-folders'], queryFn: () => api.chatFolders() })

  const {
    tagsData, tagsQueryFailed, refetchTags, tagById, activeTagIds, tagFilterRows, activeTagNames,
  } = useSidebarTags({ filterTagIds, localSlots })
  const { modelFilterRows, activeModelKeys, activeModelRows, modelFilterPasses } = useSidebarModels({ filterModelKeys, filterModelsExcluded, localSlots })
  // The direction is only ON when it has something to invert: a stored exclude
  // whose keys no session uses any more must not draw a ticked invert row (or
  // mark rows as hidden) while the next tick would narrow to "only X".
  const modelFilterInverted = filterModelsExcluded && activeModelKeys.size > 0
  const {
    rawColumns, tagColumnsSettled, columnsFailed, columnsError, refetchColumns, tagColumnsEnabled,
    hideEmptyFolderBody, orderedColumns,
  } = useBoardColumns()
  // After the columns: only board view may widen the sidebar past SIDEBAR_MAX.
  const {
    paintedSidebarWidth, rootStyle, sidebarMax, paintedWidthRef, sidebarResize, nudgeSidebar, widenForBoard, restorePreBoardWidth,
  } = useSidebarResize({ onWidthChange, onDragChange, fillsHost, boardActive: orderedColumns.length > 0 })
  const fullTitlesOnScroll = useFullTitlesOnScrollSetting()
  // Which edges of the board's lane strip hide lanes. A window too narrow for
  // every lane clips the strip at the sidebar's edge, and an overlay scrollbar
  // leaves no standing sign that more lanes sit past it, so the strip paints an
  // edge cue from these (the session tab strip's cue width). The box
  // observer covers a resize; a lane added or removed changes only the
  // content, so the column count re-measures it.
  const [attachStripEdges, stripEdges, remeasureStripEdges] = useScrollEdges<HTMLDivElement>()
  useEffect(() => { remeasureStripEdges() }, [orderedColumns.length, remeasureStripEdges])
  usePinnedOrderAuthority({ orderState: pinnedOrderState, slotsLoaded, tagColumnsSettled, orderedColumns })
  const {
    columnEditId, setColumnEditId, popoverPos, columnPopoverRef, columnPopoverImeLatch, closeColumnPopover,
  } = useColumnPopover()

  const {
    updateColumnMutation, deleteColumnMutation, reorderColumnsMutation, addColumnAfterMutation,
    dropSlotMutation, missingLanes, seedStateLanesMutation,
  } = useBoardColumnMutations({ queryClient, setBoardError, orderedColumns, rawColumns, paintedWidthRef, widenForBoard, setSeedError })
  const {
    columnMatches,
  } = useColumnMatches({ subagentStartedCounts, subagentApprovalCounts, workflowActiveSet, automationRunningSet })

  const {
    slotFolders, foldersWithActiveSubtree, setRevealForcedVisible, isFolderHidden, filterHiddenSubtree,
  } = useFolderVisibility({ folders, localSlots, filterHiddenFolders })

  useStaleMoveWatcher({ foldersLoaded, localSlots, slotFolders, setStaleRecentlyMoved })

  const {
    searchRanked, folderNameMatchIds,
  } = useSearchMatches({ slotFilter, slotSearchRanks, folders, isFolderHidden })

  // The folder filter goes inert while searching, in BOTH views: a query must
  // reach every match, so an unchecked folder can never become a search dead
  // end. Everything that consults the filter routes through this flag.
  const folderFilterActive = slotFilter.trim() === '' && filterHiddenFolders.size > 0

  // While a drag is in flight the row model keeps the filtered list it had when the
  // drag started, so the lanes hold still under the pointer.
  const [dragFrozen, setDragFrozen] = useState(false)
  // Layout-projection budget: every enrolled session row belongs to one
  // LayoutGroup, and Framer measures getBoundingClientRect for each enrolled
  // node on a commit. renderSessionRow therefore enrolls only the first
  // SIDEBAR_DISPLACEMENT_WINDOW paint positions; later rows stay ordinary
  // motion divs and snap. Reduced motion disables even that bounded window.
  // The shared live reader (not framer's useReducedMotion): the sidebar test
  // files mock framer-motion per-file, and the hook reads the media query
  // directly and re-renders on change.
  const reduceMotion = useReducedMotion()

  // The reveal's ways to drop the search and a row's folder hide (the hidden ancestor
  // chain, walked over the current folders). The tag and status clears are stable
  // owner callbacks.
  const clearSearch = useCallback(() => setSlotFilter(''), [])
  const clearFolderHide = useCallback((folderId: string | undefined) => unhideFolderChain(folderId, folders),
    [unhideFolderChain, folders])
  // Per-machine groups (preview): a row a crew owns, or a local slot whose turns
  // run on it, leaves the Local lanes and renders under that crew's group, in the
  // same filtered order. The board keeps every row in its columns, so it groups
  // nothing.
  const shownCrewGroups = orderedColumns.length > 0 ? NO_CREW_GROUPS : crewGroups
  // The row model (./chat-sidebar/rows): which rows pass the filters, the one lane
  // order, the lanes' and folders' populations, the reveal registry and the chip
  // counts. Live sessions from connected remote instances are merged in ahead of the
  // filter and the sort, so a remote row is narrowed and ordered by exactly the same
  // rules as a local one, while local key-indexed state is read only after the
  // model's origin check. Rebuilt every render from the previous result: each
  // memoized output keeps its identity while the inputs it reads hold still; the
  // result, its `lanes`, `staleSplit` and `views` are fresh per build.
  const previousRowsRef = useRef<SidebarRows | null>(null)
  const sidebarRows = buildSidebarRows({
    rows: allRows,
    local: {
      folderOf: slotFolders, pinned, pinnedRank, unread: unreadSet, running: runningSet, recent: recentSet,
      searchRanks: searchRanked,
    },
    filters: {
      tags: { resolved: activeTagIds, raw: filterTagIds },
      models: { passes: modelFilterPasses, active: activeModelKeys.size > 0 },
      search: { text: slotFilter, folderMatches: folderNameMatchIds },
      status: { active: activeFilters, hidden: hiddenFilters, paused: filtersPaused, recentWindowMs },
      folders: { hiddenSubtree: filterHiddenSubtree, active: folderFilterActive },
    },
    clears: { tags: clearTagFilter, models: clearModelFilter, search: clearSearch, status: clearAllFilters, folder: clearFolderHide, hides: clearHiddenFilters },
    crewGroups: shownCrewGroups,
    sortKey,
    clock: Date.now,
    frozen: dragFrozen,
  }, previousRowsRef.current)
  previousRowsRef.current = sidebarRows
  const { listNarrowed, revealBlockingFilters, laneOrder, isRowFolderHidden, isRowStatusHidden } = sidebarRows
  // `laneSlots` is the Local lanes' rows: `filteredSlots` minus every row a crew
  // group holds. `shownCrewRows` is each crew group's rows, the folder hide applied.
  const {
    main: laneSlots, crew: shownCrewRows, crewRowCount: shownCrewRowCount,
    flat: flatSlots, ungrouped: ungroupedSlots, boardPeerCount,
  } = sidebarRows.lanes
  const filterCounts = sidebarRows.statusCounts
  const [collapsedCrews, toggleCrewCollapsed, expandCrew] = useCollapsedCrews()
  const [collapsedSpaces, toggleSpaceCollapsed, expandSpaces] = useCollapsedCrewSpaces()
  const enableCrew = useCallback((id: string) => setCrewEnabled(dispatch, id, true)
    .finally(() => { void queryClient.invalidateQueries({ queryKey: ['instances'] }) }), [dispatch, queryClient])
  const crewEnable = useCrewEnable(enableCrew, id => instancesList.find(i => i.id === id)?.name || id)

  const { hoverPinRef, heldDisplacedRef, releaseHoverPin, heldLane, onRootPointerOver } = useHoverHold()

  /**
   * The session CONDUCTING a folder, derived from the folder's own rows.
   *
   * Read once, by the folder's menu, to decide whether that menu offers the
   * chat item and to name the session it opens. A folder whose rows resolve no
   * conductor gets `null` and its menu is the menu on main, which is why no
   * other branch in this file had to change: the ordinary folder row is still
   * the only shape this lane draws.
   *
   * The whole row comes back rather than its key, because the item names the
   * session it opens: a label that says only "conductor" leaves a first-time
   * reader with no idea what the click does.
   *
   * Computed per render rather than memoized. `sidebarRows` is a fresh object
   * every render, so a memo keyed on it would recompute anyway and only add a
   * cache to read; the work is one pass over each folder's own rows.
   */
  /**
   * Is the conductor PIN on? Settings > Developer > Feature Previews, default
   * off (see `utils/previewFlags.ts`).
   *
   * It gates the pinned card, its accent bar and its tag -- the three things
   * that change the resting list -- and nothing else. Neither way to REACH the
   * conductor is gated on it: not the row's chat button, and not the same action
   * in the folder's two menus. Both are ingresses that move nothing on the list,
   * and holding them would leave a conducted folder with no way to the
   * conversation it is drawn around.
   */
  const folderConductorPin = usePreviewFlag(PREVIEW_FOLDER_CONDUCTOR)

  /**
   * This folder's conductor AS A ROW ON SCREEN, or null. The one place either
   * caller -- the folder menu's item and the pinned card -- asks the question.
   *
   * Two sets, and they are different on purpose. Detection runs over
   * `allRowsIn`, the folder's whole membership: which session opened the others
   * is a fact about the folder, and reading it off the FILTERED lane meant a
   * search or a status hide that concealed the real root promoted whichever row
   * was left on top -- so the menu claimed a worker was "the chat that started
   * this folder's other chats", which is false, and with the preview flag on
   * that wrong card was pinned and tagged.
   *
   * The result is then required to be in `rowsIn`, the rows this folder draws,
   * because both callers put it on screen: offering a row the filter concealed
   * would either open a pane the list says is not there or re-admit a row the
   * person asked not to see. A filter that hides the root hides the FEATURE --
   * no item, no pin -- rather than moving it to a different session.
   */
  const folderConductorOf = (folderId: string): Slot | null => {
    const key = detectFolderConductor(sidebarRows.folderTree.allRowsIn(folderId))
    if (key == null) return null
    return sidebarRows.folderTree.rowsIn(folderId).find(r => r.key === key) ?? null
  }

  const { lineageAvailable } = useLineageAvailable({ allRows })
  // Which lane the sidebar is actually rendering (see `renderedLane`). The tag-column
  // board wins when columns exist — flat view does not replace it, it applies INSIDE
  // each column (folders skipped, the column's rows render flat). The conductor lane
  // is gated on `lineageAvailable` as well as the board, and the reason is the toggle:
  // it renders only when more than one lane is available, so with a persisted
  // conductor preference, no edges and no folders the cycle holds `tree` alone, the
  // button is not drawn at all, and an ungated lane would render a layout the user has
  // no control to leave. The folder filter applies to the flat lane and the tree, NOT
  // to the board.
  const shownLane = renderedLane({ lane, boardColumns: orderedColumns.length, folders: folders.length, lineageAvailable })
  const boardLaneActive = shownLane === 'board' || shownLane === 'board-flat'
  const flatLaneActive = shownLane === 'flat'
  const conductorLaneActive = shownLane === 'conductor'
  // Whether any folder ROW is on screen to drag: the tree, and the board unless
  // flat view empties its columns of folders (`relevantFolders`). The flat lane
  // explodes chats out of their folders and the conductor lane nests by lineage.
  // Gates only the copy about DRAGGING (the menu's reorder note) -- the folder
  // order itself is read and offered in every lane.
  const folderRowsDrawn = shownLane === 'tree' || shownLane === 'board'

  // Scroll memory for the session lane. Collapsing the sessions sidebar (or
  // closing the mobile drawer) UNMOUNTS ChatSidebar — OverlayDrawer gates its
  // children on `open` — so the lane remounted at the top and a user who had
  // scrolled deep into a long list was thrown back on every reopen. Anchored
  // on the top visible ROW rather than a pixel offset: rows are
  // `content-visibility: auto` with a 60px intrinsic placeholder, so a fresh
  // mount lays never-rendered rows out taller than rendered ones and the same
  // scrollTop lands on a different session. One entry per lane kind (flat and
  // tree keep independent positions); board columns are their own scrollers
  // and out of scope here. See useLaneScrollMemory.
  const laneScrollRef = useRef<HTMLDivElement | null>(null)
  // Row windowing (pages/chat/sessionRowWindow): the lane scroller is the
  // observer root, so the one callback ref feeds both the scroll memory's ref
  // object and the window. Stable, so React does not detach and re-attach it on
  // every commit.
  const laneRowWindow = useSessionRowWindowRoot()
  const setLaneRowWindowRoot = laneRowWindow.setRoot
  const setLaneScrollEl = useCallback((el: HTMLDivElement | null) => {
    laneScrollRef.current = el
    setLaneRowWindowRoot(el)
  }, [setLaneRowWindowRoot])
  const laneScrollMemory = useLaneScrollMemory(
    boardLaneActive ? null : `chat-sidebar-lane:${conductorLaneActive ? 'conductor' : flatLaneActive ? 'flat' : 'tree'}`,
    laneScrollRef,
  )

  useHoverPinLiveness({ hoverPinRef, releaseHoverPin, filteredSlots: laneSlots, boardLaneActive, flatLaneActive, conductorLaneActive, orderedColumns })

  useStaleNarrowBridge({ listNarrowed, filteredSlots: laneSlots, staleCollapseMs, sortKey, isStaleExempt, setStaleExpanded, slotFolders })
  // Reduced motion disables every row. Otherwise renderSessionRow enrolls only
  // the first SIDEBAR_DISPLACEMENT_WINDOW paint positions in layout projection,
  // bounding Framer's measurement set without a total-list-size cliff.
  // `rowAnimEnabled` itself is derived below the pinned-header hold, which
  // also turns it off (see `instantCloseId`).

  const {
    isFolderFilteredOut, revealedContainers, toggleReveal, hiddenByContainer, allHiddenFolders,
    hiddenFolderCount,
  } = useFolderFilterReveal({ folderFilterActive, filterHiddenFolders, folders, isFolderHidden, folderCompare, boardLaneActive })
  const {
    citedCreatorExists, conductorRows, conductorMatching, lineage, conductorExpanded, toggleConductorExpanded,
    expandConductorAncestors,
  } = useConductorLane({ conductorLaneActive, allRows, allLiveSlots, isRowFolderHidden, isRowStatusHidden, laneOrder, flatSlots })
  // Reveal opens the crew group holding the target, then the lane's own ancestors.
  // Looked up in `allRows`: the reveal may be clearing the filter that hides it.
  const expandRevealAncestors = useCallback((identity: string) => {
    const row = allRows.find(s => sessionRowIdentity(s) === identity)
    const crew = row ? crewOf(row) : undefined
    if (crew && shownCrewRows.has(crew)) {
      expandCrew(crew)
      // And the crew's own spaces above the row, so the reveal lands on it.
      const folders = shownCrewGroups.find(g => g.id === crew)?.folders ?? []
      expandSpaces(...crewSpaceChain(crew, folders, row?.peer_folder_id))
    }
    expandConductorAncestors(identity)
  }, [allRows, shownCrewRows, shownCrewGroups, expandCrew, expandSpaces, expandConductorAncestors])

  const {
    availableLanes, nextLane, laneSwitchLabel, cycleLane,
  } = useLaneCycle({ lineageAvailable, boardLaneActive, folders, lane, setLanePersisted })

  const sidebarRootRef = useRef<HTMLDivElement>(null)
  // Scroll peek (desktop list lanes only); see chat-sidebar/scrollPeek.
  // Desktop-only by design (the narrow-viewport-required exemption): the peek
  // draws a clipped title in full past the sidebar's edge, over the chat
  // beside it. The phone sessions pane is a near-full-width drawer that leaves
  // no room right of the pane for an overflowing title, and nothing beside it
  // to draw over; there the row's `title` attribute and the rename box stay
  // the path to the full string. The board lane has no list rows to peek.
  const peekTitles = useSidebarScrollPeek({
    enabled: fullTitlesOnScroll && !!scrollPeek && !isMobile && !boardLaneActive,
    rootRef: sidebarRootRef,
    laneRef: laneScrollRef,
  })
  const { digitModifierHeld, shortcutDigitByKey } = useShortcutOrder({ sidebarRootRef, dispatch, localSlots })

  const {
    folderFilterRows,
  } = useFolderFilterRows({ folderTree: sidebarRows.folderTree, folders, folderCompare, filterHiddenFolders, filterHiddenSubtree })

  const {
    createFolderMutation, deleteFolderMutation, updateFolderMutation, toggleCollapse,
  } = useFolderMutations({ queryClient, setFolderActionError, folders })
  // The list view's toggle. Its folder headers are sticky, so a collapse also
  // moves the lane to keep a pinned header where it is painted, in the commit
  // that hides the body (see stickyCollapse.ts); `from` is the pressed control,
  // inside the folder block.
  const { armHold, disarm: disarmHold, instantCloseId } = useHoldPinnedHeaderOnCollapse(laneScrollRef, folders)
  // A collapse from a pinned header takes the reduced-motion path for its one
  // commit (`instantCloseId`, see stickyCollapse.ts).
  const rowAnimEnabled = !reduceMotion && instantCloseId === null
  const toggleListFolderCollapse = (folder: ChatFolder, from: HTMLElement) => {
    if (folder.collapsed) disarmHold()
    else armHold(folder.id, from.closest<HTMLElement>('[data-folder-drop]'))
    toggleCollapse(folder.id)
  }

  const { clearBoardCollapse, boardFolderCollapsed, toggleColumnCollapse } = useBoardFolderCollapse()

  // ── Folder drag-to-reorder ──
  // Mouse and touch are split on purpose; the split and its WebKit reasoning
  // live in the shared hook. 5px of mouse travel is this list's own choice -
  // rows are tightly packed and a click only selects, so the threshold can sit
  // lower than the Apps nav rail's. `keyboard` is on because this IS a sortable
  // ring, so the sortable coordinate getter has somewhere to move.
  const dndSensors = useDndSensors({ distance: 5, keyboard: true })
  // Tracks the item currently being dragged, for the DragOverlay preview.
  const [activeDrag, setActiveDrag] = useState<{ type: string; id: string } | null>(null)
  // Its native counterpart: the board card in flight, which no DndContext sees.
  const { nativeSessionDrag, startNativeSessionDrag, endNativeSessionDrag } = useNativeSessionDrag()
  // The derived lane a session card hovers right now. Such a lane refuses the
  // drop, and its header's hover title is out of sight mid-drag, so the lane
  // says why in a visible line instead. It shows only while `nativeSessionDrag`
  // is set, so the card drag's own end hides it; a new drag starts it clean.
  const [derivedHintCol, setDerivedHintCol] = useState<string | null>(null)
  const startBoardCardDrag = useCallback((key: string) => {
    setDerivedHintCol(null)
    startNativeSessionDrag(key)
  }, [startNativeSessionDrag])
  const {
    reorderFolders, moveFolderTo,
  } = useFolderDropOps({ folderReorderable, queryClient, setFolderActionError, updateFolderMutation })
  const { folderSubtrees, expandFolderAncestors } = useFolderTree({ folders, updateFolderMutation, clearBoardCollapse })

  const {
    revealFlash,
  } = useSidebarReveal({ sidebarRootRef, dispatch, localSlots, revealBlockingFilters, staleCollapseMs, sortKey, isStaleExempt, slotFolders, setStaleExpanded, expandFolderAncestors, expandConductorAncestors: expandRevealAncestors, folders, setSlotFilter, setFilterHiddenFolders, setRevealForcedVisible, setFlatView })
  const renameCommit = useCallback((id: string, name: string) => {
    if (name.trim()) updateFolderMutation.mutate({ id, body: { name: name.trim() } })
    setEditingId(null)
  }, [updateFolderMutation, setEditingId])
  const {
    dragMove, undoDragMove, undoBar, folderMove, undoFolderMove, folderUndoBar, moveByDrag, moveFolderByDrag,
  } = useSidebarMoveUndo({ localSlots, folders, moveFolderTo, queryClient })
  // Surface-agnostic session actions (duplicate/read/pin/copy/move/close) shared
  // by all three row menus AND the row's non-menu buttons (Duplicate/Close) so
  // each behaviour has one definition. Rename + Tags stay local (they drive this
  // component's inline-edit + tag-popover state).
  const sessionActions = useSessionActions(mode)
  // The last close the server refused, from any close path (this sidebar, a
  // session menu elsewhere, Cmd+W): `deleteSlot` records it so none drops it.
  const closeRefused = useAppSelector(st => st.chat.closeRefused)
  // Which sessions are currently open in a popped-out window (shared singleton).
  const { poppedOut } = useChatPopouts()
  const {
    handleSidebarDragStart, reportDndActive, handleSidebarDragEnd, handleSidebarDragCancel,
    handleSidebarDragOver,
  } = useSidebarDragHandlers({ releaseHoverPin, setDragFrozen, hideFolderReorderHint, setActiveDrag, activeDrag, dragFrozen, folderReorderable, folderSortRead, showFolderReorderHint, moveFolderByDrag, reorderFolders, searchRanked, pinned, reorderPinned, localSlots, activeSlot, onDropSessionRef, moveByDrag, folders, boardFolderCollapsed, updateFolderMutation, clearBoardCollapse })
  const {
    folderCreateError, setFolderCreateError, createChatInFolder,
  } = useFolderChatCreate({ folders, mode, dispatch, dropSlotMutation, onOpenSlotInNewTab, updateFolderMutation, clearBoardCollapse })

  const {
    crewPreview, openCrewMembers, remoteCrewChatPreview,
    createChatMutation, createRemoteChatMutation, createEphemeralChatMutation,
  } = useSessionCreate({ setNewChatError, dispatch, mode, onOpenSlotInNewTab, setRemoteCrewError, setNewChatMenuOpen, onOpenPeerSession })
  // A conductor-lane member anchor opens on the Members page (see renderSessionRow).
  const navigate = useNavigate()

  // Session colors
  const { paletteColors, boost, boostFor, colorMode } = useSessionPalette()

  // ── Session row (reference-style: color palette, memory_mode, rename on right-click) ──
  // Does any descendant (direct or nested) of `folderId` contain a slot from `slots`?
  function descendantMatch(fs: ChatFolder[], folderId: string, slots: Slot[], visited = new Set<string>()): boolean {
    if (visited.has(folderId)) return false // cycle guard
    visited.add(folderId)
    for (const child of fs) {
      if (child.parent_id !== folderId) continue
      if (slots.some(s => sidebarRows.folderOf(s) === child.id)) return true
      if (descendantMatch(fs, child.id, slots, visited)) return true
    }
    return false
  }

  // Render a folder block scoped to a single column: only slots matching the column predicate.
  // Always render the folder header (even with 0 matches) so users can see + drop into it.
  const renderColumnFolder = (folder: ChatFolder, columnId: string, colSlotKeys: Set<string>, dragHandleProps?: React.HTMLAttributes<HTMLElement>, forceCollapsed?: boolean): React.ReactNode => {
    const childFolders = folders.filter(f => f.parent_id === folder.id).sort(folderCompare)
    const { rows: childSlots, navScope: folderLaneScope, container: folderHoldContainer } = heldLane(sidebarRows.folderTree.rowsIn(folder.id).filter(s => colSlotKeys.has(sessionRowIdentity(s))), columnId, `board:${columnId}:folder:${folder.id}`)
    // A nested folder the person unchecked drops out of the recursion, so neither its
    // header nor anything under it renders. Checking the folder's OWN id is enough:
    // dropping it here takes its descendants with it, the same way the tree's block
    // removal does. Its sessions are already gone from `colSlotKeys`; without this the
    // column would still draw the header of a folder the person asked not to see.
    const deepChildren = childFolders.filter(f => !isFolderFilteredOut(f))
    // Same opt-in as the tree (see the note in renderFolderBlock): only when the
    // setting is on does a column copy holding nothing lose its body, and with it
    // the collapse state it no longer has anything to remember.
    const emptyBody = hideEmptyFolderBody && deepChildren.length === 0 && childSlots.length === 0
    const collapsed = boardFolderCollapsed(columnId, folder)
    // Valid "Move folder to" destinations: everything outside this folder's
    // own subtree (cycle guard). One O(1) lookup, computed once per row.
    const subtreeIds = folderSubtrees.get(folder.id) ?? collectFolderSubtreeIds(folders, folder.id)
    const reparentTargets = folders.filter(f => !subtreeIds.has(f.id))
    // The board lane's answer to a reveal. A column has no folder HEADER row, so the
    // tree's `folderFlash` has nothing to attach to here and a board reveal used to
    // scroll to the column and then sit there unmarked -- the scroll alone is not the
    // confirmation, since the target is often already on screen and nothing moves.
    // Same state, same classes, attached to the box the reveal actually found.
    const boardFolderFlash = revealFlash?.kind === 'folder' && revealFlash.key === folder.id
      ? (revealFlash.fading ? 'fade' : 'flash')
      : null
    const count = childSlots.length + deepChildren.filter(cf => {
      const cfSlots = sidebarRows.folderTree.rowsIn(cf.id).filter(s => colSlotKeys.has(sessionRowIdentity(s)))
      return cfSlots.length > 0 || descendantMatch(
        folders,
        cf.id,
        laneSlots.filter(s => colSlotKeys.has(sessionRowIdentity(s))),
      )
    }).length
    // Board-view folders become sortable only when a drag handle is supplied
    // (root folders wrapped in SortableColumnFolder). Subfolders render without
    // one, so a board subfolder is not directly draggable at all — neither
    // reordered nor re-parented. That is NOT parity with the list view, which
    // has always given a nested row a drag (re-parent before #10428, reorder as
    // well after it); giving the board lane the same gesture needs a handle this
    // path does not pass down, so it stays a separate piece of work. Disabled
    // while renaming in THIS column (rename is per-column via editScope) so
    // the inline input stays usable.
    const draggable = !!dragHandleProps && !(editingId === folder.id && editScope === columnId)
    return (
      // Two drop mechanisms coexist on this block, one per drag SOURCE:
      //  • Native HTML5 onDrop (below) — SESSION cards drag natively (they set
      //    dataTransfer text/plain), so a session dropped here is assigned to
      //    this folder via assignToFolder.
      //  • dnd-kit DndDroppable (this wrapper) — FOLDERS drag via the pointer
      //    sensor (SortableColumnFolder), never via native DnD, so their active
      //    data lives in active.data.current, unreadable by onDrop. The
      //    folder-drop droppable is what lets handleSidebarDragEnd re-parent a
      //    folder dropped here (moveFolderTo). The two never collide: a native
      //    drag never fires dnd-kit's onDragEnd and a dnd-kit drag never fires
      //    the DOM drop event. Id is column-scoped because a root folder renders
      //    once per board column and dnd-kit droppable ids must be unique.
      <DndDroppable key={`col-${columnId}-folder-drop-${folder.id}`} id={`col-${columnId}-folder-drop:${folder.id}`} data={{ type: 'folder-drop', folderId: folder.id }}>
        {({ setNodeRef, isOver }) => (
      // The drag handlers below make this a mouse-only drop target with no
      // keyboard analogue, so scope-disable the static-interaction rule.
      // eslint-disable-next-line jsx-a11y/no-static-element-interactions
      <div ref={setNodeRef}
        data-testid={`col-${columnId}-folder-${folder.id}`}
        data-folder-drop={folder.id}
        // `folder-col` is the board lane's counterpart to the tree's `folder-row`,
        // and it exists for the flash: the reveal outline in index.css is declared
        // COMPOUND (`.folder-row.session-reveal-flash`), so adding the flash class to
        // a box carrying neither row class attaches a class that no rule matches and
        // paints nothing. Naming this box gives the same declaration something to
        // key on here.
        className={`folder-col rounded-md transition-all mb-0.5${isOver ? ' ring-1 ring-accent' : ''}${boardFolderFlash ? ` session-reveal-flash${boardFolderFlash === 'fade' ? ' session-reveal-flash-fade' : ''}` : ''}`}
        onDragOver={e => { e.preventDefault(); e.stopPropagation(); e.currentTarget.classList.add('ring-1', 'ring-accent') }}
        onDragLeave={e => { e.stopPropagation(); e.currentTarget.classList.remove('ring-1', 'ring-accent') }}
        onDrop={e => {
          e.preventDefault(); e.stopPropagation()
          e.currentTarget.classList.remove('ring-1', 'ring-accent')
          const k = e.dataTransfer.getData('text/plain')
          if (k) moveByDrag(k, folder.id)
        }}
      >
        {/* Same rule as the tree row: a column copy with no body has nothing to
         *  toggle, so it is not a control - no button role, no tab stop, no
         *  pointer cursor, no expanded state and no handler. It stays draggable,
         *  because reordering a folder is an action an empty folder can honour. */}
        <div
          className={`group relative flex items-center gap-2 pr-2 py-1 rounded-md ${draggable ? 'cursor-grab active:cursor-grabbing' : emptyBody ? 'cursor-default' : 'cursor-pointer'} text-[12px] text-muted transition-all${emptyBody ? '' : ' hover:text-text hover:bg-bg-hover'}`}
          style={{ paddingLeft: '6px' }}
          {...(draggable ? dragHandleProps : {})}
          // The collapse props go AFTER the drag spread, and the order is
          // load-bearing: `dragHandleProps` are dnd-kit's sortable listeners and
          // they carry the keyboard sensor's own `onKeyDown`, so a later drag
          // spread would replace the collapse handler and Enter would start a
          // drag instead of toggling the body.
          {...(emptyBody
            // No body to disclose, but the row is still a DRAG HANDLE while it is
            // draggable: `useSortable` here hands down only `listeners`, never its
            // `attributes`, so this hand-written `tabIndex` is the only thing that
            // lets the keyboard sensor reach the row - drop it and an empty folder
            // can be reordered with a mouse but not with a keyboard. So it keeps a
            // tab stop and says what it is, and drops only the collapse-specific
            // props (`aria-expanded`, the expand/collapse label, both handlers).
            ? (draggable ? {
              role: 'button',
              tabIndex: 0,
              'aria-label': i18nT('pages.chatSidebar.folder_2', { name: folder.name }),
              'aria-roledescription': i18nT('pages.chatSidebar.drag_to_reorder'),
            } : {})
            : {
              role: 'button',
              tabIndex: 0,
              'aria-expanded': !collapsed,
              'aria-label': collapsed ? i18nT('pages.chatSidebar.expand_folder_name', { name: folder.name }) : i18nT('pages.chatSidebar.collapse_folder_name', { name: folder.name }),
              onClick: () => toggleColumnCollapse(columnId, folder),
              // `e.target === e.currentTarget` restricts the Space/Enter toggle to
              // the row itself. Without it the row swallows every Space typed in a
              // focused DESCENDANT - the inline rename input below - because
              // preventDefault() drops the character and the folder collapses
              // instead. Same guard as Clickable and UpdateModal.
              onKeyDown: (e: React.KeyboardEvent) => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); toggleColumnCollapse(columnId, folder) } },
            })}
        >
          {/* Dimmer and hover-inert on an empty row - same rule as the tree. */}
          {/* Always open on an inert row - same reason as the tree. */}
          <FolderGlyph color={folder.color} icon={folder.icon} size={11} open={!collapsed || emptyBody}
            className={emptyBody ? 'shrink-0 text-muted/40 transition-colors' : undefined} />
          {editingId === folder.id && editScope === columnId ? (
            /* Inline rename input — board-view parity with renderFolderHeader.
             *  Without this branch the ⋯-menu "Rename" set editingId but no
             *  field ever appeared, so rename silently did nothing here. The
             *  collapse handler is on the OUTER div, so the input's onClick +
             *  onMouseDown stopPropagation are load-bearing (they keep clicking
             *  the field from bubbling to toggleColumnCollapse). Keys are
             *  handled the other way round — the row's onKeyDown ignores events
             *  whose target is not the row — so Space types a space here rather
             *  than collapsing the folder. */
            <Input ref={folderEditInputRef} className="flex-1 py-0.5 text-[12px] min-w-0" value={editName} onChange={e => setEditName(e.target.value)} onClick={e => e.stopPropagation()} onMouseDown={e => e.stopPropagation()} {...ime.bindEnter<HTMLInputElement>({ onEnter: () => renameCommit(folder.id, editName), onEscape: () => setEditingId(null), onBlur: () => renameCommit(folder.id, editName) })} />
          ) : (
            // Double-click rename is a mouse-only power shortcut; the accessible
            // path is the ⋯-menu Rename item, so scope-disable the interaction rule.
            // eslint-disable-next-line jsx-a11y/no-static-element-interactions
            <span className="flex-1 truncate" title={i18nT('pages.chatSidebar.double_click_to_rename')} onDoubleClick={e => { e.stopPropagation(); setEditingId(folder.id); setEditScope(columnId); setEditName(folder.name) }}>{folder.name}</span>
          )}
          <span className="text-[10px] text-muted shrink-0">{count}</span>
          {/* List-view parity: an empty folder's row keeps its action cluster
            *  visible (see the note in renderFolderHeader). */}
          {!(editingId === folder.id && editScope === columnId) && (
          <span className={`${emptyBody ? '' : 'opacity-0 '}group-hover:opacity-100 [@media(hover:none)]:opacity-100 group-focus-visible:opacity-100 group-has-[:focus-visible]:opacity-100 has-[[data-state=open]]:opacity-100 transition-opacity flex items-center gap-0.5`}>
            {/* ⋯ menu + a primary "new chat in folder" action, mirroring the
             *  list-view folder header (renderFolderHeader) so board view has
             *  the same one-click way to start a session inside a folder. */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <button type="button" data-testid={`col-${columnId}-folder-${folder.id}-menu`} className="text-muted hover:text-text bg-transparent border-none cursor-pointer p-[2px]" title={i18nT('pages.chatSidebar.more')} aria-label={i18nT('pages.chatSidebar.folder_options_for', { name: folder.name })} aria-haspopup="menu" onMouseDown={e => { e.stopPropagation() }} onClick={e => { e.stopPropagation() }} onKeyDown={e => { e.stopPropagation() }}>
                  <MoreVertical size={11} />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="min-w-[180px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
                <DropdownMenuItem onClick={() => { suppressMenuRestoreRef.current = true; setEditingId(folder.id); setEditScope(columnId); setEditName(folder.name) }}><Pencil size={13} /> {i18nT('pages.chatSidebar.rename')}</DropdownMenuItem>
                <DropdownMenuItem data-testid={`col-${columnId}-folder-${folder.id}-new-sub`} onClick={() => { setFolderModal({ mode: 'create', parentId: folder.id }) }}><FolderPlus size={13} /> {i18nT('pages.chatSidebar.new_subfolder')}</DropdownMenuItem>
                {/* Menu create entries take NO open-in-tab gesture (#10575,
                 *  scoped out): a menu closes on select, and Radix keyboard
                 *  activation synthesizes a modifier-free click, so the
                 *  gesture would be mouse-only and undiscoverable. Listed flat,
                 *  same as the list-view folder menu (renderFolderMenuItems). */}
                <DropdownMenuSeparator />
                <DropdownMenuItem className="items-start" data-testid={`col-${columnId}-folder-${folder.id}-new-incognito`} onClick={() => { createChatInFolder(folder.id, { columnId, memoryMode: 'incognito' }) }}><PrivateChatRow kind="incognito" /></DropdownMenuItem>
                <DropdownMenuItem className="items-start" data-testid={`col-${columnId}-folder-${folder.id}-new-temporary`} onClick={() => { createChatInFolder(folder.id, { columnId, memoryMode: 'temporary' }) }}><PrivateChatRow kind="temporary" /></DropdownMenuItem>
                <DropdownMenuSeparator />
                {/* Re-parent: board-view parity with the list-view folder menu. */}
                <FolderMoveSubmenu variant="dropdown" label={i18nT('pages.chatSidebar.move_folder_to')} sortMode={folderSortMode}
                  folders={reparentTargets}
                  currentFolderId={folder.parent_id || null}
                  onPick={pid => moveFolderTo(folder.id, pid)} />
                <DropdownMenuItem data-testid={`col-${columnId}-folder-${folder.id}-settings`} onClick={() => { setFolderModal({ mode: 'edit', folderId: folder.id }) }}><Settings size={13} /> {i18nT('components.folderConfigModal.folder_settings')}</DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem className="text-danger focus:text-danger" onClick={() => { if (confirm(i18nT('pages.chatSidebar.delete_folder_confirm', { name: folder.name }))) deleteFolderMutation.mutate(folder.id) }}><X size={13} /> {i18nT('pages.chatSidebar.delete_folder')}</DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
            {/* Same three-gesture contract as the header New button; the
             *  existing stopPropagation stays so the header click/drag
             *  handlers never see the press. */}
            <button type="button" data-testid={`col-${columnId}-folder-${folder.id}-new-chat`} className="text-muted hover:text-accent bg-transparent border-none cursor-pointer p-[2px]" title={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} aria-label={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })}
              onClick={e => { e.stopPropagation(); createChatInFolder(folder.id, { columnId, inNewTab: !!onOpenSlotInNewTab && isOpenInTabModifierClick(e) }) }}
              onMouseDown={e => { e.stopPropagation(); if (e.button === 1 && onOpenSlotInNewTab) e.preventDefault() }}
              onAuxClick={onOpenSlotInNewTab ? (e => {
                if (e.button !== 1) return
                e.preventDefault()
                e.stopPropagation()
                createChatInFolder(folder.id, { columnId, inNewTab: true })
              }) : undefined}
              onKeyDown={e => { e.stopPropagation() }}>
              <MessageSquarePlus size={11} />
            </button>
          </span>
          )}
        </div>
        {renderFolderCreateError(folder.id, columnId)}
        {!emptyBody && (
        <FolderBody padding={FOLDER_BODY_OPEN_PADDING} open={!collapsed && !forceCollapsed}>
          {/* `BOARD_FOLDER_BODY_CLS`: the connector beside the rows with the
           *  4px pad that clears the rail's hit strip, as in the list view;
           *  rows inside keep their own `ROW_BOX_CLS` pad. */}
          <div className={BOARD_FOLDER_BODY_CLS}>
            {(deepChildren.length > 0 || childSlots.length > 0) && (
              <FolderRail name={folder.name} id={`${columnId}-${folder.id}`} onToggle={() => toggleColumnCollapse(columnId, folder)} />
            )}
            {/* Default: the empty-folder affordance stays exactly as it was, in
             *  list-view parity (see renderFolderBlock). Reached only when the
             *  setting is OFF - with it on there is no body to put this in. */}
            {deepChildren.length === 0 && childSlots.length === 0 && (
              <button key={`col-${columnId}-newchat-${folder.id}`} type="button" data-testid={`col-${columnId}-folder-${folder.id}-empty-new-chat`} data-folder-new-chat=""
                // Same three-gesture contract as the folder header's "+".
                onMouseDownCapture={onOpenSlotInNewTab ? (e => { if (e.button === 1) e.preventDefault() }) : undefined}
                onAuxClick={onOpenSlotInNewTab ? (e => {
                  if (e.button !== 1) return
                  e.preventDefault()
                  createChatInFolder(folder.id, { columnId, inNewTab: true })
                }) : undefined}
                onClick={e => createChatInFolder(folder.id, { columnId, inNewTab: !!onOpenSlotInNewTab && isOpenInTabModifierClick(e) })}
                title={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} aria-label={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })}
                className="w-full flex items-center gap-2.5 px-4 py-2 rounded-md text-[11px] text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none cursor-pointer text-left">
                <span>{i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })}</span><MessageSquarePlus size={11} className="shrink-0 ml-auto" />
              </button>
            )}
            {deepChildren.map(cf => renderColumnFolder(cf, columnId, colSlotKeys))}
            {childSlots.map((s, i) => {
              const isActive = isActiveRow(s)
              const nextIsActive = isActiveRow(childSlots[i + 1])
              const showDivider = i < childSlots.length - 1 && !isActive && !nextIsActive
                && !startsAutomaticSection(childSlots, i + 1)
              // `scope` stays per-folder so the Framer layoutId and the inline
              // rename target remain unique, but the arrow rove is scoped to the
              // COLUMN: a board column's foldered and ungrouped rows are one
              // visible list, so ArrowDown has to cross the folder boundary.
              return (
                <Fragment key={sessionRowIdentity(s)}>
                  {startsAutomaticSection(childSlots, i) && <PinnedSessionDivider />}
                  {renderSessionRow(s, 1, showDivider, `${folderLaneScope}:${folder.id}`, folderLaneScope, folderHoldContainer)}
                </Fragment>
              )
            })}
          </div>
        </FolderBody>
        )}
      </div>
        )}
      </DndDroppable>
    )
  }

  const {
    startsAutomaticSection, reorderPinnedByKeyboard,
  } = usePinnedKeyboardReorder({ searchRanked, pinned, pinnedOrder, slotFolders, reorderPinned })

  // A row whose session belongs to another page (a crew member's own DM thread,
  // admitted to the conductor lane as a creator anchor) opens where it lives. Read
  // through a ref so the row actions stay ONE stable object across route changes.
  const navigateRef = useRef(navigate)
  navigateRef.current = navigate
  const openElsewhere = useCallback((path: string) => { navigateRef.current(path) }, [])
  // The caller's source-reveal handler is rebuilt whenever its own inputs move,
  // and a new identity here would rebuild `rowActions` and re-render every row.
  // Rows only ever CALL it, so they get a stable forwarder to the latest one;
  // an absent handler stays undefined, which the chips read as "cannot reveal".
  const { onOpenSource: stableOpenSource } = useStableCallbackProps({ onOpenSource })
  // What every session row can do, built once: the row's memo compares this one
  // reference, so none of these may take a new identity per render.
  const rowActions = useMemo(() => ({
    renameInputRef, onRenameStart, onRenameChange, onRenameCommit, onRenameCancel, onAutoTitle,
    onDuplicate: sessionActions.duplicate, onCloseSession: sessionActions.close,
    onMenuCloseAutoFocus, onSelectSlot, onOpenSlotInNewTab, onOpenSource: stableOpenSource,
    onOpenPeerSession: openPeerSession,
    onNativeDragStart: startBoardCardDrag, onNativeDragEnd: endNativeSessionDrag,
    onPinnedKeyboardReorder: reorderPinnedByKeyboard,
    openElsewhere, toggleConductor: toggleConductorExpanded,
  }), [
    renameInputRef, onRenameStart, onRenameChange, onRenameCommit, onRenameCancel, onAutoTitle,
    sessionActions.duplicate, sessionActions.close, onMenuCloseAutoFocus, onSelectSlot, onOpenSlotInNewTab,
    stableOpenSource, openPeerSession, startBoardCardDrag, endNativeSessionDrag, reorderPinnedByKeyboard,
    openElsewhere, toggleConductorExpanded,
  ])
  // The values every row shows the same way, ONE object per change of any of them:
  // each view holds it by reference, so it re-renders every row exactly when one of
  // these moves. The drag members are reduced to primitives first, so a drag-over
  // that rebuilds `activeDrag` with the same session leaves the rows alone.
  const dragInFlight = !!activeDrag
  const activeDraggedKey = activeDrag?.type === 'session' ? activeDrag.id : null
  const activeDraggedPinnedIndex = activeDraggedKey !== null ? (pinnedRank.get(activeDraggedKey) ?? -1) : -1
  const tintAgents = rowAgents ?? installedAgents
  // A live guide step pointing at a row control (`sessions.row-menu`, or a
  // `ui.find` whose opener is it) needs the open row's actions shown with no
  // pointer on the row. Only a step GuideLayer draws here counts (owned by this
  // tab, entered). Read once here so rows re-render only when it flips.
  const guideView = useGuide()?.view
  const guideOnRowControl = guideStepOnRowControl(guideView?.ownedHere && !guideView.needsEnter ? guideView.step : null)
  const rowShell: RowShell = useMemo(() => ({
    connected, mode, defaultAgent, installedAgents: tintAgents, tagById, paletteColors, boost, boostFor, colorMode, recentTintCount,
    // A session row on any touch screen (an iPad too) gets the single ⋯ menu,
    // not the overlay cluster.
    isMobile: isMobile || isTouchDevice,
    dragInFlight, activeDraggedKey, activeDraggedPinnedIndex, guideOnRowControl,
  }), [
    connected, mode, defaultAgent, tintAgents, tagById, paletteColors, boost, boostFor, colorMode, recentTintCount,
    isMobile, isTouchDevice, dragInFlight, activeDraggedKey, activeDraggedPinnedIndex, guideOnRowControl,
  ])
  // What this render shows rows with. The row model reads the local-only members
  // for a local row alone.
  const rowScene: RowScene = {
    activeSlot, crewWindow, shell: rowShell,
    local: {
      recentRank, subagentCounts, subagentApprovalCounts, poppedOut,
      digits: digitModifierHeld ? shortcutDigitByKey : null,
      renaming: { key: renamingSlot, scope: renameScope, value: renameValue },
      revealFlash, nativeDragKey: nativeSessionDrag,
    },
    drag: activeDrag,
  }
  const rowViews = sidebarRows.views(rowScene)
  // The active-row highlight. `activeSlot` names a LOCAL session and a peer row can
  // carry a byte-identical key, so the row model lights a peer row only for the crew
  // window open on its own (crew, key) pair, and a local row only while no crew
  // window is open; five lanes ask it (list folders, fresh children, flat, board
  // root, board columns), each also of the NEXT row to decide dividers.
  const isActiveRow = rowViews.isActive

  // scope namespaces the Framer layoutId per render location. A multi-tag slot
  // can render in several columns at once; same layoutId in one LayoutGroup
  // collides (Framer paints one, hides the rest). Distinct scope = distinct id.
  // Paint-order stamp threaded through every row this render — see
  // RowPlacement.orderStamp for why the memo boundary needs it.
  // Stamps are keyed by (scope, row) and remembered for this render's closure.
  // Folder blocks are rendered by a CHILD (SortableFolderBlock /
  // SortableSubfolderBlock call `renderFolderBlock` from their own render), and
  // dnd-kit re-renders those children on its own, e.g. when it re-measures a
  // droppable after a row mounts. That deferred call runs after this render's
  // pass has stamped every row, so a running counter would hand every row in
  // the block the clamped stamp: all of them re-render with row animation off,
  // then again when the next shell render restores the real stamps. Reusing the
  // stamp the pass already assigned keeps the deferred render's row view
  // identical, so the rows bail out of it. `sessionRowStampCount` is the running
  // total across scopes, which is what the clamp counts.
  const sessionRowStamps = new Map<string, Map<string, number>>()
  let sessionRowStampCount = 0
  const renderSessionRow = (s: Slot, _indent: number, showDivider: boolean, scope = 'list', navScope = scope, holdContainer = navScope, conductor?: ConductorRowView, folderConductor?: boolean) => {
    // Clamped, not raw: rows past the window share a stamp and bail out of a
    // displacement above them (see SIDEBAR_DISPLACEMENT_WINDOW).
    let scopeStamps = sessionRowStamps.get(scope)
    if (!scopeStamps) {
      scopeStamps = new Map<string, number>()
      sessionRowStamps.set(scope, scopeStamps)
    }
    const stampKey = sessionRowIdentity(s)
    let orderStamp = scopeStamps.get(stampKey)
    if (orderStamp === undefined) {
      orderStamp = Math.min(sessionRowStampCount, SIDEBAR_DISPLACEMENT_WINDOW)
      sessionRowStampCount += 1
      scopeStamps.set(stampKey, orderStamp)
    }
    const view = rowViews.view(s, {
      scope, navScope, holdContainer, showDivider, orderStamp, conductor, folderConductor,
      // staticRows (the compositor drawer) folds into the one row-animation
      // gate: projection under a WAAPI-driven ancestor mis-attributes the
      // panel's motion to the rows, so the drawer disables row animation
      // wholesale. Outside it, enroll only the first two-viewport paint
      // window: every later row shares the clamped stamp and snaps, keeping
      // Framer's projection registry bounded at every total list size.
      rowAnimEnabled: rowAnimEnabled && orderStamp < SIDEBAR_DISPLACEMENT_WINDOW && !staticRows,
    })
    // Windowed: far from the viewport the row renders as a cheap stub (see
    // pages/chat/sessionRowWindow). Each window root (the lane, or one board
    // column) mounts its own first rows at once so the initial paint shows real
    // rows; the row model's `windowOf` names the rows that never stub.
    const { title, keepMounted } = rowViews.windowOf(s)
    return (
      <WindowedSessionRow key={view.identity} rowId={view.identity} slotKey={s.key} navScope={navScope} holdContainer={holdContainer}
        title={title} keepMounted={keepMounted}>
        <SessionRow view={view} actions={rowActions} />
      </WindowedSessionRow>
    )
  }

  // The per-machine group chrome each list lane draws: `Local` above the lane's
  // own rows, the crew groups below them. Both are null with no crew group.
  const localGroupHeader = shownCrewGroups.length > 0 ? <LocalGroupHeader /> : null
  const renderCrewGroups = (): React.ReactNode => [crewEnable.notice, ...shownCrewGroups.map(g => (
    <CrewGroupSection key={g.id} group={g} rows={shownCrewRows.get(g.id) ?? []}
      collapsed={collapsedCrews.has(g.id)} onToggle={toggleCrewCollapsed} hideWhenEmpty={listNarrowed}
      chevron={<DisclosureChevron open={!collapsedCrews.has(g.id)} size={11} />}
      onEnable={crewEnable.onEnable}
      spaces={{
        collapsed: collapsedSpaces, onToggle: toggleSpaceCollapsed,
        // A local folder's own body: connector line, inset and collapse motion.
        body: (open, children) => <FolderBody padding={FOLDER_BODY_OPEN_PADDING} open={open}><div className={FOLDER_BODY_CLS}>{children}</div></FolderBody>,
      }}
      renderRows={(rows, scope) => rows.map((s, i) => renderSessionRow(
        s, 0, i < rows.length - 1 && !isActiveRow(s) && !isActiveRow(rows[i + 1]), scope,
      ))} />
  ))]

  // ── Folder row: matches session-row width (full width minus drawer padding) ──
  // Recursively check if a folder or any descendant contains an unread slot.
  const folderTreeHasUnread = (folderId: string, visited = new Set<string>()): boolean => {
    if (visited.has(folderId)) return false
    visited.add(folderId)
    for (const k of unreadSet) { if (slotFolders[k] === folderId) return true }
    return folders.some(f => f.parent_id === folderId && folderTreeHasUnread(f.id, visited))
  }

  // Inline failure notice for a folder-scoped create, rendered directly under
  // the folder's header row through the shared ErrorNotice surface (AUTOSDE
  // errors-use-error-notice): it carries the role="alert", the design tokens,
  // the dismiss affordance, and the agent hand-off. askAgent is on because the
  // hand-off destroys nothing here — the sidebar holds no unsaved draft (the
  // rename Input commits on blur) and survives the navigation. `columnId`
  // scopes board-view rendering to the column the create was issued from, so
  // a root folder repeated across columns announces ONE alert, under a
  // column-unique test id.
  // True when the tree lane will NOT render the folder's header (and so its
  // per-folder notice mount): the folder or an ancestor is hidden or filtered
  // out, and not currently revealed via the "N hidden folders" peek (whose
  // container key is the parent id, or 'root' at top level). Mirrors the
  // exclusion applied at visibleRootFolders / renderFolderBlock's child
  // filter, so the tree-lane fallback below renders exactly when the scoped
  // mount cannot.
  const folderCreateMountAbsent = (folderId: string): boolean => {
    let cur = folders.find(f => f.id === folderId)
    // A folder that no longer exists (deleted while its create was in flight)
    // has no header anywhere by definition — the strongest mount-absent case.
    if (!cur) return true
    const seen = new Set<string>()
    while (cur) {
      if (seen.has(cur.id)) break
      seen.add(cur.id)
      const revealed = revealedContainers.has(cur.parent_id || 'root')
      if ((isFolderHidden(cur) && !revealed) || isFolderFilteredOut(cur)) return true
      cur = cur.parent_id ? folders.find(f => f.id === cur!.parent_id) : undefined
    }
    return false
  }

  const renderFolderCreateError = (folderId: string, columnId?: string): React.ReactNode => {
    if (!folderCreateError || folderCreateError.folderId !== folderId) return null
    // Ownership: an exact columnId match wins (board columns each render the
    // folder, so scoping prevents N duplicate alerts). Outside board view the
    // single tree mount owns EVERY error for its folder — including one whose
    // columnId outlived its column or its view (user switched back to tree).
    const owns = folderCreateError.columnId === columnId || (columnId === undefined && !boardLaneActive)
    if (!owns) return null
    return (
      <div className="px-2 py-1">
        {/* inline variant with flex-wrap: the sidebar drawer is ~250px wide,
         *  and both stock single-row layouts squeeze the message to a sliver
         *  beside the Ask-agent / dismiss controls. Wrapping lets the message
         *  take the full line and the controls fold under it. */}
        <ErrorNotice
          message={folderCreateError.message}
          title={folderCreateError.title}
          report={folderCreateError.report}
          variant="inline"
          askAgent
          onDismiss={() => setFolderCreateError(null)}
          testId={columnId ? `col-${columnId}-folder-create-error-${folderId}` : `folder-create-error-${folderId}`}
          className="flex-wrap w-full"
        />
        {/* Direct remedy for the stale-directory case: open Folder settings
         *  right here instead of describing a hover-only menu glyph. On its
         *  own line, never as a row peer of the notice's Ask-agent/dismiss
         *  pair (the two-buttons-per-row cap) — same pattern as the
         *  PullRequestPanel remedy link. */}
        {folderCreateError.offerSettings && (
          <div className="mt-0.5">
            <button type="button"
              className="text-[11px] font-medium text-danger/80 hover:text-danger bg-transparent border-none p-0 cursor-pointer underline decoration-danger/30 hover:decoration-danger underline-offset-2"
              data-testid={`folder-create-error-settings-${folderId}`}
              onClick={() => { setFolderModal({ mode: 'edit', folderId }); setFolderCreateError(null) }}>
              {i18nT('components.folderConfigModal.folder_settings')}
            </button>
          </div>
        )}
      </div>
    )
  }

  /**
   * While the search box narrows the list, does this folder's subtree still put
   * ANYTHING on screen? Answered without rendering, because the render cannot
   * answer it: a nested block's `[]` is returned from inside the subfolder
   * wrapper's own render, which runs after `childNodes.push` has already committed
   * the wrapper — so `childNodes.length > 0` reads true for a subtree that draws
   * nothing, and both the drop gate and the header count believed it.
   *
   * That is not cosmetic. Searching "archive" kept `Sydney Property` on screen
   * wearing the count `1` — the `1` being a subfolder that did not render —
   * directly above the note saying no sessions matched. The row named nothing the
   * query asked for and the number contradicted the sentence beneath it.
   *
   * Each clause mirrors one thing `renderFolderBlock` actually draws, so the
   * predicate cannot drift from the render: own surviving sessions, the query
   * having named this folder (`folderNameMatchIds` already covers a matched
   * folder's whole subtree), the create-failure notice this folder owns, the
   * "N hidden folders" peek row filed in this container, and recursively any
   * child folder the tree is willing to draw. `visited` is the same cycle guard
   * `renderFolderBlock` carries, for the same reason: `parent_id` comes off disk.
   */
  const narrowedSubtreeShowsSomething = (folder: ChatFolder, visited = new Set<string>()): boolean => {
    if (visited.has(folder.id)) return false
    visited.add(folder.id)
    if (sidebarRows.folderTree.rowsIn(folder.id).length > 0) return true
    if (folderNameMatchIds?.has(folder.id)) return true
    if (folderCreateError?.folderId === folder.id) return true
    if (hiddenByContainer.get(folder.id)?.length) return true
    return folders.some(f => f.parent_id === folder.id
      && !isFolderHidden(f) && !isFolderFilteredOut(f)
      && narrowedSubtreeShowsSomething(f, visited))
  }

  /**
   * The child folders this container will draw, in the order it draws them — the
   * narrow's verdict included. Sorted, not raw array order: a subfolder's `order`
   * is set by a drag AND by chat_folder_move's before/after, and the cache order
   * reflects neither.
   */
  const drawableChildFolders = (folder: ChatFolder): ChatFolder[] =>
    folders.filter(f => f.parent_id === folder.id
      && !isFolderHidden(f) && !isFolderFilteredOut(f)
      && (!listNarrowed || narrowedSubtreeShowsSomething(f)))
      .sort(folderCompare)

  // The folder's action items, rendered once for BOTH surfaces that open them:
  // the row's ⋯ button (a DropdownMenu) and a right-click on the row (a Radix
  // ContextMenu, which positions itself at the pointer). Radix menu items only
  // work inside their own primitive family, so the family is picked from
  // `variant` -- the same shape SessionActionsMenu and FolderMoveSubmenu use.
  // `data-testid`s carry a `-ctx` suffix in the context variant so a test can
  // tell the two copies apart when both are mounted for one folder.
  const renderFolderMenuItems = (folder: ChatFolder, reparentTargets: readonly ChatFolder[], variant: 'dropdown' | 'context') => {
    const ctx = variant === 'context'
    const Item = ctx ? ContextMenuItem : DropdownMenuItem
    const Separator = ctx ? ContextMenuSeparator : DropdownMenuSeparator
    const tid = (name: string) => `folder-${name}-${folder.id}${ctx ? '-ctx' : ''}`
    const privateChatRows = (
      <>
        {/* Menu create entries take NO open-in-tab gesture (#10575,
         *  scoped out): a menu closes on select, and Radix keyboard
         *  activation synthesizes a modifier-free click, so the
         *  gesture would be mouse-only and undiscoverable. */}
        <Item className="items-start" data-testid={tid('new-incognito')} onClick={() => { createChatInFolder(folder.id, { memoryMode: 'incognito' }) }}>
          <PrivateChatRow kind="incognito" />
        </Item>
        <Item className="items-start" data-testid={tid('new-temporary')} onClick={() => { createChatInFolder(folder.id, { memoryMode: 'temporary' }) }}>
          <PrivateChatRow kind="temporary" />
        </Item>
      </>
    )
    // The conductor's own chat, reachable from the folder row instead of from an
    // expand-and-scan. The ROW now carries a button for it, beside the ⋯ menu
    // and new-chat: that puts it over the two-controls-per-row cap, which is
    // exceeded deliberately for this one case because the folder row is where a
    // reader is looking when they want that conversation. The excess is bounded
    // to a folder that resolves a conductor; every other row stays capped.
    //
    // This menu entry stays, and is not a fallback for the button. It is the
    // right-click path -- a reader who opens the row's context menu finds the
    // same action there, named identically -- and the keyboard path on a device
    // that reveals the hover cluster only on focus. Both menus render from here,
    // so the ⋯ menu and the right-click menu cannot drift apart.
    //
    // A folder whose rows resolve no conductor gets nothing added, in the menu
    // or on the row: the session cards and the folder body are what they are on
    // main.
    //
    // The label names the FOLDER and the second line says what the chat it
    // opens is to that folder. A label carrying the word "conductor" and
    // nothing else left a first-time reader unable to say what the click opens,
    // and the two other chat affordances on the same row made that worse rather
    // than clearer. The two-line shape is the one the ephemeral-chat items in
    // this file already use.
    //
    // It names the folder rather than the conductor's own title because the
    // folder is what the reader has in front of them: they opened this menu on
    // that row. The conductor's title is a session name they have not
    // necessarily seen, and reading one in a menu hung off a different name made
    // the item look like it belonged to something else.
    const conductor = folderConductorOf(folder.id)
    return (
      <>
        {conductor && (
          <Item className="items-start" data-testid={tid('conductor-chat')} onClick={() => {
            // The same offline gate every session-row click has (see the row's
            // own `onClick`/`onKeyDown`). Switching slots while the backend is
            // down selects a session nothing can load, so the click would close
            // the menu onto an empty pane with no way back to the row.
            if (!connected) return
            dispatch(switchSlot({ key: conductor.key, announceOnMissing: true }))
            onSelectSlot?.(conductor.key)
          }}>
            <CornerUpRight size={14} className="text-accent mt-[3px] shrink-0" />
            <span className="flex min-w-0 flex-col gap-px">
              <span className="truncate">{i18nT('pages.chatSidebar.chat_with_folder', { folder: folderNameText(folder) })}</span>
              <span className="whitespace-normal text-[11px] leading-snug text-muted">{i18nT('pages.chatSidebar.chat_with_folder_desc')}</span>
            </span>
          </Item>
        )}
        <Item data-testid={tid('rename')} onClick={() => { suppressMenuRestoreRef.current = true; setEditingId(folder.id); setEditScope('list'); setEditName(folder.name) }}><Pencil size={13} /> {i18nT('pages.chatSidebar.rename')}</Item>
        <Item data-testid={tid('new-subfolder')} onClick={() => { setFolderModal({ mode: 'create', parentId: folder.id }) }}><FolderPlus size={13} /> {i18nT('pages.chatSidebar.new_subfolder')}</Item>
        {/* The two private modes are listed flat, each naming its own action
         *  and what it does with memory. A grouping caption read as a button
         *  at phone width and named the pair with a word no other surface
         *  uses, so neither a caption nor a submenu stands between the user
         *  and the choice. */}
        <Separator />
        {privateChatRows}
        <Separator />
        {/* Re-parent: move this folder under another folder or back to the
         *  top level. Self + descendants are excluded (cycle guard). */}
        <FolderMoveSubmenu variant={variant} label={i18nT('pages.chatSidebar.move_folder_to')} sortMode={folderSortMode}
          folders={reparentTargets}
          currentFolderId={folder.parent_id || null}
          onPick={pid => moveFolderTo(folder.id, pid)} />
        <Item data-testid={tid('settings')} onClick={() => { setFolderModal({ mode: 'edit', folderId: folder.id }) }}><Settings size={13} /> {i18nT('components.folderConfigModal.folder_settings')}</Item>
        {/* Hide this folder from the session lists (flat lane + tree).
         *  Same state the filter menu's checkboxes drive, reached from the
         *  folder itself — which is where the user is looking when they
         *  decide a folder is noise. Distinct from "Hide when empty"
         *  below, which is a server-persisted archive affordance. */}
        <Item data-testid={tid('visibility')} onClick={() => { toggleFolderFilter(folder.id) }}>
          {filterHiddenFolders.has(folder.id)
            ? <><Eye size={13} /> {i18nT('pages.chatSidebar.show_folder')}</>
            : <><EyeOff size={13} /> {i18nT('pages.chatSidebar.hide_folder')}</>}
        </Item>
        {folderOffersHide(folder, foldersWithActiveSubtree) && (
          <Item data-testid={tid('hide')} onClick={() => { updateFolderMutation.mutate({ id: folder.id, body: { hidden: true } }) }}><EyeOff size={13} /> {i18nT('pages.chatSidebar.hide_when_empty')}</Item>
        )}
        <Separator />
        <Item className="text-danger focus:text-danger" data-testid={tid('delete')} onClick={() => { if (confirm(i18nT('pages.chatSidebar.delete_folder_confirm', { name: folder.name }))) deleteFolderMutation.mutate(folder.id) }}><X size={13} /> {i18nT('pages.chatSidebar.delete_folder')}</Item>
      </>
    )
  }

  const renderFolderHeader = (folder: ChatFolder, dragHandleProps?: React.HTMLAttributes<HTMLElement>, emptyBody = false, depth = 0) => {
    // Same predicate `renderFolderBlock` renders by, so the number describes what
    // the row can actually show. Counting a hidden-when-empty child made the count
    // and the body disagree: the body skipped it, so no body rendered, while the
    // count still said 1 - and the row then presented as a toggle with nothing to
    // toggle. A folder the user hid is a folder they asked not to see, so it is
    // not part of what this row holds.
    //
    // `drawableChildFolders` carries the narrow's verdict for the same reason: with
    // a search active, a child whose whole subtree draws nothing is not part of
    // what this row holds either, and counting it printed a number the body below
    // could not account for.
    const childFolders = drawableChildFolders(folder)
    // The row model's folder index: a peer row is never in a folder, so a peer key
    // colliding with a local one cannot count a session this machine does not own
    // toward the folder it does.
    const childSlots = sidebarRows.folderTree.rowsIn(folder.id)
    const count = childSlots.length + childFolders.length
    const collapsed = !!folder.collapsed
    // One derivation, not two: `renderFolderBlock` decides the body from the nodes
    // it renders, and this row follows that decision. With the count now built
    // from the same predicate, `count === 0` agrees with it by construction rather
    // than by a guard that had to pick which way to fail.
    const emptyRow = emptyBody
    // `button` when the row toggles something, a plain `span` when it does not.
    const HeaderShell = (emptyRow ? 'span' : 'button') as 'button'
    const hasUnread = folderTreeHasUnread(folder.id)
    const draggable = !!dragHandleProps && editingId !== folder.id
    // Valid "Move folder to" destinations: everything outside this folder's
    // own subtree (cycle guard). One O(1) lookup, computed once per row.
    const subtreeIds = folderSubtrees.get(folder.id) ?? collectFolderSubtreeIds(folders, folder.id)
    const reparentTargets = folders.filter(f => !subtreeIds.has(f.id))
    // Reveal confirmation for THIS folder. The `kind` check is what keeps a
    // session reveal from lighting up a folder whose id equals that slot key.
    const folderFlash = revealFlash?.kind === 'folder' && revealFlash.key === folder.id
      ? (revealFlash.fading ? 'fade' : 'flash')
      : null
    // The row's own chat control, or null for a folder that resolves no
    // conductor. The SAME selector the menu item asks, so the button and the
    // menu entry can never offer different sessions -- and null here means the
    // cluster emits its two controls and nothing else.
    const folderRowConductor = folderConductorOf(folder.id)
    return (
      // Right-click (or long-press) anywhere on the row opens the SAME menu the
      // ⋯ button does, positioned at the pointer. The ⋯ button stays: it is the
      // keyboard-reachable path, and the only one on a device with no secondary
      // button. Rename mode opts out so a right-click in the name input keeps the
      // browser's own edit menu (cut/paste).
      <ContextMenu key={`folder-header-${folder.id}`}>
        <ContextMenuTrigger asChild disabled={editingId === folder.id && editScope === 'list'}>
      <div
        // The reveal target for this folder (command palette Folders tab), and the
        // only marker that identifies a folder ROW. Deliberately not the existing
        // `data-folder-drop`: that one is a drop zone and is rendered once per
        // BOARD COLUMN as well as here, so `querySelector` would return whichever
        // copy sorts first in the DOM — the same ambiguity the session reveal
        // avoids by targeting `data-session-row` instead of `data-slot-key`.
        data-folder-row={folder.id}
        // Non-interactive container (role="group"): the row holds a collapse
        // toggle button + action buttons, so it must NOT itself be a button —
        // an interactive element can't legally contain other interactive
        // elements (invalid ARIA), and a folder row is a grouping, not an action.
        role="group"
        aria-label={i18nT('pages.chatSidebar.folder_2', { name: folder.name })}
        // The whole header is the drag-to-reorder handle (pointer listeners only,
        // no role override). 8px activation distance keeps the collapse toggle
        // and action buttons clickable; drag is off while renaming.
        {...(draggable ? dragHandleProps : {})}
        // `pl-[3px]` (3px), with no inline left-pad override. Deliberately LESS than
        // the session rows' pad, so the glyph outdents into the row gutter while the
        // folder name lands on its sessions' text. The pad is NOT free: the guides
        // below fix it, so nothing else may claim it. That is why the unread dot
        // sits inline on the right, not in an absolutely-positioned left gutter
        // (a gutter wide enough for it would break guide 3).
        //
        // With H = this header's box left, D = `FOLDER_BODY_INSET_PX` 2 — the
        // nested body's own left inset, applied by `FolderBody` so its collapse
        // animation does not clip. It is invisible in the class list, which is
        // exactly why four revisions derived this geometry from Tailwind classes
        // and each landed 2px out. It is now a named, exported constant that the
        // alignment test imports and asserts against the rendered padding, so it
        // is no longer a free empirical term.
        // P = this `px-3.5` 14,
        // G = glyph 14, g = `gap-[5px]` 5, M = body `ml-3` 12, B = 1px border,
        // p = body `pl-1` 4, R = session row `pl-3.5` 14 (the same pad in the root
        // lane and inside every folder body):
        //
        //   GUIDE 1  glyph sits ON the connector line        P = D + M
        //   GUIDE 2  name == agent / title / tool-call sub   P + G + g = D + M + B + p + R
        //   GUIDE 3  nested glyph == sibling content column  P = R
        //
        //   14 = 2 + 12      14 + 14 + 5 = 2 + 12 + 1 + 4 + 14      14 = 14
        //
        // Each nesting level costs D + M + B + p = 19px. This is the maintainer's
        // decided geometry (#3905; docs/decisions/2026-10-09-folder-glyph-sits-on-
        // its-connector-line.md). A tighter indent that outdents the glyph off its
        // connector line (#14717) was reverted by that decision: the glyph on the
        // line is the picture, and width for titles is not bought by moving it.
        //
        // All three hold at EVERY depth and in the root lane: the algebra has no
        // per-depth term, so depth 3 nests exactly as depth 2 does.
        //
        // Measured on the built SPA (x in CSS px), NOT derived — a paper estimate
        // of these same numbers was 3px out: root glyph 248, root-lane session
        // content 255; depth 1 glyph 258, name and all three text lines 264;
        // depth 2 name/content 274; depth 3 content 284.
        //
        // Computing these guides from class names without D breaks them (moving
        // the name past the content, or leaving a nested glyph short). Re-measure with
        // `website/scripts/capture-folder-glyph.mjs` under MEASURE=1 — never
        // re-derive on paper.
        // A row with no body does not light up on hover. The highlight is this
        // sidebar's "this row is pressable" signal, and a row that toggles nothing
        // wearing the same one is the whole reason the previous round's dead click
        // read as broken. Its cluster is already visible at rest, so hover has
        // nothing left to reveal here either.
        // Pinned to the top of the lane while any of its folder is on screen: the
        // header is `sticky` inside its own folder block (the drop container that
        // holds header + body), so it rides the top edge until the block's end
        // pushes it off, and the next folder's header takes over. A nested header
        // pins one row lower per depth so the whole ancestor path stays readable,
        // and a shallower header paints above a deeper one as it is pushed out.
        // The opaque surface and the row height live in index.css
        // (`.folder-row-sticky`); `sticky` also serves as the containing block
        // the old `relative` provided for the absolutely-positioned children.
        style={{ top: `calc(var(--folder-row-sticky-h) * ${depth} - var(--folder-row-sticky-inset))`, zIndex: FOLDER_ROW_STICKY_Z - depth }}
        className={`folder-row folder-row-sticky group sticky flex items-center gap-2 px-3.5 py-1.5 rounded-md text-sm text-muted transition-all${emptyRow ? '' : ' hover:text-text hover:bg-bg-hover'} ${draggable ? 'cursor-grab active:cursor-grabbing' : ''}${folderFlash ? ` session-reveal-flash${folderFlash === 'fade' ? ' session-reveal-flash-fade' : ''}` : ''}`}>
        {editingId === folder.id && editScope === 'list' ? (
          <>
            <FolderGlyph color={folder.color} icon={folder.icon} size={14} open={!collapsed} />
            <Input ref={folderEditInputRef} className="flex-1 py-0.5 text-[13px] min-w-0" value={editName} onChange={e => setEditName(e.target.value)} onClick={e => e.stopPropagation()} onMouseDown={e => e.stopPropagation()} {...ime.bindEnter<HTMLInputElement>({ onEnter: () => renameCommit(folder.id, editName), onEscape: () => setEditingId(null), onBlur: () => renameCommit(folder.id, editName) })} />
            <span className="text-[11px] text-muted tabular-nums shrink-0">{count}</span>
          </>
        ) : (
          <>
            {/* The collapse toggle is the real interactive control — a native
             *  <button> (keyboard-operable for free), filling the row so clicking
             *  the folder glyph/name still toggles.  Double-click the name renames. */}
            {/* A folder with no body has nothing to toggle, so on an empty row this
             *  is not a control at all: no button role, no tab stop, no pointer
             *  cursor, no expanded state to announce and no handler. Keeping the
             *  <button> and neutering its handler is the worst of the options - a
             *  focusable control that looks clickable and does nothing. The name
             *  still double-click renames and the row's own cluster still creates
             *  and opens the menu, so the row keeps every action it can honour. */}
            <HeaderShell
              className={`flex items-center gap-[5px] flex-1 min-w-0 bg-transparent border-none text-left text-inherit p-0${emptyRow ? '' : ' cursor-pointer'}`}
              {...(emptyRow
                // Nothing to disclose, but the row is still a DRAG HANDLE while it
                // is draggable, and this shell is the only focusable thing inside
                // it: the sortable `listeners` sit on the row div, which has no tab
                // stop of its own, so keyboard activation reaches them by bubbling
                // from here. Swapping the <button> for a <span> without this took
                // keyboard reordering away from empty folders in the list view -
                // the same hole the board row had, through a different door.
                ? (draggable ? {
                  role: 'button',
                  tabIndex: 0,
                  'aria-label': i18nT('pages.chatSidebar.folder_2', { name: folder.name }),
                  'aria-roledescription': i18nT('pages.chatSidebar.drag_to_reorder'),
                } : {})
                : {
                type: 'button' as const,
                'aria-expanded': !collapsed,
                'aria-label': collapsed ? i18nT('pages.chatSidebar.expand_folder_name', { name: folder.name }) : i18nT('pages.chatSidebar.collapse_folder_name', { name: folder.name }),
                onClick: (e: React.MouseEvent<HTMLElement>) => toggleListFolderCollapse(folder, e.currentTarget),
              })}>
              {/* An inert row's glyph says "inactive" by WEIGHT, not by shape. The
               *  closed shape is this product's "collapsed, click to expand"
               *  affordance, so drawing it on a row that toggles nothing invites
               *  exactly the dead click it was meant to prevent; the open shape
               *  invites no click, and "contents shown below" is not a lie when
               *  there are none. So the shape stays open and the glyph instead goes
               *  dimmer and stops brightening on hover, which every pressable
               *  sibling does. Same icon, same box: the alignment guides that key
               *  off this glyph's geometry are untouched. */}
              {/* `|| emptyRow` is the point, not a tidy-up: a folder that was
               *  collapsed BEFORE it emptied still carries `collapsed: true`, and
               *  binding the glyph to that alone would draw the closed shape on an
               *  inert row - this product's "click to expand" affordance on a row
               *  that cannot expand. An inert row is always drawn open. */}
              <FolderGlyph color={folder.color} icon={folder.icon} size={14} open={!collapsed || emptyRow}
                className={emptyRow ? 'shrink-0 text-muted/40 transition-colors' : undefined}
                testId={`folder-collapse-${folder.id}`} />
              {/* Double-click rename is a mouse-only power shortcut; the accessible
               *  path is the ⋯-menu Rename item, so scope-disable the interaction rule. */}
              {/* The matched letters are marked while the search box narrows the list.
               *  Without it a folder row surfaced by a NAME match carries no cue at
               *  all: the row for "Sydney Property" on a search for "archive" (its
               *  subfolder) is indistinguishable from one whose own name matched, so
               *  the lane reads as arbitrary. The launcher already marks its matched
               *  letters, and this is the same signal in the surface the user was
               *  looking at. `highlightText` returns the plain string when the term
               *  is empty or absent, so ancestor and subtree rows — which have
               *  nothing to mark — are untouched, and that difference is itself the
               *  cue: the marked row is the one that explains the result. */}
              {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
              <span className="flex-1 text-[13px] font-medium text-text truncate text-left" title={i18nT('pages.chatSidebar.double_click_to_rename')} onDoubleClick={e => { e.stopPropagation(); setEditingId(folder.id); setEditScope('list'); setEditName(folder.name) }}>{highlightText(folderNameText(folder), slotFilter.trim(), false, -1)}</span>
              {/* Channel-owned folder (created by per-channel session filing):
               *  show the channel's brand mark so the folder reads as "these are
               *  the Discord conversations" at a glance. Guarded the same way the
               *  session rows are — a channel with no brand asset shows nothing
               *  rather than ChannelBrandIcon's generic Link2 fallback, which
               *  means "live mirroring" elsewhere in this sidebar. */}
              {folder.channel && hasChannelBrandIcon(folder.channel) && (
                <span className="shrink-0 opacity-80" aria-hidden><ChannelBrandIcon channel={folder.channel} size={11} /></span>
              )}
              {folder.project_dir && <span className="text-[10px] text-accent/60 shrink-0" title={folder.project_dir}><Link2 size={9} /></span>}
              {/* Unread dot on the RIGHT, inline before the count — a state marker
               *  reading after the text, not a gutter marker. The header's left pad
               *  is load-bearing for the alignment guides, so the dot stays where it
               *  does not compete with it. Only when collapsed: an expanded folder's
               *  child rows carry their own markers. */}
              {hasUnread && collapsed && (
                // Carries the same accessible name as a session row's unread
                // marker, and the SAME i18n key: a colour-only dot is invisible to
                // a screen reader and indistinguishable from decoration, and this
                // one sits beside a count where that reads as styling. Like the
                // session row's marker, it is `role="img"` with that label.
                // `--ok` for the same reason as the session row's dot: it is the
                // SAME unread state rolled up, so it reads the same semantic
                // status token rather than the brand accent, matching the
                // `recent` filter and the connection-status dot.
                <span className="w-2 h-2 rounded-full shrink-0" style={{ background: 'var(--ok)' }}
                  role="img"
                  aria-label={i18nT('pages.chatSidebar.agent_finished_your_turn')}
                  title={i18nT('pages.chatSidebar.agent_finished_your_turn')} />
              )}
              <span className="text-[11px] text-muted tabular-nums shrink-0">{count}</span>
            </HeaderShell>
            {folder.default_agent && <span className="text-[10px] text-accent bg-accent/10 px-1.5 py-0.5 rounded-full shrink-0 truncate max-w-[60px]" title={i18nT('pages.chatSidebar.default_agent', { name: folder.default_agent })}>{folder.default_agent}</span>}
          </>
        )}
        {/* An empty folder's row is otherwise a dead end: hiding the body took
          *  away the only control it had, and the closed glyph alone does not say
          *  the row can be opened or created in. So the row's own action cluster
          *  stops hiding on an empty folder — it already holds exactly the two
          *  controls that row needs (create, and the ⋯ menu whose rename/delete
          *  is what an empty folder usually wants), and an empty folder holds no
          *  sessions, so it resolves no conductor and the third control below is
          *  never emitted on it. */}
        {!(editingId === folder.id && editScope === 'list') && (
        <div className={`transition-all flex items-center gap-0.5 rounded-md group-focus-visible:opacity-100 group-has-[:focus-visible]:opacity-100 has-[[data-state=open]]:opacity-100${emptyRow ? ' shrink-0 -my-1' : ' absolute top-1/2 -translate-y-1/2 right-1.5 p-1 bg-card border border-border shadow-sm opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 [@media(hover:none)]:static [@media(hover:none)]:translate-y-0 [@media(hover:none)]:shrink-0 [@media(hover:none)]:-my-1 [@media(hover:none)]:p-0 [@media(hover:none)]:bg-transparent [@media(hover:none)]:border-transparent [@media(hover:none)]:shadow-none'}`}>
          {/* ⋯ menu first, then the primary "new chat" action.  Sibling
           *  <button>s of the collapse toggle (valid ARIA — no nesting). */}
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button type="button" className="cursor-pointer p-[4px] rounded text-muted hover:text-text hover:bg-bg-hover transition-all bg-transparent border-none" title={i18nT('pages.chatSidebar.more')} aria-label={i18nT('pages.chatSidebar.folder_options_for', { name: folder.name })} aria-haspopup="menu" data-testid={`folder-menu-${folder.id}`} onMouseDown={e => { e.stopPropagation() }}><MoreVertical size={12} /></button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="min-w-[180px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
              {renderFolderMenuItems(folder, reparentTargets, 'dropdown')}
            </DropdownMenuContent>
          </DropdownMenu>
          {/* Same three-gesture contract as the header New button: plain click
           *  creates and switches; Cmd/Ctrl-click and middle-click create the
           *  session as a background TAB. Gated on `onOpenSlotInNewTab` --
           *  embedded hosts have no tab strip, so the modifier is ignored. */}
          <button type="button" data-testid={`folder-new-chat-${folder.id}`} className="cursor-pointer p-[4px] rounded text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none" title={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} aria-label={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })}
            onMouseDownCapture={onOpenSlotInNewTab ? (e => { if (e.button === 1) e.preventDefault() }) : undefined}
            onAuxClick={onOpenSlotInNewTab ? (e => {
              if (e.button !== 1) return
              e.preventDefault()
              e.stopPropagation()
              createChatInFolder(folder.id, { inNewTab: true })
            }) : undefined}
            onClick={e => { e.stopPropagation(); createChatInFolder(folder.id, { inNewTab: !!onOpenSlotInNewTab && isOpenInTabModifierClick(e) }) }}><MessageSquarePlus size={12} /></button>
          {/* Third control: the conductor's own chat, on the row itself.
           *
           *  This is the one place in the sidebar over the two-controls-per-row
           *  cap (`max-two-buttons-per-row`, `website/AUTOSDE.yaml`), and it is
           *  over it deliberately rather than by oversight: the folder row is
           *  where a reader is looking when they want that conversation, and
           *  sending them through the menu for it was judged the wrong trade.
           *  The excess is BOUNDED to this one case -- a folder that resolves a
           *  conductor -- and the cap governs every other row in the product.
           *
           *  Rendered ONLY when a conductor resolves. A folder with none keeps
           *  the two-control cluster exactly as it is on `main` -- the element
           *  below is simply not emitted, so nothing about that row's geometry
           *  or markup is conditional on this feature.
           *
           *  NOT gated on `folderConductorPin`: this ships always-on, while the
           *  flag holds the pinned LAYOUT, which re-orders a list
           *  people already read; a button on a row is an ingress they can
           *  decline, and holding it would leave the row with no way to the
           *  conversation it is drawn around.
           *
           *  `title` AND `aria-label` carry the same string the menu item shows,
           *  so the action is named identically wherever it is reached and a
           *  screen reader gets it without hover.
           *
           *  The glyph is an ARROW, not a speech bubble. Two bubble glyphs were
           *  tried here and both failed the same way: new-chat's neighbouring
           *  `MessageSquarePlus` is a bubble, so a blind UX read of the hovered
           *  row came back "purple speech bubble with no plus ... I'm not sure,
           *  nothing tells me what it does". `CornerUpRight` shares no outline
           *  with it at 12px, and it points the right way: the conductor sits
           *  ABOVE the chats it opened, so "up and over to that one" is what the
           *  click does. It is deliberately the mirror of the `CornerDownRight`
           *  this file already draws for a row nested UNDER its creator.
           *
           *  And the glyph does not carry the meaning alone -- that was the rest
           *  of the same finding, since an icon-only control keeps its name in
           *  `title`, which is OS chrome a reader may never see. So the button
           *  shows the label too, the SAME `chat_with_folder` string, truncated:
           *  no new wording and no fourth name for one action. The cluster is an
           *  absolutely-positioned overlay on hover, so the text costs the row
           *  no width at rest; on a touch device, where the cluster is static and
           *  inline and width is scarcest, the label is dropped and the menu
           *  item remains the named path. */}
          {folderRowConductor && (
            <button type="button" data-testid={`folder-conductor-chat-row-${folder.id}`}
              className="group/conductor-chat relative cursor-pointer p-[4px] rounded text-accent hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none"
              title={i18nT('pages.chatSidebar.chat_with_folder', { folder: folderNameText(folder) })}
              aria-label={i18nT('pages.chatSidebar.chat_with_folder', { folder: folderNameText(folder) })}
              onMouseDown={e => { e.stopPropagation() }}
              onClick={e => {
                e.stopPropagation()
                // The same body the menu item runs, including the offline gate
                // every session click has: switching to a session nothing can
                // load would leave an empty pane with no way back to the row.
                if (!connected) return
                dispatch(switchSlot({ key: folderRowConductor.key, announceOnMissing: true }))
                onSelectSlot?.(folderRowConductor.key)
              }}>
              <CornerUpRight size={12} className="shrink-0" />
              {/* The name, on screen, in the page.
               *
               *  `title` alone was the finding: it is OS chrome, a reader may
               *  never see it, and no screenshot can show it. An inline label
               *  was tried first and rejected on the rendered pixels -- at this
               *  sidebar's width the string made the hover overlay 136px of a
               *  242px row and covered the folder's own name down to two
               *  letters, so the control explained itself by hiding what it
               *  was pointing at.
               *
               *  So it floats instead: out of flow, under the button, revealed
               *  on hover AND on keyboard focus. It costs the row no width at
               *  any width, it occludes nothing at rest, and it carries the
               *  SAME `chat_with_folder` string as the tooltip and the menu
               *  item, so one action never picks up a second name.
               *
               *  `aria-hidden`, because the button's own `aria-label` already
               *  carries this string and a screen reader must not say it twice.
               *  `pointer-events-none`, so the panel cannot eat the click it
               *  is describing.
               *
               *  `[@media(hover:none)]:hidden` is DECLARATIVE, not behavioural,
               *  and must not be deleted as dead: Tailwind gates `hover:` behind
               *  `@media (hover: hover)`, so on a touch screen the reveal never
               *  fires and the panel is already hidden. What the class adds is
               *  the statement that this is intended -- `hoverVariantPolicy`
               *  scans for anything hidden-until-hover and fails the build
               *  unless the touch state is spelled out on the element or the
               *  site is listed as decoration. It belongs here rather than in
               *  that allowlist because the panel is a label and not a control:
               *  on touch the cluster is static and inline, where width is
               *  scarcest, and the same action is still reached by name through
               *  the folder's menus. */}
              <span aria-hidden="true"
                className="pointer-events-none absolute top-full right-0 mt-1 hidden group-hover/conductor-chat:block group-focus-visible/conductor-chat:block [@media(hover:none)]:hidden whitespace-nowrap rounded-md border border-border bg-card px-2 py-1 text-[11px] font-medium leading-none text-text shadow-md z-20">
                {i18nT('pages.chatSidebar.chat_with_folder', { folder: folderNameText(folder) })}
              </span>
            </button>
          )}
        </div>
        )}
        {/* During a session drag, replace an expanded parent's normal presentation
          *  with one explicit destination and extend its hit area 8px into the
          *  first child row. Keeping the overlay INSIDE the sticky header makes
          *  it follow pinned parents without inserting layout; all remaining
          *  child-row pixels and the child body keep their own droppable. A
          *  session already filed directly here gets no no-op target. */}
        {activeDrag?.type === 'session' && !collapsed && childFolders.length > 0
          && slotFolders[activeDrag.id] !== folder.id
          && <ParentFolderDropTarget folder={folder} depth={depth} />}
      </div>
        </ContextMenuTrigger>
        <ContextMenuContent data-testid={`folder-context-menu-${folder.id}`} className="min-w-[180px]" onClick={e => e.stopPropagation()} onCloseAutoFocus={onMenuCloseAutoFocus}>
          {renderFolderMenuItems(folder, reparentTargets, 'context')}
        </ContextMenuContent>
      </ContextMenu>
    )
  }

  // One row announcing the folders this container is hiding, rendered at the
  // BOTTOM of that container's folder list and indented to its depth. Peeking it
  // open renders those folders' real blocks (dimmed), so every normal
  // affordance — including ⋯ → Show folder, the durable undo — still works.
  // `containerKey` is 'root' | 'flat' | parent folder id.
  const renderHiddenReveal = (containerKey: string, hidden: readonly ChatFolder[], depth: number): React.ReactNode => {
    if (hidden.length === 0) return null
    const open = revealedContainers.has(containerKey)
    const n = hidden.length
    return (
      <div key={`hidden-reveal-${containerKey}`} data-testid={`hidden-reveal-${containerKey}`}>
        <button
          type="button"
          onClick={() => toggleReveal(containerKey)}
          aria-expanded={open}
          title={open ? i18nT('pages.chatSidebar.collapse_hidden_folders') : i18nT('pages.chatSidebar.show_hidden_folder', { count: n })}
          data-folder-hidden-reveal=""
          className="w-full flex items-center gap-1.5 py-1 pl-3.5 pr-2 text-left text-[11px] text-muted hover:text-text hover:bg-accent-subtle rounded-md cursor-pointer bg-transparent border-none transition-colors"
        >
          <DisclosureChevron open={open} size={11} />
          <span>{i18nT('pages.chatSidebar.hidden_folder_count', { count: n })}</span>
        </button>
        {open && (
          <div className="opacity-70">
            {hidden.map(f => (
              <Fragment key={`revealed-${f.id}`}>{renderFolderBlock(f, depth)}</Fragment>
            ))}
          </div>
        )}
      </div>
    )
  }

  const renderFolderBlock = (folder: ChatFolder, depth: number, visited = new Set<string>(), dragHandleProps?: React.HTMLAttributes<HTMLElement>, forceCollapsed = false): React.ReactNode[] => {
    if (depth > 10 || visited.has(folder.id)) return []
    visited.add(folder.id)
    const allChildSlots = sidebarRows.folderTree.rowsIn(folder.id)
    /**
     * The one row this folder is drawn AROUND, or null.
     *
     * A conducted folder is one conversation that opened the rest, and reading
     * it off the lane meant picking that card out of a time-sorted list where it
     * sits wherever it last spoke, drawn exactly like the sessions it opened. So
     * it is lifted to the top of the folder and marked, and the folder row plus
     * that card read as the one thing they are.
     *
     * Null for an ordinary folder AND whenever the preview flag is off, and
     * `allChildSlots` is then handed on UNTOUCHED -- same array, same order,
     * same nodes. That is the whole fallback: nothing about an ordinary render
     * is conditional on this feature, so the lane tens of thousands of people
     * already read is unchanged for them.
     *
     * `folderConductorOf`, the same selector the folder menu's item asks, so
     * the pin and the item can never name different sessions: it detects over
     * the folder's unfiltered membership and then requires the row to be one
     * this folder draws. Null while a filter conceals the root, which is also
     * why the row it returns is always one of `allChildSlots` below.
     */
    const folderConductorSlot = folderConductorPin ? folderConductorOf(folder.id) : null
    // What the body draws: every row in the folder except the pinned one. The
    // folder HEADER's count still reads `rowsIn` and so still counts the
    // conductor -- it says how many sessions are filed here, and that is still
    // all of them.
    const childSlots = folderConductorSlot == null
      ? allChildSlots
      : allChildSlots.filter(s => s !== folderConductorSlot)
    const childNodes: React.ReactNode[] = []
    // Nested subfolders are sortables, exactly as root folders are: dragging one
    // either re-orders it among its siblings (drop on a sibling's edges or body)
    // or re-parents it (drop on the middle band of another folder's header, or on
    // the root lane to move it to the top level). Both gestures are the ones the
    // root lane already has -- see SortableSubfolderBlock for why a nested row
    // could previously only re-parent. The subtree ids ride along in the drag data
    // so collision detection can exclude self/descendants as targets, and the
    // sibling ids so a reorder cannot resolve into another container.
    //
    // `drawableChildFolders`, not a raw parent_id filter: while the list is
    // narrowed a child whose subtree draws nothing must be skipped HERE, before
    // the push. The nested render returns `[]` from inside the wrapper's own
    // render, which runs long after this push, so a wrapper committed now can
    // never be taken back -- and `childNodes.length` is what the drop gate below
    // and the header count both read.
    //
    // One SortableContext per PARENT, holding exactly that parent's drawable
    // children: `order` is a per-container index, so the ring a drag may move
    // within is one container's children and nothing else. It renders no DOM of
    // its own, so the row geometry the alignment guides pin is untouched.
    const childFolderRows = drawableChildFolders(folder)
    if (childFolderRows.length) {
      const siblingIds = childFolderRows.map(f => f.id)
      childNodes.push(
        <SortableContext key={`subfolder-ring-${folder.id}`} items={siblingIds} strategy={verticalListSortingStrategy}>
          {childFolderRows.map(cf => (
            <SortableSubfolderBlock key={`subfolder-drag-${cf.id}`} folder={cf} reorderable={folderReorderable} dragWithheld={folderDragWithheld}
              depth={depth + 1} visited={visited}
              subtree={[...(folderSubtrees.get(cf.id) ?? collectFolderSubtreeIds(folders, cf.id))]}
              siblings={siblingIds}
              disabled={editingId === cf.id}
              renderFolderBlock={renderFolderBlock} />
          ))}
        </SortableContext>
      )
    }
    // Bottom of THIS container's folder list: announce what the filter is
    // hiding here, at this depth. Sits after the sibling folders and before the
    // new-subfolder input, so it reads as part of the folder list.
    const hiddenHere = hiddenByContainer.get(folder.id)
    if (hiddenHere?.length) childNodes.push(renderHiddenReveal(folder.id, hiddenHere, depth + 1))
    const { fresh: freshChildSlotsRaw, stale: staleChildSlots } = splitStale(childSlots)
    // Stale rows are collapsed into their own section and are stale precisely
    // because nothing is bumping them, so only the live list needs the hold.
    const { rows: freshChildSlots, navScope: treeChildScope, container: treeChildContainer } = heldLane(freshChildSlotsRaw, 'list', `tree:folder:${folder.id}`)
    /**
     * The pinned conductor card: the folder body's FIRST child, before the
     * sibling folders and before the sorted rows.
     *
     * INSIDE `FOLDER_BODY_CLS`, which is load-bearing. An earlier revision drew
     * it outside that div to put it at the folder row's own left edge, and that
     * outdented its text 15px left of the folder name -- breaking the shared
     * edge recorded in
     * docs/decisions/2026-10-09-folder-glyph-sits-on-its-connector-line.md,
     * whose guide 2 is that a folder name and the agent label of every session
     * filed inside it sit on one column. Drawn here the card keeps that column:
     * it is a row in the body like any other, so no guide moves and there is no
     * per-depth term to get wrong.
     *
     * So the pin is POSITION ONLY. `childNodes.unshift` below puts it first,
     * which is why it is not handed to the lane: `freshChildSlots` is in lane
     * order (recency, or the search ranking), and pinning by sorting would mean
     * teaching that comparator what a folder is.
     *
     * Same lane scope and hold container as its siblings, so keyboard roving
     * reaches it as the folder's first row. Kept OUT of `heldLane`'s list
     * because that list is the one a hover-hold displaces, and this row's
     * position is the one thing about it that is fixed.
     *
     * The accent bar is drawn OUT OF FLOW -- an absolutely positioned 2px span
     * in a `relative` wrapper -- for the same alignment reason. A `border-l-2`
     * on the wrapper would push the card's content 2px right and break the same
     * guide by 2px instead of 15. Out of flow it costs no width: it sits inside
     * the card's own left pad, so it reads as a bar down the card's left edge
     * while the text stays on the shared column. `inset-y-1` keeps it off the
     * row divider above and below.
     */
    const folderConductorNode = folderConductorSlot && (
      <div key={`folder-conductor-${folder.id}`} data-folder-conductor={folder.id} className="relative">
        <span aria-hidden="true" className="absolute left-0 inset-y-1 w-0.5 rounded-full bg-accent" />
        {renderSessionRow(folderConductorSlot, depth + 1, false, treeChildScope, treeChildScope, treeChildContainer, undefined, true)}
      </div>
    )
    freshChildSlots.forEach((s, i) => {
      const isActive = isActiveRow(s)
      const nextIsActive = isActiveRow(freshChildSlots[i + 1])
      const showDivider = i < freshChildSlots.length - 1 && !isActive && !nextIsActive
        && !startsAutomaticSection(freshChildSlots, i + 1)
      if (startsAutomaticSection(freshChildSlots, i)) {
        childNodes.push(<PinnedSessionDivider key={`pinned-divider-${folder.id}`} />)
      }
      childNodes.push(renderSessionRow(s, depth + 1, showDivider, treeChildScope, treeChildScope, treeChildContainer))
    })
    if (!searchRanked && staleExpanded.has(folder.id)
      && staleChildSlots.length > 0 && freshChildSlots.length > 0
      && pinned.has(freshChildSlots[freshChildSlots.length - 1].key)) {
      childNodes.push(<PinnedSessionDivider key={`pinned-divider-stale-${folder.id}`} />)
    }
    const staleSection = renderStaleSection(folder.id, staleChildSlots, depth + 1, folder.name)
    if (staleSection) childNodes.push(staleSection)
    // FIRST in the body, after everything else has been pushed. One `unshift`
    // rather than a branch at each of the three places below that read
    // `childNodes.length`: the pinned card is a child of this folder like any
    // other, so counting it as one keeps the empty-body, narrowed-away and
    // body-wrapper decisions exactly as they are on main.
    if (folderConductorNode) childNodes.unshift(folderConductorNode)
    // Hide folders with no matching children while the list is narrowed —
    // unless this folder owns the active create-failure notice: a create fired
    // from the folder-picker menu can target a folder the narrow is hiding,
    // and eliding it would make the failure exactly as silent as before #8229.
    //
    // Nor when the folder ITSELF is what the query named. An empty folder whose
    // name matches is still the answer to "where is that folder" — dropping it
    // would mean the one search guaranteed to name it is also the one search that
    // cannot show it. `folderNameMatchIds` covers the matched folder's subtree, so
    // a matched parent keeps its empty children too: they are part of what the
    // query asked to see.
    if (listNarrowed && childNodes.length === 0
      && folderCreateError?.folderId !== folder.id
      && !folderNameMatchIds?.has(folder.id)) return []
    // Wrap children in a bordered container so the folder's extent is visually
    // clear when multiple folders are open. Only wrap when there's content,
    // otherwise the FolderBody would render an empty 1px-tall strip with a line.
    // Opt-in (Settings > Chat > "Hide the body of an empty folder"): a folder with
    // nothing in it renders NO body - not a collapsed one, not an empty one - so
    // it costs one row instead of two and a tree of area folders stops spending
    // most of the sidebar's height on rows holding nothing. Dropping the body
    // rather than collapsing it is why there is no per-folder expansion state:
    // nothing is hidden, so nothing needs re-reaching.
    const emptyBody = hideEmptyFolderBody && childNodes.length === 0
    const wrapped = childNodes.length > 0 ? (
      <div key={`folder-children-${folder.id}`} className={FOLDER_BODY_CLS}>
        <FolderRail name={folder.name} id={folder.id} onToggle={e => toggleListFolderCollapse(folder, e.currentTarget)} />
        {childNodes}
      </div>
    ) : emptyBody || listNarrowed ? null : (
      // Default: the empty-folder affordance stays exactly as it was. A newly
      // created (or emptied) expanded folder would otherwise render nothing,
      // leaving the hover-only create control on the header as the only
      // (invisible-at-rest) way to start a session in it.
      <div key={`folder-children-${folder.id}`} className={FOLDER_BODY_CLS}>
        <button key={`folder-newchat-${folder.id}`} type="button" data-testid={`folder-empty-new-chat-${folder.id}`} data-folder-new-chat=""
          // Same three-gesture contract as the folder header's "+" above.
          onMouseDownCapture={onOpenSlotInNewTab ? (e => { if (e.button === 1) e.preventDefault() }) : undefined}
          onAuxClick={onOpenSlotInNewTab ? (e => {
            if (e.button !== 1) return
            e.preventDefault()
            createChatInFolder(folder.id, { inNewTab: true })
          }) : undefined}
          onClick={e => createChatInFolder(folder.id, { inNewTab: !!onOpenSlotInNewTab && isOpenInTabModifierClick(e) })}
          title={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })} aria-label={i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })}
          className="w-full flex items-center gap-2.5 pl-3.5 pr-3 py-2 rounded-md text-[12px] text-muted hover:text-accent hover:bg-bg-hover transition-all bg-transparent border-none cursor-pointer text-left">
          <span>{i18nT('pages.chatSidebar.new_chat_in_name', { name: folder.name })}</span><MessageSquarePlus size={13} className="shrink-0 ml-auto" />
        </button>
      </div>
    )
    // Outer container wraps header + body so the entire folder block is a
    // single drag-drop target. Dropping anywhere inside (header, children,
    // empty space) assigns the dragged session to this folder.
    // Uses a dragEnter counter instead of contains() checks — nested child
    // folders fire enter/leave pairs that balance to zero when the drag
    // moves into a subfolder, so the parent highlight clears correctly.
    return [
      <DndDroppable key={`folder-drop-${folder.id}`} id={`folder-drop:${folder.id}`} data={{ type: 'folder-drop', folderId: folder.id }}>
        {({ setNodeRef, isOver }) => (
          // `--folder-pin-stack` is how far the pinned headers above this block's
          // rows reach down the lane (this header plus every ancestor's), so a
          // row that keyboard roving or a reveal scrolls into view lands below
          // them instead of behind them (`scroll-margin-top` in index.css).
          <div ref={setNodeRef} data-folder-drop={folder.id} style={{ '--folder-pin-stack': `calc(var(--folder-row-sticky-h) * ${depth + 1})` } as React.CSSProperties} className={`rounded-md transition-all mb-0.5${isOver ? ' ring-1 ring-accent' : ''}`}>
            {renderFolderHeader(folder, dragHandleProps, emptyBody, depth)}
            {renderFolderCreateError(folder.id)}
            {wrapped && <FolderBody key={`folder-body-${folder.id}`} padding={FOLDER_BODY_OPEN_PADDING} open={!folder.collapsed && !forceCollapsed} instantClose={instantCloseId === folder.id}>{wrapped}</FolderBody>}
          </div>
        )}
      </DndDroppable>,
    ]
  }

  const {
    rootFolders, visibleRootFolders, rootFolderIds,
  } = useRootFolderLanes({ folders, folderCompare, isFolderHidden, isFolderFilteredOut })
  // True while actively dragging a session that currently lives in a folder.
  // Used to reveal the empty-state drop placeholder inside the "No folder"
  // group so there's always a reachable ungroup target.
  const draggingFolderedSession = activeDrag?.type === 'session' && !!slotFolders[activeDrag.id]
  // The board's reading of the same question, from the native mirror: a card
  // drags with HTML5 DnD there, so `activeDrag` never carries it. Reveals the
  // unfile strip at the foot of every column.
  const boardDraggingFiledSession = nativeSessionDrag !== null && !!slotFolders[nativeSessionDrag]
  // WHY the session being dragged may not be referenced into the open chat, or
  // null when it may be. Carries the reason rather than a boolean because the two
  // refusals read differently to the user (a privacy guard vs a self-drop no-op).
  // Drives the drop zone's refusal state; the drop handler re-decides with the
  // same function.
  const draggingRefRefusal = activeDrag?.type === 'session'
    ? sessionRefBlockReason({
      key: activeDrag.id,
      activeSlot,
      memoryMode: localSlots.find(x => x.key === activeDrag.id)?.memory_mode,
    })
    : null
  // True while dragging a folder that currently has a parent — the only case
  // where "drop on the root lane to move to top level" applies.
  const draggingNestedFolder = activeDrag?.type === 'folder' && !!folders.find(f => f.id === activeDrag.id)?.parent_id

  // Droppable rects are normally snapshotted once at drag-start, but these
  // lanes ANIMATE during drags (the dragged folder's body collapses over 150ms;
  // hovered collapsed folders auto-expand; the chat-pane zone mounts mid-drag),
  // so the snapshot goes stale and drop targets diverge from the cursor. While a
  // drag is live, poll re-measurement (dnd-kit's numeric `frequency`
  // self-reschedules a measure loop) so rects track the animating layout. Idle
  // sessions keep the plain strategy — no background measuring.
  const dndMeasuring = activeDrag
    ? { droppable: { strategy: MeasuringStrategy.Always, frequency: 100 } }
    : { droppable: { strategy: MeasuringStrategy.Always } }
  /** The follow-the-cursor preview for whatever is being dragged. */
  const dragGhost = activeDrag
    ? activeDrag.type === 'folder'
      ? <FolderDragGhost folder={folders.find(x => x.id === activeDrag.id)} />
      : <SessionDragGhost slot={localSlots.find(x => x.key === activeDrag.id)} fallbackLabel={activeDrag.id} />
    : null
  /**
   * The drag preview is PORTALED to `document.body`.
   *
   * dnd-kit positions the overlay `fixed`, which normally escapes ancestor
   * overflow — but the sidebar rides inside OverlayDrawer's morph `clip-path`,
   * and a clip-path clips every descendant including fixed ones. Rendered in
   * place, the ghost therefore vanished the instant the cursor crossed out of
   * the sidebar and into the chat pane, i.e. for the whole second half of the
   * one gesture that aims there. Portaling keeps it visible until release; it
   * stays inside the DndContext because React portals preserve context.
   */
  const dragOverlay = createPortal(
    <DragOverlay dropAnimation={null} modifiers={activeDrag?.type === 'session' ? SESSION_DRAG_GHOST_MODIFIERS : undefined}>{dragGhost}</DragOverlay>,
    document.body,
  )

  // Narrow-sidebar header responsiveness: below ~256px the full "New chat"
  // label no longer fits next to the label + kebab, so collapse the create
  // button to icon-only; below ~200px also drop the "Sessions" label.
  const compactHeader = paintedSidebarWidth < 256
  const tinyHeader = paintedSidebarWidth < 200

  // The Older Sessions pane's guide scope (`open:chat.older-sessions`).
  useGuideDisclosureScope('chat.older-sessions', historyOpen)

  return (
    // stable theming hook 'sidebar' — see website/docs/theming-contract.md
    // The region is the session picker a guide's "choose the session" step
    // points at (`sessions.list`): both layouts draw their rows inside it.
    <div role="region" aria-label={i18nT('pages.chatSidebar.sessions')} {...uiLocation('sessions.list', sidebarRootRef)} onPointerOver={onRootPointerOver} onPointerLeave={releaseHoverPin} className={`${LIST_SHELL_CLS} flex flex-col shrink-0 relative h-full`} style={rootStyle}>
      {/* Scroll peek: full titles in a floating, click-through layer. The
          card itself keeps `sidebarWidth`. */}
      <ScrollPeekLayer titles={peekTitles} />
      {/* Drag handle — the shared column grip (components/ResizeHandle), so
          this edge looks and behaves exactly like the Crew Members roster's and
          the app workspaces'. Positioned absolutely on the card's right border
          (the default is an in-flow flex sibling); `inset` is the card's
          rounded-xl radius so the accent bar spans exactly the straight
          segment of the border. `sidebar-resize-handle` stays as the hook the
          mobile overlay and the split-pane host use to hide it. */}
      <ResizeHandle
        handleProps={sidebarResize}
        label={i18nT('pages.chatSidebar.resize_sidebar')}
        onNudge={nudgeSidebar}
        value={paintedSidebarWidth}
        min={SIDEBAR_MIN}
        max={sidebarMax}
        inset={12}
        // z-40: above the floating search dock (ListDock, z-30). Once a filter
        // chip or a notice mounts, the dock's opaque shelf spans the card's full
        // width, and at z-10 it took the inner half of the grip's 6px strip.
        className="sidebar-resize-handle absolute top-0 -right-[3px] h-full z-40"
      />

      {/* Header — all elements ("Sessions" title, kebab, New button) centered
          on one line 23px from the panel top (1px card border + mt-0.5, then
          centered in a 40px row) — the shared control baseline: the nav rail
          header, chat title row, and activity strip icons center on the same
          line.
          px-2 is symmetric so the New button ends 9px from the card's right
          edge (8 + 1px border) — the same as its 9px gap to the top edge
          (1px border + mt-0.5 + 6px of the h-10 row around the h-7 button). */}
      <div className={LIST_HEADER_CLS}>
        <div className={`flex items-center gap-1.5 min-w-0 flex-1 ${collapsible && !isMobile ? 'pl-9' : 'pl-1.5'}`}>
          {!tinyHeader && <span className={LIST_TITLE_CLS}>{i18nT('pages.chatSidebar.sessions')}</span>}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <DropdownMenu guideScope="menu:sessions.list-menu">
            <DropdownMenuTrigger asChild>
              <button className="mc-touch-hit w-7 h-7 rounded-md border border-border bg-transparent text-muted cursor-pointer flex items-center justify-center hover:border-border-strong hover:text-text transition-all" title={i18nT('pages.chatSidebar.more_options')} aria-label={i18nT('pages.chatSidebar.more_options')} {...uiLocation('sessions.list-menu')}><MoreVertical size={14} /></button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="min-w-[180px]">
              <DropdownMenuItem onSelect={() => navigate('/session-dashboards')} {...uiLocation('sessions.list-menu.dashboards')}>
                <Monitor size={14} className="text-muted" />
                {i18nT('commandCenter.all_title')}
              </DropdownMenuItem>
              <DropdownMenuItem disabled={seedStateLanesMutation.isPending} onClick={() => {
                if (seedStateLanesMutation.isPending) return
                const isActive = tagColumnsEnabled && rawColumns.length > 0
                const next = !isActive
                const cfg = loadChatConfig()
                // The toggle persists `tagColumnsEnabled` through saveChatConfig,
                // which returns false and rolls back when the write (or its dirty
                // marker) cannot be stored. Surface that failure and STOP before
                // clearing the error, seeding lanes or restoring widths — none of
                // which should happen for a preference that was not saved (GPT 6.1
                // F1, errors-use-error-notice).
                if (!saveChatConfig({ ...cfg, tagColumnsEnabled: next })) {
                  setToggleSaveFailed(true)
                  setSeedError(i18nT('pages.chatSidebar.failed_to_save_board_toggle'))
                  return
                }
                setToggleSaveFailed(false)
                setSeedError('')
                if (!next) {
                  // Leaving board view: give back the width the user chose before
                  // the lanes were auto-widened, rather than stranding a ~900px
                  // sidebar in list view.
                  restorePreBoardWidth()
                }
                // Seed when the board has no lanes and nothing configured worth
                // keeping. Seeding is additive and idempotent, so a repeat click
                // cannot duplicate lanes; the pending guard above only stops a
                // second request racing the first before the cache refreshes.
                if (next && !rawColumns.some(c => c.source === 'state' || c.name || (c.tag_ids || []).length || c.include_untagged)) {
                  seedStateLanesMutation.mutate()
                }
              }} {...uiLocation('sessions.list-menu.view')}>
                <Columns3 size={14} className={tagColumnsEnabled && rawColumns.length > 0 ? 'text-accent' : 'text-muted'} />
                {tagColumnsEnabled && rawColumns.length > 0 ? i18nT('pages.chatSidebar.switch_to_list_view') : i18nT('pages.chatSidebar.switch_to_board_view')}
              </DropdownMenuItem>
              {tagColumnsEnabled && rawColumns.length > 0 && missingLanes.length > 0 && (
                <DropdownMenuItem
                  data-testid="add-state-lanes"
                  {...uiLocation('sessions.list-menu.add-lanes')}
                  disabled={seedStateLanesMutation.isPending}
                  onClick={() => { if (!seedStateLanesMutation.isPending) seedStateLanesMutation.mutate() }}
                >
                  <Columns3 size={14} className="text-muted" />
                  {i18nT('pages.chatSidebar.add_state_lanes')}
                </DropdownMenuItem>
              )}
              <DropdownMenuItem onClick={() => { setCleanupOpen(!cleanupOpen); setCleanupExpanded(false); setCleanupError('') }} {...uiLocation('sessions.list-menu.clean-up')}>
                <BrushCleaning size={14} className="text-muted" />
                {i18nT('pages.chatSidebar.clean_up_sessions')}
              </DropdownMenuItem>
              <DropdownMenuItem data-testid="clean-up-empty-folders" onClick={() => setFolderCleanupOpen(!folderCleanupOpen)}>
                <FolderX size={14} className="text-muted" />
                {i18nT('pages.chatSidebar.clean_up_empty_folders_menu')}
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => { setBulkModelOpen(true); setBulkModel(''); setBulkSkipRunning(true); setBulkModelError('') }} {...uiLocation('sessions.list-menu.switch-model')}>
                <Cpu size={14} className="text-muted" />
                {i18nT('pages.chatSidebar.switch_all_to_model')}
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => setManageTagsOpen(o => !o)} {...uiLocation('sessions.list-menu.manage-tags')}>
                <TagIcon size={14} className="text-muted" />
                {i18nT('pages.chatSidebar.manage_tags')}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          {/* Split create-button: main segment = one-click New chat; caret
           *  opens a menu grouping New folder + New chat in folder (flat
           *  folder flyout). Replaces the old standalone New-folder + New-chat
           *  header buttons. Menu is portaled to <body> so the right-side
           *  folder flyout escapes the sidebar's overflow clip. */}
          <div className="relative flex items-center rounded-md bg-accent text-accent-fg overflow-hidden [@media(pointer:coarse)]:overflow-visible shrink-0" data-create-menu>
            <button
              disabled={creatingSlot}
              className={`mc-touch-hit-y flex items-center h-7 rounded-s-md cursor-pointer bg-transparent border-none text-accent-fg hover:bg-accent-hover active:scale-95 transition-all disabled:opacity-70 disabled:cursor-wait disabled:active:scale-100 ${compactHeader ? 'justify-center w-7' : 'gap-1.5 pl-2 pr-2.5 text-[12px] font-semibold'}`}
              // Same three-gesture contract as a session row: plain click
              // creates and switches; Cmd/Ctrl-click and middle-click create the
              // session as a background TAB and leave the user where they are.
              // Both tab gestures are gated on `onOpenSlotInNewTab` — without a
              // tab strip (embedded hosts) there is nothing to open into, so the
              // modifier is ignored and the click stays an ordinary create.
              // Middle-press autoscroll is cancelled on mousedown, as on rows.
              onMouseDownCapture={onOpenSlotInNewTab ? (e => { if (e.button === 1) e.preventDefault() }) : undefined}
              onAuxClick={onOpenSlotInNewTab ? (e => {
                if (e.button !== 1 || creatingSlot) return
                e.preventDefault()
                createChatMutation.mutate({ inNewTab: true })
              }) : undefined}
              onClick={e => { createChatMutation.mutate({ inNewTab: !!onOpenSlotInNewTab && isOpenInTabModifierClick(e) }) }}
              title={i18nT('pages.chatSidebar.new_chat')}
              aria-label={i18nT('pages.chatSidebar.new_chat_session')}
              aria-busy={creatingSlot}
              {...uiLocation('chat.new-session')}
            >{creatingSlot ? <Loader2 size={15} className="animate-spin" /> : <Plus size={15} />}{!compactHeader && <span className="whitespace-nowrap">{creatingSlot ? i18nT('pages.chatSidebar.creating') : i18nT('pages.chatSidebar.new')}</span>}</button>
            <span className="w-px h-4 bg-accent-fg opacity-30" aria-hidden="true" />
            <DropdownMenu guideScope="menu:sessions.create-menu" open={newChatMenuOpen} onOpenChange={o => { setNewChatMenuOpen(o); if (!o) setRemoteCrewError('') }}>
              <DropdownMenuTrigger asChild>
                <button
                  className="mc-touch-hit-end flex items-center justify-center w-6 h-7 rounded-e-md cursor-pointer bg-transparent border-none text-accent-fg hover:bg-black/10 active:scale-95 transition-all"
                  title={i18nT('pages.chatSidebar.create')} aria-label={i18nT('pages.chatSidebar.more_create_options')} {...uiLocation('sessions.create-menu')}><ChevronDown size={13} /></button>
              </DropdownMenuTrigger>
              {/* max-w bounds the menu: the mode descriptions below are full
               *  sentences, and without an upper bound a flex item's automatic
               *  min-width lets the longest one stretch the menu across the
               *  session list instead of wrapping. */}
              <DropdownMenuContent align="end" className="min-w-[200px] max-w-[264px]" onCloseAutoFocus={onMenuCloseAutoFocus}>
                {/* The plain chat is what the button's main segment does, but a
                 *  menu that lists every OTHER way to create and omits the
                 *  ordinary one reads as if the other kinds were the only ones
                 *  the caret can make. Listed first so the default stays the
                 *  default. */}
                <DropdownMenuItem disabled={creatingSlot} onClick={() => { createChatMutation.mutate({ inNewTab: false }) }}>
                  <MessageSquarePlus size={14} className="text-muted" /> {i18nT('pages.chatSidebar.new_chat')}
                </DropdownMenuItem>
                {/* The two private modes sit at the top level beside New chat,
                 *  each naming its own action and what it does with memory. They
                 *  used to share a submenu named for a word no other surface uses,
                 *  which at phone width collapsed into a caption that read as a
                 *  button. */}
                <DropdownMenuItem className="items-start" data-testid="new-incognito-chat" disabled={creatingSlot} onClick={() => { createEphemeralChatMutation.mutate('incognito') }} {...uiLocation('sessions.create-menu.incognito')}>
                  <PrivateChatRow kind="incognito">{i18nT('pages.chatSidebar.new_incognito_chat')}</PrivateChatRow>
                </DropdownMenuItem>
                <DropdownMenuItem className="items-start" data-testid="new-temporary-chat" disabled={creatingSlot} onClick={() => { createEphemeralChatMutation.mutate('temporary') }} {...uiLocation('sessions.create-menu.temporary')}>
                  <PrivateChatRow kind="temporary">{i18nT('pages.chatSidebar.new_temporary_chat')}</PrivateChatRow>
                </DropdownMenuItem>
                {/* Import creates a session too, so it sits with the create rows
                 *  rather than only in a per-session ⋯ menu that has to be opened
                 *  on some unrelated session first. */}
                <ImportSessionItem Item={DropdownMenuItem} />
                {/* Crew Members is a DOOR, not a create action: it navigates to the
                 *  Members page (or, while that page is preview-gated, to the
                 *  Settings card that turns it on — see `openCrewMembers`). It sits
                 *  among the create entries because this menu is where "crew" was
                 *  offered until Crew Mode retired, so it is where a returning user
                 *  looks. Not disabled by `creatingSlot`: it creates nothing.
                 *
                 *  CAPTURED: the Feature Previews "See what it looks like" dialog
                 *  shows the Members page this entry opens. A visible change to
                 *  that page makes the picture stale — re-shoot with
                 *  `scripts/capture-feature-previews.mjs`.
                 *
                 *  Separators on BOTH sides: every other row here creates something and is
                 *  named "New …"; this one navigates and is not. Without the rule a
                 *  reader parsed it as an unnamed create action on every menu open
                 *  (UX review on #9519). It sits between the session rows and the
                 *  folder rows, in a group of its own. */}
                <DropdownMenuSeparator />
                <DropdownMenuItem className="items-start" data-testid="open-crew-members" onClick={openCrewMembers}>
                  <Users size={14} className="text-muted mt-[3px] shrink-0" />
                  <span className="flex min-w-0 flex-col gap-px">
                    <span>{i18nT('pages.chatSidebar.open_crew_members')}</span>
                    <span className="whitespace-normal text-[11px] leading-snug text-muted">{crewPreview ? i18nT('pages.chatSidebar.open_crew_members_desc') : i18nT('pages.chatSidebar.open_crew_members_gated_desc')}</span>
                  </span>
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={() => { setFolderModal({ mode: 'create', parentId: '' }) }} {...uiLocation('sessions.create-menu.new-folder')}>
                  <FolderPlus size={14} className="text-muted" /> {i18nT('pages.chatSidebar.new_folder')}
                </DropdownMenuItem>
                {folders.length > 0 && (() => {
                  const folderRows = (() => {
                    const roots = folders.filter(f => !f.parent_id).sort(folderCompare)
                    const childrenOf = (pid: string) => folders.filter(f => f.parent_id === pid).sort(folderCompare)
                    const items: { f: ChatFolder; depth: number }[] = []
                    const walk = (list: ChatFolder[], depth: number) => { for (const f of list) { items.push({ f, depth }); walk(childrenOf(f.id), depth + 1) } }
                    walk(roots, 0)
                    return items.map(({ f, depth }) => (
                      <DropdownMenuItem key={f.id} style={{ paddingLeft: `${12 + depth * 16}px` }} onClick={() => createChatInFolder(f.id, { focus: true })}>
                        <Folder size={14} className={depth === 0 ? 'text-muted' : 'text-muted/60'} /> {f.name}
                      </DropdownMenuItem>
                    ))
                  })()
                  // A flyout has nowhere to open at phone width (Radix pins a
                  // submenu to the trigger's side and only shifts it vertically),
                  // so on a phone the folders are listed inline under a caption.
                  // The caption carries no icon: drawn with the same Folder glyph
                  // as the rows under it, it read as one more tappable row. Text
                  // alone, in the FILTER / SORT BY caption style, marks a heading.
                  if (isMobile) {
                    return (
                      <>
                        <DropdownMenuSeparator />
                        <DropdownMenuLabel data-testid="new-chat-in-folder-caption" className="text-[11px] uppercase tracking-[.04em]">
                          {i18nT('pages.chatSidebar.new_chat_in_folder')}
                        </DropdownMenuLabel>
                        <div className="max-h-[240px] overflow-y-auto">{folderRows}</div>
                      </>
                    )
                  }
                  return (
                  <DropdownMenuSub>
                    <DropdownMenuSubTrigger className="data-[disabled]:pointer-events-none data-[disabled]:opacity-50">
                      <Folder size={14} className="text-muted" /> {i18nT('pages.chatSidebar.new_chat_in_folder')}
                      <ChevronRight size={13} className="ml-auto text-muted" />
                    </DropdownMenuSubTrigger>
                    {/* Intentional tighter cap composed via min() with the
                        primitive's available-height var: 300px keeps the folder
                        list submenu compact while preserving the viewport
                        never-clip floor (a bare max-h would override the
                        primitive, since cn()'s tailwind-merge dedupes max-h-*).
                        overflow is left to the primitive. */}
                    <DropdownMenuSubContent className="max-h-[min(300px,var(--radix-dropdown-menu-content-available-height))]">
                      {folderRows}
                    </DropdownMenuSubContent>
                  </DropdownMenuSub>
                  )
                })()}
                {/* "New chat on crew" — the same shape as "New chat in folder"
                 *  above (dynamic rows behind one submenu, listed inline at phone
                 *  width where a Radix flyout has nowhere to open), because it
                 *  answers the same kind of question. The row is absent, not
                 *  disabled, when no crew holds a live tunnel: a disabled row
                 *  would advertise a capability the install may never have. It
                 *  sits AFTER the folder rows so the local ways to create keep
                 *  their position.
                 *
                 *  Preview-gated on its OWN flag (`utils/previewFlags.ts`), not
                 *  the Crew Members page's: the landing is what is unfinished, since the
                 *  created session opens in that crew's pane and the local list
                 *  does not yet show live remote sessions. Toggle lives in
                 *  Settings > Remote Crew. */}
                {remoteCrewChatPreview && warmCrews.length > 0 && (() => {
                  // The picked row swaps its icon for a spinner and its label for
                  // "Starting chat on <crew>…": the create opens a session on the peer
                  // before the local one exists, so it takes seconds, and the menu
                  // stays open (onSelect preventDefault) the whole time, so the row
                  // is where the eye already is.
                  const crewRows = warmCrews.map(c => {
                    const pendingHere = createRemoteChatMutation.isPending && createRemoteChatMutation.variables === c.id
                    return (
                      <DropdownMenuItem key={c.id} data-testid={`new-chat-on-crew-${c.id}`}
                        // The picked row is the progress cue, so it is not disabled
                        // (a disabled row is drawn faded); the guard in onSelect is
                        // what refuses a second pick while the create runs.
                        disabled={createRemoteChatMutation.isPending && !pendingHere}
                        aria-busy={pendingHere || undefined}
                        onSelect={e => {
                          e.preventDefault()
                          if (createRemoteChatMutation.isPending) return
                          createRemoteChatMutation.mutate(c.id)
                        }}>
                        {pendingHere
                          ? <Loader2 size={14} className="text-info animate-spin" aria-hidden="true" data-testid={`new-chat-on-crew-spinner-${c.id}`} />
                          : <Server size={14} className="text-info" />}{' '}
                        {pendingHere ? i18nT('pages.chatSidebar.creating_on_crew', { name: c.name }) : c.name}
                      </DropdownMenuItem>
                    )
                  })
                  // Inline failure reason (version mismatch, tunnel down), shown
                  // through the shared ErrorNotice (website AGENTS.md forbids a
                  // hand-written text-danger div for a rejected mutation). Kept in
                  // the menu because the create leaves nothing behind on failure —
                  // closing would erase the only signal; `onSelect preventDefault`
                  // on the rows keeps a failed create from auto-closing over it.
                  // The sibling menu item is the keyboard-reachable hand-off in
                  // both the mobile inline list and the desktop submenu.
                  const errRow = remoteCrewError
                    ? (
                      <>
                        <div className="px-2 py-1.5">
                          <ErrorNotice
                            id={remoteCrewErrorId}
                            message={remoteCrewError}
                            variant="inline"
                            testId="new-chat-on-crew-error"
                          />
                        </div>
                        <ErrorNoticeMenuItem
                          Item={DropdownMenuItem}
                          message={remoteCrewError}
                          describedBy={remoteCrewErrorId}
                        />
                      </>
                    )
                    : null
                  if (isMobile) {
                    return (
                      <>
                        <DropdownMenuLabel className="text-[11px] uppercase tracking-[.04em] flex items-center gap-2">
                          <Server size={13} className="text-info" /> {i18nT('pages.chatSidebar.new_chat_on_crew')}
                        </DropdownMenuLabel>
                        <div className="max-h-[240px] overflow-y-auto">{crewRows}{errRow}</div>
                      </>
                    )
                  }
                  return (
                    <DropdownMenuSub>
                      <DropdownMenuSubTrigger data-testid="new-chat-on-crew" className="data-[disabled]:pointer-events-none data-[disabled]:opacity-50">
                        <Server size={14} className="text-info" /> {i18nT('pages.chatSidebar.new_chat_on_crew')}
                        <ChevronRight size={13} className="ml-auto text-muted" />
                      </DropdownMenuSubTrigger>
                      {/* Intentional tighter cap composed via min() with the
                          primitive's available-height var: 300px keeps the crew
                          list submenu compact while preserving the viewport
                          never-clip floor (a bare max-h would override the
                          primitive, since cn()'s tailwind-merge dedupes max-h-*).
                          overflow is left to the primitive. */}
                      <DropdownMenuSubContent className="max-h-[min(300px,var(--radix-dropdown-menu-content-available-height))]">
                        {crewRows}{errRow}
                      </DropdownMenuSubContent>
                    </DropdownMenuSub>
                  )
                })()}
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </div>

      {/* Split View (session grid) has no entry here on purpose: this sidebar is a
       *  navigation surface, and the grid's own affordances live next to the
       *  transcript they act on — the chat header's Columns2 button (⌘D) opens it,
       *  and the header's "in split" badge is the way back into a live split. */}

      {/* Clean Up dialog */}
      {folderCleanupOpen && <FolderCleanupPanel folders={folders} onClose={() => setFolderCleanupOpen(false)} />}
      {cleanupOpen && (() => {
        const archivable = cleanupPreview ? cleanupPreview.map(k => localSlots.find(s => s.key === k)).filter(Boolean) as Slot[] : []
        const noStale = cleanupPreview != null && cleanupPreview.length === 0 && !activeIsStale
        return (
          <div className="mx-2 mb-2 p-3 rounded-lg bg-bg border border-border shadow-md text-sm animate-rise">
            <div className="font-medium text-text-strong mb-2"><BrushCleaning size={14} className="lucide-inline" /> {i18nT('pages.chatSidebar.clean_up_sessions_2')}</div>
            <div className="text-muted text-[12px] mb-2">{i18nT('pages.chatSidebar.archive_sessions_with_no_activity_in_the_last')}</div>
            <div className="flex items-center gap-2 mb-3">
              {[1, 3, 7].map(d => (
                <button key={d} className={`px-2.5 py-1 rounded-md text-[12px] border transition-all cursor-pointer ${
                  cleanupDays === d ? 'bg-accent text-accent-fg border-accent' : 'bg-transparent text-muted border-border hover:border-border-strong hover:text-text'
                }`} onClick={() => setCleanupDays(d)}>{i18nT('pages.chatSidebar.day', { count: d })}</button>
              ))}
            </div>
            <div className="text-[12px] text-muted mb-3">
              {cleanupPreviewLoading
                ? i18nT('pages.chatSidebar.checking')
                : cleanupPreviewError
                  ? (
                    // Read failure inside a confirm dialog with no draft: nothing to
                    // lose, so the hand-off is on. Retry stays a separate button
                    // rather than being the error surface itself.
                    <span className="inline-flex items-center gap-2 flex-wrap">
                      <ErrorNotice message={i18nT('pages.chatSidebar.failed_to_load_preview')} variant="inline" askAgent testId="cleanup-preview-error" />
                      <Btn className="text-[12px] px-2 py-0.5" onClick={() => queryClient.invalidateQueries({ queryKey: ['cleanup-preview'] })}>{i18nT('pages.chatSidebar.retry')}</Btn>
                    </span>
                  )
                  : noStale
                    ? i18nT('pages.chatSidebar.no_inactive_sessions_to_archive')
                    : cleanupPreview != null && <>
                      {i18nT('pages.chatSidebar.session', { count: archivable.length })} {i18nT('pages.chatSidebar.will_be_moved_to_older_sessions')}{activeIsStale ? ` ${i18nT('pages.chatSidebar.1_skipped_currently_selected')}` : ''} {i18nT('pages.chatSidebar.pinned_sessions_are_kept')}
                      {archivable.length > 0 && (
                        <button className="ml-1 text-accent hover:underline cursor-pointer bg-transparent border-none p-0 text-[12px]" onClick={() => setCleanupExpanded(!cleanupExpanded)}>
                          {cleanupExpanded ? i18nT('pages.chatSidebar.hide') : i18nT('pages.chatSidebar.show')} {i18nT('pages.chatSidebar.session', { count: archivable.length })} ▸
                        </button>
                      )}
                      {cleanupExpanded && archivable.length > 0 && (
                        <div className="mt-2 max-h-32 overflow-y-auto rounded-md border border-border bg-bg-elevated p-1.5">
                          {archivable.map(s => (
                            <div key={s.key} className="text-[12px] text-muted truncate py-0.5 px-1">
                              {s.title && s.title !== s.key ? s.title : s.key}
                              {slotActivityTs(s) && <span className="ml-1 text-[11px] opacity-60">{fmtRelativeTime(slotActivityTs(s))}</span>}
                            </div>
                          ))}
                        </div>
                      )}
                      </>
              }
            </div>
            {/* Archive inputs are server-side (the days window is a persisted
                pick, not a draft), so nothing is lost by handing off. Its own
                line, above the Cancel/Archive pair: a third control in that row
                would break max-two-buttons-per-row. */}
            <ErrorNotice message={cleanupError} askAgent className="mb-2" testId="cleanup-error" />
            <div className="flex items-center gap-2 justify-end">
              <Btn className="text-[12px] px-3 py-1" onClick={() => setCleanupOpen(false)}>{i18nT('pages.chatSidebar.cancel')}</Btn>
              <Btn className="text-[12px] px-3 py-1 bg-accent text-accent-fg hover:bg-accent-hover" disabled={archivable.length === 0 || cleanupMutation.isPending || cleanupPreviewLoading} onClick={() => {
                setCleanupError('')
                cleanupMutation.mutate()
              }}>{cleanupMutation.isPending ? i18nT('pages.chatSidebar.archiving') : i18nT('pages.chatSidebar.archive_session', { count: archivable.length })}</Btn>
            </div>
          </div>
        )
      })()}

      {/* Switch-all-to-model dialog — mirrors the Clean Up panel. Picking a
       *  model applies it to every live session; running sessions are skipped
       *  by default. A switch restarts each session's agent process and keeps
       *  its conversation: the next turn resumes it on the new model. The one
       *  thing a switch can destroy is a reply in flight, and only for a
       *  running session left unskipped, so the red caveat renders only once
       *  the skip box is cleared with some session running. */}
      {bulkModelOpen && (
        <div className="mx-2 mb-2 p-3 rounded-lg bg-bg border border-border shadow-md text-sm animate-rise">
          <div className="font-medium text-text-strong mb-2"><Cpu size={14} className="lucide-inline" /> {i18nT('pages.chatSidebar.switch_all_sessions')}</div>
          <div className="text-muted text-[12px] mb-2">
            {i18nT('pages.chatSidebar.bulk_switch_keeps_conversation')}
            {bulkRunningCount > 0 && !bulkSkipRunning && <> <span data-testid="bulk-model-running-caveat" className="text-danger">{i18nT('pages.chatSidebar.bulk_switch_stops_running_reply')}</span></>}
          </div>
          {bulkModelsFailed && (
            <div className="flex flex-wrap items-center gap-2 mb-2">
              {/* No hand-off: the chosen bulkModel/skipRunning selection is unsaved,
                  and the navigation would discard it. Retry stays in the panel.
                  Its own row above the listbox, wrapping the Retry button under
                  the notice when the sidebar is too narrow for both: an inline
                  notice sharing a fixed row collapses to one character per line
                  at sidebar width, and the Cancel/Switch row below is already at
                  the two-button limit. */}
              <ErrorNotice
                className="flex-1 min-w-[12rem]"
                message={i18nT('pages.chatSidebar.model_list_failed')}
                testId="bulk-model-roster-error"
              />
              <Btn
                className="text-[12px] px-3 py-1 shrink-0"
                disabled={bulkModelsQuery.isFetching}
                onClick={() => bulkModelsQuery.refetch()}
              >{i18nT('pages.chatSidebar.retry')}</Btn>
            </div>
          )}
          <div ref={bulkListRef} role="listbox" aria-label={i18nT('pages.chatSidebar.model_list')} tabIndex={-1} onKeyDown={bulkOnListKeyDown} className="max-h-[220px] overflow-y-auto rounded-md border border-border bg-bg-elevated p-1 mb-2 outline-hidden">
            <ModelDropdownList models={bulkModelOptions} activeModel={bulkModelPick} onSelect={setBulkModel} />
          </div>
          {bulkRunningCount > 0 && (
            <label className="flex items-center gap-2 text-[12px] text-muted mb-2 cursor-pointer">
              {/* aria-labelledby, not aria-label: the name is the visible
                  "Skip N running sessions" text, which is two catalog keys plus a
                  live count. Binding it by reference keeps the announced name and
                  the rendered name the same string, so the count cannot drift. */}
              <input type="checkbox" aria-labelledby={bulkSkipRunningLabelId} checked={bulkSkipRunning} onChange={e => setBulkSkipRunning(e.target.checked)} />
              <span id={bulkSkipRunningLabelId}>{i18nT('pages.chatSidebar.skip')} {i18nT('pages.chatSidebar.running_session', { count: bulkRunningCount })}</span>
            </label>
          )}
          {/* The bucket the Switch count leaves out with no other surface:
              sessions already on the pick. Without it a lower count, or a
              disabled "Switch 0 sessions", cannot be reconciled with the rows. */}
          {bulkModelPick && bulkPartition.onTarget > 0 && (
            <div data-testid="bulk-model-on-target" role="status" className="text-[12px] text-muted mb-2">
              {i18nT('pages.chatSidebar.already_on_model', { count: bulkPartition.onTarget })}
            </div>
          )}
          {/* No hand-off: the chosen bulkModel/skipRunning selection is unsaved,
              and the navigation would discard it. Its own line, above the
              Cancel/Switch pair: a third control in that row would break
              max-two-buttons-per-row, and an inline notice sharing the row
              collapses to one character per line at sidebar width. */}
          <ErrorNotice message={bulkModelError} className="mb-2" testId="bulk-model-error" />
          <div className="flex items-center gap-2 justify-end">
            <Btn className="text-[12px] px-3 py-1" onClick={() => { setBulkModelOpen(false); setBulkModel(''); setBulkModelError('') }}>{i18nT('pages.chatSidebar.cancel')}</Btn>
            <Btn className="text-[12px] px-3 py-1 bg-accent text-accent-fg hover:bg-accent-hover" disabled={!bulkModelPick || bulkAffectedCount === 0 || bulkModelMutation.isPending} onClick={() => { setBulkModelError(''); bulkModelMutation.mutate({ model: bulkModelPick, skipRunning: bulkSkipRunning }) }}>{bulkModelMutation.isPending ? i18nT('pages.chatSidebar.switching') : i18nT('pages.chatSidebar.switch_session', { count: bulkAffectedCount })}</Btn>
          </div>
        </div>
      )}

      {/* Manage-tags panel — mirrors the Clean Up / Switch All panels. Renders
       *  the shared TagManagerList in 'manage' mode (no column context), so tag
       *  CRUD is reachable in list view too, not only from a board column. */}
      {manageTagsOpen && (
        <div data-testid="manage-tags-panel" className="mx-2 mb-2 p-3 rounded-lg bg-bg border border-border shadow-md text-sm animate-rise">
          <div className="flex items-center justify-between mb-2">
            <div className="font-medium text-text-strong"><TagIcon size={14} className="lucide-inline" /> {i18nT('pages.chatSidebar.manage_tags_2')}</div>
            <button type="button" className="text-muted hover:text-text bg-transparent border-none cursor-pointer p-0 leading-none" onClick={() => setManageTagsOpen(false)} aria-label={i18nT('pages.chatSidebar.close')}><X size={13} /></button>
          </div>
          <div className="text-muted text-[12px] mb-2">{i18nT('pages.chatSidebar.rename_flag_as_status_or_delete_tags_changes_app')}</div>
          <TagManagerList mode="manage" />
        </div>
      )}

      {/* The floating dock (components/ListDock): the glass search capsule, the
          filter chips and the list-level notices hover over the lanes, and
          every lane's scroller pads its top by the dock's live height. */}
      <ListDock field={(
        // Search with inline sort/filter control — the shared list-panel
        // search row (components/SearchFilterBar), also mounted by the Crew
        // Members roster.
      <SearchFilterBar
        placeholder={i18nT('pages.chatSidebar.search_sessions')}
        clearLabel={i18nT('pages.chatSidebar.clear_search')}
        value={slotFilter}
        onChange={setSlotFilter}
        trailingCount={availableLanes.length > 1 ? 2 : 1}
        trailing={(
          <>
            {/* ONE button cycling tree -> conductor -> flat, skipping any lane that
             *  cannot render (see `availableLanes`). Deliberately not a segmented
             *  control: the sidebar's chrome stays Raycast-plain, so the icon shows
             *  the lane you are IN and the copy names where the next press goes.
             *  Hidden entirely when only the tree is available, which is the
             *  pre-existing "no folders, nothing to flatten" case. */}
            {availableLanes.length > 1 && (
            <button
              type="button"
              className={`relative w-6 h-6 rounded flex items-center justify-center cursor-pointer transition-colors border-none ${lane !== 'tree' ? 'text-accent bg-accent-subtle' : 'text-muted hover:text-text hover:bg-bg-hover bg-transparent'}`}
              onClick={cycleLane}
              /* Both strings describe the ACTION and are derived from `nextLane`, not
               * from the lane in view: this is the feature's only entry point, so a
               * label naming the current lane tells every user -- and every screen
               * reader -- that the press goes somewhere it does not.
               *
               * With a board configured the toggle flattens INSIDE each column rather
               * than producing the single flat lane, so the copy must not promise
               * "all chats without folders" (one combined list). */
              title={laneSwitchLabel}
              aria-label={laneSwitchLabel}
              /* NOT `aria-pressed`. This cycles three positions, and a boolean would
               * announce the same "pressed" for conductor and for flat -- two different
               * states told apart by nothing a screen reader hears. The lane in view is
               * named instead, which is the fact a reader actually wants. */
              data-lane={lane}
              data-next-lane={nextLane}
              data-testid="flat-view-toggle"
            >
              {/* Crossfaded rather than hard-swapped. One persistent button showing two
                * different drawings on press reads as two different buttons -- a blind
                * read of this control reported exactly that confusion -- and a short
                * dissolve is what says "the same button changed" instead. */}
              <AnimatePresence mode="wait" initial={false}>
                <motion.span
                  key={conductorView ? 'conductor' : 'list'}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  transition={{ duration: 0.12 }}
                  className="flex items-center justify-center"
                >
                  {conductorView ? <ListTree size={14} /> : <List size={14} />}
                </motion.span>
              </AnimatePresence>
            </button>
            )}
            <DropdownMenu open={filterSortOpen} onOpenChange={setFilterSortOpen}>
              <DropdownMenuTrigger asChild>
                {/* The funnel holds the way back, so while a hide withholds rows its
                    title says both that something is withheld and how much.

                    Warn, not accent: the view toggle immediately beside it tints accent
                    to mean "this lane is active", so one accent doing both jobs reads as
                    the toggle's own state rather than as a population kept off screen.

                    The count does NOT go in the accessible name. A button's name names
                    the button; a count that changes under the reader belongs in content,
                    and every lane draws it as content — a reveal row where there are
                    folder headers to hang one from, the lane notice in a board. That also
                    keeps the name stable for a reader navigating by control name. Both
                    numbers read `hiddenFolderCount`, so they cannot disagree. */}
                <FilterMenuButton
                  title={hiddenFolderCount > 0
                    ? i18nT('pages.chatSidebar.sort_filter_sessions_hidden', { count: hiddenFolderCount })
                    : i18nT('pages.chatSidebar.sort_filter_sessions')}
                  aria-label={i18nT('pages.chatSidebar.sort_and_filter_sessions')}
                  badge={filterCounts['unread']}
                  className={hiddenFolderCount > 0 ? 'text-warn' : undefined}
                  data-folder-hide-active={hiddenFolderCount > 0 ? String(hiddenFolderCount) : undefined}
                />
              </DropdownMenuTrigger>
              <FilterMenuContent align="end">
                <FilterMenuLabel>{i18nT('pages.chatSidebar.filter')}</FilterMenuLabel>
                {SESSION_FILTERS.map(filterDef => {
                  const active = activeFilters.has(filterDef.key)
                  const hidden = hiddenFilters.has(filterDef.key)
                  const slotCount = filterCounts[filterDef.key] ?? 0
                  const isRecent = filterDef.key === 'recent'
                  if (isRecent) {
                    // The window picker is a NESTED FLYOUT on a wide viewport and
                    // renders INLINE on a phone. Radix hardcodes a submenu to
                    // side="right" and only lets its popper shift on the cross
                    // axis, so at phone width neither side fits: the flyout lands
                    // on whichever side overflows less and is cut off by the
                    // viewport (measured at 390px: 249px wide, 192px of it past
                    // the right edge, --radix-popper-available-width: 57px). No
                    // width or padding tuning can recover that — the flyout has
                    // nowhere to go beside a menu that already spans most of the
                    // screen, so on a phone the options come inline instead.
                    const picker = (
                      // Non-menu-item controls: stop click/keydown from reaching
                      // Radix so choosing a window doesn't dismiss the menu
                      // (mirrors the folder-rename input pattern).
                      // eslint-disable-next-line jsx-a11y/no-static-element-interactions -- the three handlers only stopPropagation, so this wrapper has no action of its own for a keyboard to reach; the chips and the number input inside are the real controls and each is separately focusable
                      <div
                        onClick={e => e.stopPropagation()}
                        onMouseDown={e => e.stopPropagation()}
                        onKeyDown={e => e.stopPropagation()}
                      >
                        <div className="px-1 pb-1 text-[11px] text-muted">{i18nT('pages.chatSidebar.within')}</div>
                        <div className="flex flex-wrap gap-1 px-1 mb-2">
                          {RECENT_WINDOW_PRESETS.map(preset => (
                            <DurationChip
                              key={preset.ms}
                              label={preset.label}
                              selected={recentWindowMs === preset.ms}
                              onSelect={() => selectRecentPreset(preset.ms)}
                            />
                          ))}
                        </div>
                        <div className="px-1 text-[12px] text-muted">
                          <div className="mb-1">{i18nT('pages.chatSidebar.custom')}</div>
                          <div className="flex items-center gap-1.5">
                            {/* Draft-string value so the field can be cleared
                                / partially typed; commit + clamp on blur or
                                Enter. Unit changes commit immediately but keep
                                the amount as-typed (no re-derivation flip). */}
                            <input
                              type="number"
                              min={1}
                              max={9999}
                              value={recentAmountDraft}
                              onChange={e => setRecentAmountDraft(e.target.value)}
                              onBlur={commitRecentAmount}
                              onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); commitRecentAmount() } }}
                              aria-label={i18nT('pages.chatSidebar.custom_recency_amount')}
                              className="w-12 shrink-0 px-1.5 py-0.5 rounded border border-border bg-bg-elevated text-text text-[12px]"
                            />
                            <SimpleSelect
                              value={recentUnitDraft}
                              onChange={v => changeRecentUnit(v as RecentUnit)}
                              className="px-1.5 py-0.5 text-[12px] rounded"
                              options={['minutes', 'hours', 'days']}
                              optionLabels={[i18nT('pages.chatSidebar.min'), i18nT('pages.chatSidebar.hours'), i18nT('pages.chatSidebar.days')]}
                              aria-label={i18nT('pages.chatSidebar.custom_recency_unit')}
                              // Was `flex-1 min-w-0` on the old <select>; the
                              // trigger's chrome is fixed inside ui/select.tsx,
                              // but the flex sizing has to survive on the
                              // wrapper div that replaces it as the flex item.
                              style={{ flex: '1 1 0%', minWidth: 0 }}
                            />
                          </div>
                        </div>
                      </div>
                    )
                    const rowBody = (
                      <>
                        {filterDef.icon(active)}
                        <span className="flex-1 truncate">
                          {i18nT(FILTER_LABEL_KEY[filterDef.key])}
                          <span className="text-muted"> · {formatRecentWindow(recentWindowMs)}</span>
                          {slotCount > 0 ? ` (${slotCount})` : ''}
                        </span>
                        {active && <Check size={14} className="text-accent shrink-0" />}
                      </>
                    )
                    if (isMobile) {
                      // Inline: the row keeps its only job (toggle the filter) and
                      // the window options sit under it, one tap each. No chevron
                      // — there is nothing left to open.
                      //
                      // The picker is NOT gated on the filter being active. It was,
                      // on the reasoning that picking a window did not enable the
                      // filter so an always-visible picker reported an effect it
                      // was not having — and picking now DOES enable it, at the
                      // commit seam, for every viewport. Keeping the gate would be
                      // the per-modality thinking that caused this defect.
                      return (
                        <Fragment key={filterDef.key}>
                          <DropdownMenuItem
                            title={i18nT(FILTER_DESCRIPTION_KEY[filterDef.key])}
                            onSelect={e => { e.preventDefault(); toggleFilter('recent') }}
                          >
                            {rowBody}
                          </DropdownMenuItem>
                          <div className="px-2 pb-1">{picker}</div>
                        </Fragment>
                      )
                    }
                    // Flyout. The whole row is a single SubTrigger (one focusable
                    // menu item with correct roving-tabindex). Toggling the
                    // filter must be reachable by every input modality:
                    //  - pointer: onClick toggles; we deliberately do NOT
                    //    preventDefault so Radix's own click-to-open still fires.
                    //  - keyboard: Radix routes Enter/Space/ArrowRight to open the
                    //    submenu and the SubTrigger is a div (no synthetic click),
                    //    so onClick never fires for keys. onKeyDown toggles on
                    //    Enter/Space (preventDefault suppresses Radix's open for
                    //    just those keys); ArrowRight falls through and opens.
                    return (
                      <DropdownMenuSub key={filterDef.key}>
                        <DropdownMenuSubTrigger
                          title={i18nT(FILTER_DESCRIPTION_KEY[filterDef.key])}
                          onClick={() => toggleFilter('recent')}
                          onKeyDown={e => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault()
                              toggleFilter('recent')
                            }
                          }}
                        >
                          {rowBody}
                          <ChevronRight size={13} className="text-muted shrink-0" />
                        </DropdownMenuSubTrigger>
                        <DropdownMenuSubContent className="min-w-[190px] p-2">
                          {picker}
                        </DropdownMenuSubContent>
                      </DropdownMenuSub>
                    )
                  }
                  const filterLabel = i18nT(FILTER_LABEL_KEY[filterDef.key])
                  const canHide = !!filterDef.hideStorageKey
                  const showRow = (
                    <DropdownMenuItem
                      key={filterDef.key}
                      title={i18nT(FILTER_DESCRIPTION_KEY[filterDef.key])}
                      // Keep the menu open so multiple filters can be toggled.
                      onSelect={e => { e.preventDefault(); toggleFilter(filterDef.key) }}
                      className={canHide ? 'flex-1 min-w-0' : undefined}
                    >
                      {filterDef.icon(active)}
                      <span className="flex-1 truncate">{filterLabel}{slotCount > 0 ? ` (${slotCount})` : ''}</span>
                      {active && <Check size={14} className="text-accent shrink-0" />}
                    </DropdownMenuItem>
                  )
                  if (!canHide) return showRow
                  // Two menu items drawn as one row, not a button nested in one: a
                  // focusable control inside a menuitem is skipped by the menu's
                  // roving focus and read as part of the row's name. The second
                  // item sits where the check mark would go and is offered only
                  // while show-only is OFF: with it on, the row carries its check
                  // like every other filter row, so a click anywhere on it, the
                  // check included, turns show-only off as it always did, and going
                  // from show-only straight to a hide is two clicks on purpose.
                  // While the chip is hiding the item draws the lit hide icon;
                  // otherwise the icon appears on row hover or focus, and always on
                  // touch screens, which have no hover. The wrapper stays in place
                  // either way so the row is not remounted, which would drop the
                  // menu's focus from it, when show-only flips. The styling sits on
                  // the wrapper and the icon slot, not on the menu primitives.
                  // The wrapper fades with the same `transition-colors` the menu
                  // items carry: an instant wrapper over fading items leaves the
                  // eye item's own padded focus box behind for a beat when the
                  // pointer leaves the row.
                  const hideLabel = hidden
                    ? i18nT('pages.chatSidebar.filter_unhide_named', { filter: filterLabel })
                    : i18nT('pages.chatSidebar.filter_hide_named', { filter: filterLabel })
                  return (
                    <div
                      key={filterDef.key}
                      role="group"
                      className="group/filter flex items-center rounded-md transition-colors hover:bg-bg-hover focus-within:bg-bg-hover"
                    >
                      {showRow}
                      {!active && (
                        <DropdownMenuItem
                          aria-label={hideLabel}
                          title={hideLabel}
                          onSelect={e => { e.preventDefault(); toggleHideFilter(filterDef.key) }}
                          className="group/hide shrink-0"
                          data-testid={`filter-hide-${filterDef.key}`}
                          data-filter-hidden={hidden ? 'true' : undefined}
                        >
                          <span
                            className={hidden
                              ? 'inline-flex size-3.5 items-center justify-center rounded bg-accent-subtle text-accent ring-4 ring-accent-subtle'
                              : 'inline-flex size-3.5 items-center justify-center text-muted group-focus/hide:text-text'}
                          >
                            <EyeOff
                              size={12}
                              className={hidden ? undefined : 'opacity-0 group-hover/filter:opacity-100 group-focus-within/filter:opacity-100 [@media(hover:none)]:opacity-100'}
                            />
                          </span>
                        </DropdownMenuItem>
                      )}
                    </div>
                  )
                })}
                {/* Lift every active status filter at once, for a look at the
                    whole list without losing the filter setup, then put them
                    back. A menu row, not a third control in the chip row:
                    AUTOSDE max-two-buttons-per-row grandfathers that row but
                    forbids growing it, and the chip click keeps its one meaning
                    (clear this filter). Hidden with no active filter, when
                    there is nothing to pause. */}
                {activeFilters.size > 0 && (
                  <DropdownMenuItem
                    data-testid="filter-pause-all"
                    aria-pressed={filtersPaused}
                    onSelect={e => { e.preventDefault(); setAllFiltersPaused(!filtersPaused) }}
                  >
                    {filtersPaused ? <Play size={12} className="text-muted" aria-hidden="true" /> : <Pause size={12} className="text-muted" aria-hidden="true" />}
                    <span className="flex-1 truncate">{filtersPaused ? i18nT('pages.chatSidebar.resume_all_filters') : i18nT('pages.chatSidebar.pause_all_filters')}</span>
                  </DropdownMenuItem>
                )}
                <DropdownMenuSeparator />
                {/* Names its object: this menu also carries "Folder order" two
                    sections down, and a bare "Sort by" over one list beside an
                    order over another read as sorting twice, with a guess about
                    which list each one changes. The members page keeps the bare
                    key -- it has no second list. */}
                <FilterMenuLabel>{i18nT('pages.chatSidebar.sort_sessions_by')}</FilterMenuLabel>
                {SORT_OPTIONS.map(o => (
                  <DropdownMenuItem
                    key={o.value}
                    onSelect={() => { setSortKey(o.value); safeSetItem(SESSION_SORT_STORAGE_KEY, o.value) }}
                  >
                    <span className="flex-1">{i18nT(SORT_LABEL_KEY[o.value])}</span>
                    {sortKey === o.value && <Check size={14} className="text-accent shrink-0" />}
                  </DropdownMenuItem>
                ))}
                {/* Folder order: the same row grammar as Sort by, one section down,
                    because it answers the same kind of question about this list.
                    Custom is the person's own arrangement (drag, or an agent's
                    chat_folder_move); Name and Created are views over it that
                    never rewrite a stored position. Offered in EVERY lane, unlike
                    the stale control below: the flat lane explodes chats out of
                    their folders and the conductor lane nests by lineage, but the
                    mode is not idle there -- every row menu's "Move to folder"
                    picker, the history search's folder groups, the Command Bar,
                    the job form and the MCP tree all list in it -- and this is the
                    only control that writes it. A mode a person cannot change from
                    the lane they are in is the trap. */}
                <DropdownMenuSeparator />
                <FilterMenuLabel>{i18nT('pages.chatSidebar.folder_order')}</FilterMenuLabel>
                {FOLDER_SORT_MODES.map(mode => (
                  <DropdownMenuItem
                    key={mode}
                    data-testid={`folder-order-${mode}`}
                    // "Already the mode" is the value in flight while a save is
                    // pending (the newest pick, queued or on the wire), not the
                    // cache (which moves only when a save lands): pick Name,
                    // reopen, pick Custom -- Custom must go out, not be read as
                    // a no-op against the still-Custom cache. A pick that is a
                    // change goes into `folderSortMut`'s one-at-a-time queue.
                    onSelect={() => {
                      const current = folderSortMut.isPending ? folderSortMut.variables : folderSortMode
                      if (mode !== current) folderSortMut.mutate(mode)
                    }}
                  >
                    <span className="flex-1">{i18nT(FOLDER_SORT_LABEL_KEY[mode])}</span>
                    {folderSortMode === mode && <Check size={14} className="text-accent shrink-0" />}
                  </DropdownMenuItem>
                ))}
                {/* Outside Custom a folder drag can re-parent but not reorder
                    (the rows' droppable side is off). Said here, where the mode
                    is chosen, the same caption grammar as the stale control's
                    paused hint below -- and as a FACT about the modes, not the
                    sidebar hint's "Switch to Custom" sentence: that one sits
                    beside a button that does the switching, and the same words
                    here, two rows under the Custom row itself, read as an action
                    that does nothing. The drop itself speaks again when a drag
                    ends with nothing moved (`folderReorderHint`). Only where a
                    folder row is drawn to drag (`folderRowsDrawn`) -- a note
                    promising a reorder the flat and conductor lanes cannot offer
                    would mislead. */}
                {folderSortMode !== 'custom' && folderRowsDrawn && (
                  <div className="px-2 pt-0.5 pb-1.5 text-[11px] text-muted italic whitespace-normal" data-testid="folder-order-reorder-note">
                    {i18nT('pages.chatSidebar.folder_order_reorder_note')}
                  </div>
                )}
                {/* A folder from before the `created_at` stamp existed has no date, so
                    the created comparator puts it after every stamped row, in the
                    stored order -- on an existing tree that is the order the person
                    already had, and picking the mode looks like nothing happened.
                    The same fact-line pattern as the drag note, shown only while the
                    mode is active and such a folder is in the list (a fresh install
                    never sees it); every folder creator has stamped since the mode
                    shipped, so the line retires as those folders go. */}
                {folderSortMode === 'created' && folders.some(f => !folderHasCreatedStamp(f)) && (
                  // Bounded: the menu sizes to its widest child, and a one-line
                  // sentence this long would widen every row to it. Wrapped at the
                  // width the rows already have, it reads as the caption it is.
                  <div className="px-2 pt-0.5 pb-1.5 text-[11px] text-muted italic whitespace-normal max-w-[300px]" data-testid="folder-order-unstamped-note">
                    {i18nT('pages.chatSidebar.folder_order_unstamped_note')}
                  </div>
                )}
                <DropdownMenuSeparator />
                {/* Stale-session collapse threshold. Lives beside Sort rather than
                    in Settings: it shapes how this list reads, exactly like the
                    sort order, and the Recent filter's window picker set the
                    precedent for a duration control in this menu. Hidden in the
                    flat lane and on the board — those views render rows through
                    paths the collapse does not touch, and a control that
                    displays an active setting while doing nothing is a lie. */}
                {!flatLaneActive && !boardLaneActive && (() => {
                  // The trigger must not advertise "· 2d" while the collapse
                  // is inert (narrowed list / non-date sort): a control that
                  // displays an active setting while doing nothing is a lie.
                  const stalePaused = staleCollapseMs > 0 && (listNarrowed || sortKey !== 'date-desc')
                  const staleRowBody = (
                    <>
                      <Clock size={14} className="text-muted shrink-0" />
                      <span className="flex-1 truncate">
                        {i18nT('pages.chatSidebar.stale_collapse_menu')}
                        <span className="text-muted"> · {staleCollapseMs > 0
                          ? (stalePaused
                            ? i18nT('pages.chatSidebar.stale_collapse_paused')
                            : formatRecentWindow(staleCollapseMs))
                          : i18nT('pages.chatSidebar.stale_collapse_off')}</span>
                      </span>
                    </>
                  )
                  const staleLabel = (ms: number) => (ms > 0 ? formatRecentWindow(ms) : i18nT('pages.chatSidebar.stale_collapse_off'))
                  // Caption saying what the durations mean, mirroring the Recent
                  // submenu's "Within" caption above its presets. While paused the
                  // WHY must be readable without hover — the trigger's title
                  // tooltip is invisible to keyboard and touch users, so the hint
                  // renders in the picker too.
                  const staleCaption = (
                    <>
                      <div className="px-2 pt-1 pb-1 text-[11px] text-muted whitespace-normal">{i18nT('pages.chatSidebar.stale_collapse_caption')}</div>
                      {stalePaused && (
                        <div className="px-2 pb-1.5 text-[11px] text-muted italic whitespace-normal">{i18nT('pages.chatSidebar.stale_collapse_paused_hint')}</div>
                      )}
                    </>
                  )
                  if (isMobile) {
                    // Same reason as the Recent picker above: a flyout cannot fit
                    // beside a phone-width menu. The thresholds render inline as
                    // chips rather than as seven more menu rows, so the menu stays
                    // scannable and every option is one tap away.
                    return (
                      <>
                        {/* A section caption, NOT a menu row: inline, there is
                            nothing to tap here (the chips below carry the action),
                            so styling it like the tappable rows above would invite
                            a tap that does nothing. Matches FILTER / SORT BY. */}
                        <DropdownMenuLabel
                          className="text-[11px] uppercase tracking-[.04em] flex items-center gap-2"
                          data-testid="stale-collapse-menu"
                        >
                          {staleRowBody}
                        </DropdownMenuLabel>
                        {staleCaption}
                        <div className="flex flex-wrap gap-1 px-2 pb-1.5">
                          {STALE_COLLAPSE_PRESETS_MS.map(ms => (
                            <DurationChip
                              key={ms}
                              label={staleLabel(ms)}
                              selected={staleCollapseMs === ms}
                              onSelect={() => setStaleCollapseMs(ms)}
                            />
                          ))}
                        </div>
                      </>
                    )
                  }
                  return (
                <DropdownMenuSub>
                  <DropdownMenuSubTrigger data-testid="stale-collapse-menu"
                    title={stalePaused ? i18nT('pages.chatSidebar.stale_collapse_paused_hint') : undefined}>
                    {staleRowBody}
                    <ChevronRight size={13} className="text-muted shrink-0" />
                  </DropdownMenuSubTrigger>
                  <DropdownMenuSubContent className="min-w-[150px] max-w-[240px]">
                    {staleCaption}
                    {STALE_COLLAPSE_PRESETS_MS.map(ms => (
                      <DropdownMenuItem
                        key={ms}
                        onSelect={() => setStaleCollapseMs(ms)}
                      >
                        <span className="flex-1">{staleLabel(ms)}</span>
                        {staleCollapseMs === ms && <Check size={14} className="text-accent shrink-0" />}
                      </DropdownMenuItem>
                    ))}
                  </DropdownMenuSubContent>
                </DropdownMenuSub>
                  )
                })()}
                {/* Tags. Placed above Folders and NOT gated on the lane: tags are
                    a property of the session, so they mean the same thing in the
                    flat list, the folder tree and the board — and the board is
                    exactly where a phone user is most likely to want this, since
                    the columns scroll sideways there. Folders, by contrast, are a
                    list-view structure and stay hidden on the board. */}
                {tagFilterRows.length > 0 && (
                  <>
                    <DropdownMenuSeparator />
                    <FilterMenuLabel>
                      {i18nT('pages.chatSidebar.tags')}
                    </FilterMenuLabel>
                    {tagFilterRows.map(({ tag: t, count, selected }) => (
                      <DropdownMenuItem
                        key={t.id}
                        title={selected
                          ? i18nT('pages.chatSidebar.stop_filtering_by_tag', { name: t.name })
                          : i18nT('pages.chatSidebar.show_only_sessions_tagged', { name: t.name })}
                        // Keep the menu open so several tags can be selected.
                        onSelect={e => { e.preventDefault(); toggleTagFilter(t.id) }}
                        data-testid={`tag-filter-${t.id}`}
                        role="menuitemcheckbox"
                        aria-checked={selected}
                      >
                        <span
                          aria-hidden="true"
                          className="w-3.5 h-3.5 shrink-0 rounded-[3px] border flex items-center justify-center"
                          style={selected
                            ? { borderColor: t.color, background: t.color }
                            : { borderColor: 'var(--border)', background: 'transparent' }}
                        >
                          {selected && <Check size={10} strokeWidth={3} style={{ color: t.color === '#ffffff' ? '#000' : '#fff' }} />}
                        </span>
                        <span className="flex-1 truncate">{t.name}</span>
                        {/* 0 is rendered, not omitted: a zero-count tag is exactly
                            the one that blanks the list when selected. */}
                        <span className="text-muted text-[11px] shrink-0">{count}</span>
                      </DropdownMenuItem>
                    ))}
                  </>
                )}
                {/* Models. Lists only the models some session uses, most used
                    first, so the section is also the quickest read of what the
                    sessions run on. A session property like tags, so it is not
                    gated on the lane either. */}
                {modelFilterRows.length > 0 && (
                  <>
                    <DropdownMenuSeparator />
                    <FilterMenuLabel>
                      {i18nT('pages.chatSidebar.models')}
                    </FilterMenuLabel>
                    {/* The invert row sits FIRST, under the heading, so the mode
                        that changes what every tick below it means is read before
                        the ticks, not discovered at the bottom of a menu that may
                        be clipped on a short window. Always listed so the "every
                        session not on model X" option can be found before anything
                        is ticked, but disabled until a model is selected:
                        with nothing selected it would flip a flag that changes
                        nothing on screen. Enabled on the RESOLVED selection, so
                        a stored key no session uses cannot arm it either.
                        "Hide" names what the list does. */}
                    <>
                      <DropdownMenuItem
                        disabled={activeModelKeys.size === 0}
                        title={i18nT('pages.chatSidebar.hide_sessions_on_these_models_hint')}
                        onSelect={e => { e.preventDefault(); setModelFilterExcluded(!modelFilterInverted) }}
                        data-testid="model-filter-exclude"
                        role="menuitemcheckbox"
                        aria-checked={modelFilterInverted}
                      >
                        {/* The same checkbox square as the model rows: the on
                            state must be legible as a shape, not only as a
                            colour, and a reader who sees a tick knows a second
                            click clears it. */}
                        <span
                          aria-hidden="true"
                          className={`w-3.5 h-3.5 shrink-0 rounded-[3px] border flex items-center justify-center ${modelFilterInverted ? 'border-accent bg-accent text-accent-fg' : 'border-border bg-transparent'}`}
                        >
                          {modelFilterInverted && <Check size={10} strokeWidth={3} />}
                        </span>
                        <span className="flex-1 truncate">{i18nT('pages.chatSidebar.hide_sessions_on_these_models')}</span>
                        {/* A disabled row says why, in the row: a title tooltip is
                            not shown reliably on a disabled item. Once a model is
                            ticked the note is hidden, not removed: it is the widest
                            row in the menu, and dropping it would shrink the menu
                            and slide every row sideways under the pointer. */}
                        <span
                          className={`text-muted text-[11px] shrink-0 ${activeModelKeys.size === 0 ? '' : 'invisible'}`}
                          aria-hidden={activeModelKeys.size === 0 ? undefined : true}
                          data-testid="model-filter-exclude-note"
                        >{i18nT('pages.chatSidebar.tick_a_model_first')}</span>
                      </DropdownMenuItem>
                      {/* Set apart from the model rows: it is a mode, not another
                          model. */}
                      <DropdownMenuSeparator />
                    </>
                    {modelFilterRows.map(row => {
                      const name = modelFilterLabel(row)
                      // The tooltip names what the click DOES: with the selection
                      // inverted, ticking a row hides its sessions.
                      const unselectedTitle = modelFilterInverted
                        ? i18nT('pages.chatSidebar.hide_sessions_using_model', { name })
                        : i18nT('pages.chatSidebar.show_only_sessions_using_model', { name })
                      return (
                        <DropdownMenuItem
                          key={row.key}
                          title={row.selected ? i18nT('pages.chatSidebar.stop_filtering_by_model', { name }) : unselectedTitle}
                          // Keep the menu open so several models can be selected.
                          onSelect={e => { e.preventDefault(); toggleModelFilter(row.key, modelFilterRows.map(r => r.key)) }}
                          data-testid={`model-filter-row-${row.key}`}
                          role="menuitemcheckbox"
                          aria-checked={row.selected}
                        >
                          {/* The square always shows the tick for a selected row, so
                              a click's effect stays legible. Once the selection is
                              inverted the row reads as "hidden" through a muted name
                              and a trailing eye-off glyph (not a strike-through,
                              which reads as "deleted"). */}
                          <span
                            aria-hidden="true"
                            className={`w-3.5 h-3.5 shrink-0 rounded-[3px] border flex items-center justify-center ${row.selected ? 'border-accent bg-accent text-accent-fg' : 'border-border bg-transparent'}`}
                          >
                            {row.selected && <Check size={10} strokeWidth={3} />}
                          </span>
                          <span
                            className={`flex-1 truncate ${row.selected && modelFilterInverted ? 'text-muted' : ''}`}
                            data-excluded={row.selected && modelFilterInverted ? 'true' : undefined}
                          >{name}</span>
                          {row.selected && modelFilterInverted && <EyeOff size={12} className="text-muted shrink-0" aria-hidden="true" />}
                          <span className="text-muted text-[11px] shrink-0">{row.count}</span>
                        </DropdownMenuItem>
                      )
                    })}
                  </>
                )}
                {/* Folders sit LAST on purpose: the list grows with the user's
                    folder count, so anything below it would get pushed out of
                    easy reach. Being last, it can simply overflow into the
                    menu's own scroll (the DropdownMenuContent primitive caps to
                    the available viewport height and scrolls) with no inner
                    scroll region of its own. */}
                {/* Reachable in EVERY view, the board included. The folder hide applies
                    to the board's own population, so a person who hides a folder and
                    then switches to a board needs the control that reverses it where
                    they are standing: a board column has no folder header, so it carries
                    no reveal row either, and without this section the hide would have no
                    way back short of leaving board view. */}
                {folderFilterRows.length > 0 && (
                  <>
                    <DropdownMenuSeparator />
                    {/* The heading doubles as the shelve control: activating it
                        rolls the folder list up or down. It stays a menu item so
                        keyboard users reach it with the same arrow keys as every
                        other row, and preventDefault keeps the menu open. */}
                    <DropdownMenuItem
                      onSelect={e => { e.preventDefault(); toggleFoldersShelved() }}
                      data-testid="folder-filter-shelve"
                      aria-expanded={!foldersShelved}
                      title={foldersShelved ? i18nT('pages.chatSidebar.show_the_folder_list') : i18nT('pages.chatSidebar.roll_the_folder_list_up')}
                      className="text-[11px] uppercase tracking-[.04em] text-muted"
                    >
                      <DisclosureChevron open={!foldersShelved} size={12} />
                      <span className="flex-1">
                        {i18nT('pages.chatSidebar.folders')}
                        {hiddenFolderCount > 0 && (
                          <span className="normal-case tracking-normal"> &middot; {i18nT('pages.chatSidebar.hidden_folder_count', { count: hiddenFolderCount })}</span>
                        )}
                      </span>
                    </DropdownMenuItem>
                    {!foldersShelved && (
                      <>
                    {filterHiddenFolders.size > 0 && (
                      <DropdownMenuItem onSelect={e => { e.preventDefault(); showAllFolders() }} data-testid="folder-filter-show-all">
                        <RotateCcw size={12} className="text-muted shrink-0" />
                        <span className="flex-1">{i18nT('pages.chatSidebar.show_all_folders')}</span>
                      </DropdownMenuItem>
                    )}
                    {folderFilterRows.map(({ folder: f, depth, count, hidden, hiddenByAncestor }) => (
                      <DropdownMenuItem
                        key={f.id}
                        style={{ paddingLeft: `${8 + depth * 14}px` }}
                        title={hiddenByAncestor
                          ? i18nT('pages.chatSidebar.hidden_because_parent_hidden', { name: f.name })
                          : hidden ? i18nT('pages.chatSidebar.show_folder') : i18nT('pages.chatSidebar.hide_folder')}
                        // Keep the menu open so several folders can be toggled.
                        onSelect={e => { e.preventDefault(); toggleFolderFilter(f.id) }}
                        data-testid={`folder-filter-${f.id}`}
                        role="menuitemcheckbox"
                        aria-checked={!hidden && !hiddenByAncestor}
                      >
                        <span
                          aria-hidden="true"
                          className="w-3.5 h-3.5 shrink-0 rounded-[3px] border flex items-center justify-center"
                          style={hidden || hiddenByAncestor
                            ? { borderColor: 'var(--border)', background: 'transparent' }
                            : { borderColor: 'var(--accent)', background: 'var(--accent)' }}
                        >
                          {!hidden && !hiddenByAncestor && <Check size={10} className="text-accent-fg" strokeWidth={3} />}
                        </span>
                        <FolderGlyph color={f.color} icon={f.icon} size={12} className="shrink-0 text-muted" />
                        <span className={`flex-1 truncate${hiddenByAncestor ? ' opacity-50' : ''}`}>{f.name}</span>
                        {count > 0 && <span className="text-muted text-[11px] shrink-0">{count}</span>}
                      </DropdownMenuItem>
                    ))}
                      </>
                    )}
                  </>
                )}
              </FilterMenuContent>
            </DropdownMenu>
          </>
        )}
      />
      )} shelf={(
        <>
      {/* One aggregate chip in its OWN row, never per-tag chips in the row below.
          AUTOSDE max-two-buttons-per-row grandfathers that row's existing filter
          chips but forbids growing it, and per-tag chips grow it without bound.
          Tag colours survive as spans inside this single control. */}
      {activeTagIds.size > 0 && (
        <div className="px-3 pb-1">
          <button
            type="button"
            data-testid="tag-filter-chip"
            className="inline-flex items-center gap-1 max-w-full pl-2 pr-1 py-0.5 rounded-full text-[11px] cursor-pointer transition-colors bg-bg-elevated/60 border border-border text-muted hover:text-text"
            onClick={clearTagFilter}
            title={i18nT('pages.chatSidebar.clear_named_filter', { filter: fmtList(activeTagNames, { type: 'disjunction' }) })}
            aria-label={i18nT('pages.chatSidebar.clear_named_filter', { filter: fmtList(activeTagNames, { type: 'disjunction' }) })}
          >
            {/* Swatch carries the colour, the name stays in body text: a pale
                tag on this surface can fall near 2:1 contrast at 11px. */}
            <span className="truncate inline-flex items-center gap-1.5">
              {tagFilterRows.filter(({ tag: t }) => activeTagIds.has(t.id)).map(({ tag: t }) => (
                <span key={t.id} className="inline-flex items-center gap-1">
                  <span
                    aria-hidden="true"
                    className="w-2 h-2 shrink-0 rounded-full border border-border"
                    style={{ background: t.color }}
                  />
                  {t.name}
                </span>
              ))}
            </span>
            <X size={11} className="shrink-0" />
          </button>
        </div>
      )}
      {/* The model filter's one chip, in its own row for the same AUTOSDE reason
          as the tag chip above. The label carries the direction: "Not Fable 5"
          when the selection is inverted, else the model names. */}
      {activeModelRows.length > 0 && (() => {
        const models = fmtList(activeModelRows.map(row => modelFilterLabel(row)), { type: 'disjunction' })
        const label = modelFilterInverted ? i18nT('pages.chatSidebar.not_models', { models }) : models
        return (
          <div className="px-3 pb-1">
            <FilterChip
              aggregate
              testId="model-filter-chip"
              label={label}
              clearLabel={i18nT('pages.chatSidebar.clear_named_filter', { filter: label })}
              onClear={clearModelFilter}
            />
          </div>
        )
      })()}
      {activeFilters.size > 0 && (
        <div className={FILTER_CHIP_ROW_CLS}>
          {SESSION_FILTERS.filter(filterDef => activeFilters.has(filterDef.key)).map(filterDef => {
            const slotCount = filterCounts[filterDef.key] ?? 0
            const filterLabel = i18nT(FILTER_LABEL_KEY[filterDef.key])
            // The label goes in as-is. It used to be `.toLowerCase()`d to read as
            // mid-sentence English, which does not survive translation: German
            // nouns are capitalised, CJK has no case, and Turkish lowercases `I`
            // to a dotless `ı`.
            const clearLabel = i18nT('pages.chatSidebar.clear_named_filter', { filter: filterLabel })
            return (
              <FilterChip
                key={filterDef.key}
                testId={`filter-chip-${filterDef.key}`}
                label={`${filterLabel}${filterDef.key === 'recent' ? ` · ${formatRecentWindow(recentWindowMs)}` : ''}${slotCount > 0 ? ` (${slotCount})` : ''}`}
                color={filterDef.color}
                paused={filtersPaused}
                clearLabel={clearLabel}
                onClear={() => toggleFilter(filterDef.key)}
              />
            )
          })}
        </div>
      )}
      {/* The hides, as ONE aggregate chip in their own row, for the same reason as
          the tag chip: the row above holds the base branch's filter chips and must
          not grow, and a chip per hidden status would add up to three more buttons
          to it. Neutral like every clear-several pill (`FilterChip` doc): the one
          click drops every hide at once. Counts ride inside the label the way the
          roster's chip carries its own, and the accessible name says what the
          click does. After the include chips so the strip reads what is kept,
          then what is dropped. */}
      {hiddenFilters.size > 0 && (
        <div className="px-3 pb-1">
          <FilterChip
            aggregate
            label={i18nT('pages.chatSidebar.hiding_named_filter', {
              filter: fmtList(
                SESSION_FILTERS.filter(filterDef => hiddenFilters.has(filterDef.key)).map(filterDef => {
                  const slotCount = filterCounts[filterDef.key] ?? 0
                  return `${i18nT(FILTER_LABEL_KEY[filterDef.key])}${slotCount > 0 ? ` (${slotCount})` : ''}`
                }),
                { type: 'unit', style: 'short' },
              ),
            })}
            clearLabel={i18nT('pages.chatSidebar.filter_unhide_named', {
              filter: fmtList(SESSION_FILTERS.filter(filterDef => hiddenFilters.has(filterDef.key)).map(filterDef => i18nT(FILTER_LABEL_KEY[filterDef.key]))),
            })}
            onClear={clearHiddenFilters}
            testId="hidden-filter-chip"
          />
        </div>
      )}
      {seedError && (
        /* Outside the layout branches on purpose. A TOTAL seed failure leaves
         * zero columns, so the board branch never renders — a banner inside it
         * would be invisible in exactly the case it exists for, while the
         * toggle has already flipped and the user is looking at a list.
         * askAgent: the seed writes derived lanes, no draft in the sidebar.
         * Retry is a separate button, not the notice itself. */
        <div className="mx-2 mt-2 flex flex-col gap-1 shrink-0">
          <ErrorNotice
            title={toggleSaveFailed ? undefined : i18nT('pages.chatSidebar.lane_seed_failed')}
            message={seedError}
            askAgent
            onDismiss={() => { setSeedError(''); setToggleSaveFailed(false) }}
            testId="lane-seed-error"
          />
          {/* The Retry button seeds state lanes — the recovery for a SEED
           * failure. It must not show under a toggle-SAVE failure: there its
           * "Try again" label would promise to retry the save but instead seed
           * lanes into a board the user never switched to (UX Review). The
           * save-failure copy already carries its own next step. */}
          {!toggleSaveFailed && (
            <div>
              <Btn
                type="button"
                className="text-[12px] px-2 py-0.5"
                onClick={() => { setSeedError(''); seedStateLanesMutation.mutate() }}
              >
                {i18nT('pages.chatSidebar.lane_seed_retry')}
              </Btn>
            </div>
          )}
        </div>
      )}
      {/* Yields to the preview's instance-sessions banner, which already reports
       *  a failed ['instances'] read when the preview flag is on. */}
      {hasRemoteExecutedRow && !remoteSessionsError && <RemoteCrewNamesError />}
      {/* Read failures for the two lists this pane is built from. Same placement
       *  rationale as the seed banner: a failed folders query means no folder
       *  tree, a failed columns query means no board, so neither branch can host
       *  its own notice. Both are pure reads — askAgent on, Retry via refetch. */}
      {foldersFailed && (
        <div className="mx-2 mt-2 flex flex-col gap-1 shrink-0">
          <ErrorNotice
            title={i18nT('pages.chatSidebar.folders_load_failed')}
            message={(errMessage(foldersError) || i18nT('components.errorBoundary.something_went_wrong'))}
            askAgent
            testId="chat-folders-error"
          />
          <div>
            <Btn type="button" className="text-[12px] px-2 py-0.5" onClick={() => void refetchFolders()}>
              {i18nT('pages.chatSidebar.retry')}
            </Btn>
          </div>
        </div>
      )}
      {columnsFailed && (
        <div className="mx-2 mt-2 flex flex-col gap-1 shrink-0">
          <ErrorNotice
            title={i18nT('pages.chatSidebar.columns_load_failed')}
            message={(errMessage(columnsError) || i18nT('components.errorBoundary.something_went_wrong'))}
            askAgent
            testId="tag-columns-error"
          />
          <div>
            <Btn type="button" className="text-[12px] px-2 py-0.5" onClick={() => void refetchColumns()}>
              {i18nT('pages.chatSidebar.retry')}
            </Btn>
          </div>
        </div>
      )}
      {/* Folder writes (create / delete / update) and local "New chat" creates.
       *  Inputs are already persisted or were never typed (a create menu pick),
       *  so the hand-off loses nothing. Dismissable: the failure is a moment, not
       *  a state — the caches have already been re-synced. */}
      {/* A refused single-session close. Dismissable: the session is simply still
       *  open, and closing it again is the retry. A "still saving" refusal clears
       *  on its own, so it offers no agent hand-off; any other failure does. */}
      <ErrorNotice
        title={i18nT('pages.chatSidebar.close_refused_title')}
        message={closeRefused
          ? i18nT(closeRefused.reason === 'historyWrite' ? 'pages.chatSidebar.close_refused' : 'pages.chatSidebar.close_refused_other', { title: closeRefused.title })
          : null}
        askAgent={closeRefused?.reason === 'failed'}
        onDismiss={() => dispatch(setCloseRefused(null))}
        className="mx-2 mt-2 shrink-0"
        testId="close-refused"
      />
      <ErrorNotice
        title={i18nT('pages.chatSidebar.folder_update_failed')}
        message={folderActionError}
        askAgent
        onDismiss={() => setFolderActionError('')}
        className="mx-2 mt-2 shrink-0"
        testId="folder-action-error"
      />
      {/* The folder order (dashboard.folder_sort) could not be read AND there is
       *  no body to fall back on: the tree is drawn in the stored order meanwhile
       *  and sibling drags are withdrawn, so the person is told why the order they
       *  chose is not the one they see -- and that nothing is asked of them (the
       *  read retries on its own). Not dismissable: it is a state, not a moment.
       *  Keyed on the LATCHED failure, not the query status, so a retry's pending
       *  phase does not unmount it; it clears when a body arrives. A failed
       *  background refetch with a body on hand says nothing here. */}
      {folderSortRead.error !== null && (
        <ErrorNotice
          title={i18nT('pages.chatSidebar.folder_order_unavailable')}
          message={folderSortRead.error}
          // The server's own words ("config store", "gateway") stay the message --
          // they are the journal key the hand-off reads -- but under the title as
          // a smaller line, not as the lead: the title says what happened.
          messagePlacement="below"
          footer={i18nT('pages.chatSidebar.folder_order_unavailable_detail')}
          askAgent
          // Under the text, not beside it: a sibling column in this ~300px
          // panel left the title and server string one or two words a line.
          actionPlacement="below"
          className="mx-2 mt-2 shrink-0"
          testId="folder-order-unavailable"
        />
      )}
      {/* A folder drag just ended with nothing moved because the drawn order is
       *  not the stored one (see `folderReorderHint`). Status, not error: no
       *  request failed and nothing is broken -- the gesture is withdrawn in this
       *  mode, and this line says where it comes back and offers the way there:
       *  the action writes Custom through the same path the menu row does, so the
       *  person who just tried to reorder does not have to find the menu. It stays
       *  until the person does something else (no clock takes the action away).
       *  Announced live so a keyboard drag gets the same answer a pointer drag does. */}
      {folderReorderHint && (
        <div
          ref={folderReorderHintRef}
          role="status"
          className="mx-2 mt-2 shrink-0 rounded-lg border border-border bg-bg-elevated px-3 py-2 text-[12px] text-muted"
          data-testid="folder-reorder-hint"
        >
          <div className="flex items-center gap-2">
            <ArrowUpDown size={14} className="shrink-0" aria-hidden="true" />
            <span className="min-w-0 flex-1">{i18nT('pages.chatSidebar.folder_order_reorder_hint')}</span>
          </div>
          {/* On its own line, aligned with the sentence: beside it, the two fight
              for the sidebar's width and the sentence wraps to a word a line.
              The shared primitive, restyled to a link: its disabled state (the
              PATCH in flight) then reads disabled instead of keeping the accent
              and the pointer cursor. */}
          <Btn
            className="mt-1 ml-[22px] p-0 border-none bg-transparent rounded-none text-accent hover:bg-transparent hover:underline text-[12px] font-medium active:scale-100"
            data-testid="folder-reorder-hint-switch"
            disabled={folderSortMut.isPending}
            onClick={() => { if (folderSortMode !== 'custom') folderSortMut.mutate('custom') }}
          >
            {i18nT('pages.chatSidebar.folder_order_switch_custom')}
          </Btn>
        </div>
      )}
      <ErrorNotice
        message={newChatError}
        askAgent
        onDismiss={() => setNewChatError('')}
        className="mx-2 mt-2 shrink-0"
        testId="new-chat-error"
      />
      {/* A refused rename: the editor is already closed and the title has been
       *  reverted to the server value by the recovery refetch, so there is no
       *  unsaved draft left to lose and the hand-off is safe. Dismissable: the
       *  failure is a moment, not a state. */}
      <ErrorNotice
        title={i18nT('pages.chatPage.could_not_rename_session')}
        message={renameError}
        askAgent
        onDismiss={() => setRenameError('')}
        className="mx-2 mt-2 shrink-0"
        testId="rename-error"
      />
      <ErrorNotice
        title={i18nT('pages.chatPage.could_not_generate_title')}
        message={autoTitleError}
        askAgent
        // Below, not beside: at sidebar width a side link squeezes the
        // message into a one-word column.
        actionPlacement="below"
        onDismiss={() => setAutoTitleError('')}
        className="mx-2 mt-2 shrink-0"
        testId="auto-title-error"
      />
        {/* An instance that is CONNECTED but did not answer contributes no rows.
          *  Saying so is the difference between "that instance has nothing open" and
          *  "we could not ask": without this line the list silently claims a
          *  completeness it does not have, which is worse than showing fewer rows.
          *  Placed ABOVE the view branches so it appears in the flat lane, the board
          *  and the folder tree alike — an unreachable peer is not a property of one
          *  layout. Non-blocking by design: local rows are unaffected. */}
        {instanceSessions.loading && (
          /* The same honesty rule as the notice below, for the window BEFORE any
           *  peer has answered: remote rows land seconds after mount, so a list
           *  that stays silent until then reads as complete while it is not, and
           *  the arriving rows shift the list under a scan already in progress.
           *  Muted single line in the same slot, so the two states cannot stack
           *  into competing banners. */
          <div className="mx-2 mt-2 px-2 py-1.5 text-[11px] text-muted flex items-center gap-1.5">
            <Server size={11} aria-hidden="true" className="shrink-0" />
            <span className="min-w-0 truncate">
              {i18nT('pages.chatSidebar.checking_remote_instances')}
            </span>
          </div>
        )}
        {remoteSessionsError && (
          /* A peer read that failed, through the one shared error surface — see
           *  `remoteSessionsError` for how the two failing reads collapse into it.
           *  `askAgent` is ON: this is a LIST read, so the hand-off's navigation
           *  destroys no unsaved state, and a tunnel that stopped answering is
           *  exactly the class of failure the user cannot fix by hand but the
           *  agent often can. Not dismissible: the condition is live, so a
           *  dismissed banner would reappear on the next 15s refetch.
           *  `shrink-0` matches the new-chat notice above it — the rail is a
           *  flex column and a growable banner would eat the list's height. */
          <ErrorNotice
            title={remoteSessionsError.title}
            message={remoteSessionsError.message}
            askAgent
            actionPlacement="below"
            className="mx-2 mt-2 shrink-0"
            testId="instance-sessions-error"
          />
        )}
        </>
      )}>
      <LayoutGroup id="chat-slots">
        {conductorLaneActive ? (
          // Conductor lane: every session nested under the session that OPENED it.
          //
          // The row is the EXISTING session card, unchanged — agent label, time,
          // title, preview, PR chips, loop status, tags, needs-you. The lane adds
          // three things and nothing else: indentation per depth, a chevron with a
          // child count on a card that has children, and that subtree's aggregated
          // badges while the card is collapsed. No compact row design: a nested
          // session is the same object as a top-level one and reads the same.
          //
          // Search flattens it, the way the flat lane does: a query must reach every
          // match, so a match three levels down inside a collapsed conductor cannot be
          // hidden behind a chevron the user would have to guess at.
          //
          // DnD is off, like the flat lane's row order: position here is a function of
          // who opened whom, so there is nothing a drop inside the lane could land on.
          <SessionRowWindowContext.Provider value={laneRowWindow.rowWindow}>
          <motion.div ref={setLaneScrollEl} onScroll={laneScrollMemory.onScroll} layoutScroll={rowAnimEnabled} className={`${LIST_BODY_CLS} flex flex-col`} style={{ scrollbarWidth: 'none' }} data-testid="conductor-view-lane">
            {folderCreateError && renderFolderCreateError(folderCreateError.folderId, folderCreateError.columnId)}
            {localGroupHeader}
            {(() => {
              const tree = lineage
              if (!tree) return null
              const searching = slotFilter.trim() !== ''
              // Keyed by identity, exactly as `lineage` is: a raw-key map would let a
              // federated peer row overwrite the local row it collides with, so one
              // session would vanish and the other would render twice.
              const byKey = new Map(conductorRows.map(s => [sessionRowIdentity(s), s] as const))
              // The rows this lane may show: every match, plus each ancestor a match
              // needs to hang from. An ancestor is on screen as CONTEXT -- the filter
              // did not admit it -- so it renders dimmed and still carries its chevron.
              // Without it a filtered-out conductor's workers each resolved no parent
              // and scattered to the top level, which is what switching on Unread did.
              const kept = new Set<string>()
              for (const id of conductorMatching) {
                if (!byKey.has(id)) continue
                kept.add(id)
                for (const up of ancestorsOf(id, tree.parentOf)) kept.add(up)
              }
              const keptKids = (key: string) =>
                (tree.children.get(key) ?? []).filter(k => kept.has(k))
              const rows: Array<{ id: string; slot: Slot; depth: number; childCount: number; expanded: boolean; orphanOf: string | null; citesParent?: string | null; anchorOnly: boolean; aggregate: { needsYou: number; running: number } | null }> = []

              /** Does this row want the user? The same two signals the row itself
               *  renders as a dot or a subtitle, so a collapsed conductor's badge and
               *  its children's badges can never disagree. */
              // Both predicates are derived from the state the CHILD ROWS themselves
              // render from, because the collapsed aggregate claims to be the same fact
              // at a coarser zoom: a count that disagrees with the glyphs it stands for
              // is worse than no count. `runningSet` is the widened signal -- own turn,
              // a live workflow, or a loop -- so a workflow-active child is counted the
              // way its own row is drawn, and a subagent awaiting approval is an ask
              // even though the slot itself carries no `pending_approval`.
              const isRunning = sidebarRows.isRunning
              const wantsUser = (s: Slot) =>
                !!(
                  s.pending_approval
                  || s.needs_input
                  || (subagentApprovalCounts[s.key] || 0) > 0
                  || (unreadSet.has(s.key) && !isRunning(s))
                )

              const emit = (key: string, depth: number) => {
                const slot = byKey.get(key)
                if (!slot) return
                const kids = keptKids(key)
                const expanded = conductorExpanded.has(key)
                const subtree = kids.length > 0 && !expanded
                  ? descendantsOf(key, tree.children).filter(k => kept.has(k))
                  : []
                // Which of the two citation glyphs this row earns. `orphanCitation` only
                // knows the row was placed under nothing; whether that is because the
                // creator closed or because the folder filter conceals it is decided
                // against the unfiltered population. A creator that is still there is
                // open and running, so saying it closed would be false.
                //
                // `closedCreatorCitation` is the row the backend re-parented: it IS
                // placed, under the nearest ancestor still open, and its own creator
                // closed. Same fact as an orphan's, so the same glyph, and the backend
                // said so outright -- which is why `ancestor` settles the reading here
                // rather than the population being consulted for it.
                const placedUnder = tree.parentOf.get(key) ?? null
                const cited = orphanCitation(slot, placedUnder)
                  ?? closedCreatorCitation(slot, placedUnder)
                const creator = citedCreatorOf(slot)
                const creatorStillOpen = cited != null && slot.parent?.ancestor !== true
                  && creator !== null
                  && (citedCreatorExists.get(creator.origin)?.has(creator.key) ?? false)
                rows.push({
                  id: key,
                  slot,
                  depth,
                  childCount: kids.length,
                  expanded,
                  orphanOf: creatorStillOpen ? null : cited,
                  citesParent: creatorStillOpen ? cited : null,
                  anchorOnly: !conductorMatching.has(key),
                  // Only a COLLAPSED conductor aggregates: while it is open its
                  // children show their own badges, and showing both would count the
                  // same session twice on one screen.
                  aggregate: subtree.length > 0
                    ? {
                      needsYou: subtree.filter(k => { const c = byKey.get(k); return c ? wantsUser(c) : false }).length,
                      running: subtree.filter(k => { const c = byKey.get(k); return c ? isRunning(c) : false }).length,
                    }
                    : null,
                })
                if (!expanded) return
                for (const kid of kids) emit(kid, depth + 1)
              }

              if (searching) {
                // Flattened: every match at depth 0, in the lane's order, with no
                // chevrons. Matches the flat lane's answer to the same question.
                for (const s of flatSlots) {
                  // The cited creator rides along even though the lane is not nesting:
                  // flattened, a child is otherwise indistinguishable from a root.
                  rows.push({ id: sessionRowIdentity(s), slot: s, depth: 0, childCount: 0, expanded: false, orphanOf: null, citesParent: s.parent?.slot ?? null, anchorOnly: false, aggregate: null })
                }
              } else {
                for (const key of tree.roots) if (kept.has(key)) emit(key, 0)
              }

              return rows.map((row, i) => {
                const next = i < rows.length - 1 ? rows[i + 1] : null
                const isActive = isActiveRow(row.slot)
                const showDivider = next != null && !isActive && !isActiveRow(next.slot)
                return (
                  <Fragment key={row.id}>
                    {/* NO WRAPPER. The row is rendered exactly as the flat lane renders
                     *  it, and the lane's three additions are handed INTO it (see
                     *  `ConductorRowView`, and the chevron's `toggleConductor` action).
                     *  Wrapping the card in a flex column with the chevron and counts
                     *  beside it narrowed the card: its title truncated early, its own
                     *  divider stopped short of the row, and the counts took the column
                     *  the top line keeps for the time. */}
                    {renderSessionRow(row.slot, row.depth, showDivider, 'conductor', 'conductor', 'conductor', {
                      depth: row.depth,
                      childCount: row.childCount,
                      expanded: row.expanded,
                      aggregate: row.aggregate,
                      orphanOf: row.orphanOf,
                      citesParent: row.citesParent ?? null,
                      anchorOnly: row.anchorOnly,
                    })}
                  </Fragment>
                )
              })
            })()}
            {flatSlots.length === 0 && shownCrewRowCount === 0 && (
              <div className="px-3 py-4 text-[12px] text-muted">{i18nT('pages.chatSidebar.no_sessions_match')}</div>
            )}
            {flatSlots.length > 0 && lineage != null && lineage.children.size === 0 && allHiddenFolders.length === 0 && (
              // Not an error state: the crew log may be off, or nothing has opened
              // anything yet. The lane still shows every session -- it just has no
              // nesting to show, and says so instead of looking broken.
              //
              // Withheld while this lane is concealing a folder, because then the note
              // cannot be read as intended: the rows above it are live sessions, and the
              // reveal row immediately below already says how many folders are hidden,
              // which is the actual reason there is no nesting left to draw.
              <div className="px-3 py-2 text-[11px] text-muted select-none" data-testid="conductor-lane-empty-note">
                {i18nT('pages.chatSidebar.no_conductor_sessions_yet')}
              </div>
            )}
            {renderHiddenReveal('conductor', allHiddenFolders, 0)}
            {renderOlderSessionsHint('conductor')}
            {renderCrewGroups()}
          </motion.div>
          </SessionRowWindowContext.Provider>
        ) : flatLaneActive ? (
          // Flat view: every chat exploded out of its folder into one lane.
          // Removes only the folder rendering hierarchy — sort, pin priority,
          // filters, and search all apply as usual (filteredSlots). No folder
          // tree. This lane only renders when NO tag columns exist: with a
          // board configured, flat view applies inside each column instead
          // (see the column body), so the board never silently disappears.
          // Inactive without folders (the toggle is hidden then too), so a
          // persisted flat preference can never strand the user.
          //
          // Its DndContext carries EXACTLY ONE target: the chat pane. No
          // SortableContext and no folder droppables are registered, so
          // dragging a session into the open chat works here just as it does in
          // the tree, while row order stays a pure function of the sort key —
          // there is nothing for a drop inside the lane to land on. (Order is
          // the reason: a flat lane spans every folder, so a manual position
          // would have no place to be stored.) `sidebarCollision` also keeps the
          // pane out of its closest-edge fallback, so a release inside the
          // sidebar resolves to no target rather than snapping to the pane.
          <DndContext sensors={dndSensors} collisionDetection={sidebarCollision}
            measuring={dndMeasuring}
            onDragStart={handleSidebarDragStart} onDragEnd={handleSidebarDragEnd} onDragCancel={handleSidebarDragCancel}>
            <DndActiveProbe report={reportDndActive} />
            {chatDropTarget && onDropSessionRef && activeDrag?.type === 'session'
              && createPortal(
                <ChatPaneDropZone refusal={draggingRefRefusal} />,
                chatDropTarget,
              )}
            <SessionRowWindowContext.Provider value={laneRowWindow.rowWindow}>
            <motion.div ref={setLaneScrollEl} onScroll={laneScrollMemory.onScroll} layoutScroll={rowAnimEnabled} className={`${LIST_BODY_CLS} flex flex-col`} style={{ scrollbarWidth: 'none' }} data-testid="flat-view-lane">
              {/* Flat view renders no folder headers, so the per-folder mount
               *  points for the create-failure notice never exist here — yet
               *  the New menu still offers "New chat in folder". Render the
               *  notice at the top of the lane so a failed folder create is
               *  never console-only in this layout. */}
              {folderCreateError && renderFolderCreateError(folderCreateError.folderId, folderCreateError.columnId)}
              {localGroupHeader}
              {(() => {
                // Date segments (Today / Yesterday / Last 7 Days / …) between
                // rows — resurrects the 9bb0f71 active-list pattern: only for
                // date sorts (segments mislead on name/created order, same
                // guard as the history pane), and pinned rows render first
                // without segments since pinning overrides date order.
                const isDateSort = sortKey === 'date-desc' || sortKey === 'date-asc'
                // Hoisted above the hold so the pixel anchor can count header heights.
                const baseSeg = (s: Slot) => (isDateSort && !sidebarRows.isPinned(s) ? dateSegment(slotActivityTs(s)) : '')
                const { rows: flatRows, navScope: flatLaneScope, container: flatHoldContainer } = heldLane(flatSlots, 'flat', 'flat', baseSeg)
                // Reads the flag holdHovered just set for THIS lane, so the header
                // rule and the hold cannot disagree about the row being displaced.
                const heldKey = heldDisplacedRef.current ? hoverPinRef.current?.key : undefined
                // Unconditional exclusion would drop the header of a bucket whose
                // sole row — or the lane's top row — is merely being hovered.
                const segOf = (s: Slot) => {
                  const seg = baseSeg(s)
                  return seg && s.key === heldKey ? '' : seg
                }
                let prevSeg = ''
                return flatRows.map((s, i) => {
                  const seg = segOf(s)
                  const showHeader = seg !== '' && seg !== prevSeg
                  if (seg) prevSeg = seg
                  const next = i < flatRows.length - 1 ? flatRows[i + 1] : null
                  const nextIsActive = isActiveRow(next)
                  const isActive = isActiveRow(s)
                  // No divider before a segment header — the header separates.
                  const nextSeg = next ? segOf(next) : seg
                  const showDivider = next != null && !isActive && !nextIsActive && nextSeg === seg
                    && !startsAutomaticSection(flatSlots, i + 1)
                  return (
                    <Fragment key={sessionRowIdentity(s)}>
                      {startsAutomaticSection(flatSlots, i) && <PinnedSessionDivider />}
                      {showHeader && (
                        <div data-date-header data-testid="date-segment-header" className="px-3 pt-3 pb-1 text-[11px] font-semibold text-muted uppercase tracking-[.06em] select-none first:pt-1">{seg}</div>
                      )}
                      {renderSessionRow(s, 0, showDivider, flatLaneScope, flatLaneScope, flatHoldContainer)}
                    </Fragment>
                  )
                })
              })()}
              {flatSlots.length === 0 && shownCrewRowCount === 0 && (
                <div className="px-3 py-4 text-[12px] text-muted">{i18nT('pages.chatSidebar.no_sessions_match')}</div>
              )}
              {/* Flat view has no containers to anchor to — every hide, top-level
               *  or nested, collapses into this one row at the bottom of the lane. */}
              {renderHiddenReveal('flat', allHiddenFolders, 0)}
              {renderOlderSessionsHint('flat')}
              {renderCrewGroups()}
            </motion.div>
            </SessionRowWindowContext.Provider>
            {dragOverlay}
          </DndContext>
        ) : orderedColumns.length === 0 ? (
          // Legacy single-lane layout (identical to pre-columns behavior)
          // Scrollbar hidden (scrollbar-none + inline scrollbarWidth covers
          // Firefox, modern WebKit, and Safari <16) to match the app rail in
          // App.tsx: on macOS with "always show scrollbars" this lane is
          // permanently scrollable, so the 6px track was a fixed stripe down
          // the sidebar rather than a transient hint. Scrolling itself is
          // untouched — wheel, trackpad, keyboard, and drag-autoscroll all
          // still work, and the list's own overflow is still the affordance.
          <SessionRowWindowContext.Provider value={laneRowWindow.rowWindow}>
          <motion.div ref={setLaneScrollEl} onScroll={laneScrollMemory.onScroll} layoutScroll={rowAnimEnabled} className={`${LIST_BODY_CLS} flex flex-col`} style={{ scrollbarWidth: 'none' }} data-testid="tree-view-lane">
            {/* Scroll floor for collapseFloor.ts: lets a folder collapsed near
             *  the end of the list stay where its pinned header was instead of
             *  the list sliding down. Zero height and first in the lane, so
             *  nothing above it can move it; its hidden child is placed only
             *  during a collapse. */}
            <div {...{ [COLLAPSE_FLOOR_ATTR]: '' }} aria-hidden="true" className="relative h-0 shrink-0">
              <div className="absolute left-0 w-px h-px hidden pointer-events-none" />
            </div>
            {/* Tree-lane fallback, completing the set (flat and board lanes
             *  carry the same): a create into a folder the folder-filter or
             *  hide feature excludes never renders that folder's header, so
             *  its scoped notice mount does not exist. Renders exactly when
             *  the scoped mount cannot (folderCreateMountAbsent). */}
            {folderCreateError && folderCreateMountAbsent(folderCreateError.folderId) && renderFolderCreateError(folderCreateError.folderId, folderCreateError.columnId)}
            {/* One DndContext owns folder reorder (sortable) + session drag-to-
             *  assign (draggable rows + droppable folder/root targets). */}
            <DndContext sensors={dndSensors} collisionDetection={sidebarCollision}
              measuring={dndMeasuring}
              onDragStart={handleSidebarDragStart} onDragOver={handleSidebarDragOver} onDragEnd={handleSidebarDragEnd} onDragCancel={handleSidebarDragCancel}>
              <DndActiveProbe report={reportDndActive} />
              {/* "Drag a session into the open chat" target. Portaled into
               *  ChatPage's pane so it covers the WHOLE conversation area (not
               *  just the composer), while staying inside this DndContext —
               *  React portals preserve context, and useDroppable measures the
               *  node where it actually renders. Mounted only during a session
               *  drag, and only when a pane and a handler exist. */}
              {chatDropTarget && onDropSessionRef && activeDrag?.type === 'session'
                && createPortal(
                  <ChatPaneDropZone refusal={draggingRefRefusal} />,
                  chatDropTarget,
                )}
              {/* Root lane is the fallback drop target: dropping a session on
               *  empty space (not over a folder) ungroups it (folderId: null). */}
              <DndDroppable id="root-lane" data={{ type: 'folder-drop', folderId: null }}>
                {({ setNodeRef }) => (
                  <div ref={setNodeRef} className="flex flex-col flex-1 min-h-0">
                    {localGroupHeader}
                    <SortableContext items={rootFolderIds} strategy={verticalListSortingStrategy}>
                      {visibleRootFolders.map(f => <SortableFolderBlock key={f.id} folder={f} subtree={[...(folderSubtrees.get(f.id) ?? collectFolderSubtreeIds(folders, f.id))]} siblings={rootFolderIds} reorderable={folderReorderable} dragWithheld={folderDragWithheld} renderFolderBlock={renderFolderBlock} />)}
                    </SortableContext>
                    {/* Bottom of the ROOT folder list. For a top-level hide this
                     *  is the sidebar's own bottom, which is exactly the "single
                     *  footer row" shape — the nested case is what needs depth. */}
                    {renderHiddenReveal('root', hiddenByContainer.get('root') ?? [], 0)}
                    {/* Every folder block and the ungrouped bucket read
                        laneSlots, so an empty one means nothing can render
                        below — say so rather than leaving a blank lane.
                        A folder-NAME search is the case where the plain wording
                        lies: the matched folder rows are rendered directly above
                        this line, so "No sessions match" alone reads as a
                        contradiction ("no conversations matched, even though two
                        folders did"). `folderNameMatchIds` is non-null only when
                        at least one folder name matched, which is exactly when
                        those rows are on screen, so it is the condition — not a
                        proxy for it.
                        The wording deliberately does NOT point at "the folders
                        above": the tree keeps a folder that merely holds a
                        subfolder, so a row that matched nothing can sit in that
                        list, and a sentence claiming it matched is the same
                        contradiction with the roles reversed. It claims only that
                        a folder name matched; the marks say which row. */}
                    {laneSlots.length === 0 && shownCrewRowCount === 0 && listNarrowed && (
                      <div className="px-3 py-4 text-[12px] text-muted">{i18nT(folderNameMatchIds ? 'pages.chatSidebar.no_sessions_match_folders' : 'pages.chatSidebar.no_sessions_match')}</div>
                    )}
                    {/* Ungrouped sessions live in a headerless droppable bucket
                     *  (folderId: null) that fills the remaining height below the
                     *  folders, so the whole empty lower area is a drop target —
                     *  dropping a session here ungroups it. The ring only lights up
                     *  while dragging a foldered session (when ungrouping applies). */}
                    {(rootFolders.length > 0 || ungroupedSlots.length > 0 || shownCrewGroups.length > 0) && (
                      <DndDroppable id="root-group" data={{ type: 'folder-drop', folderId: null }}>
                        {({ setNodeRef: setRootGroupRef, isOver }) => (
                          <div ref={setRootGroupRef} className={`flex flex-col flex-1 min-h-0 rounded-md transition-all ${isOver && (draggingFolderedSession || draggingNestedFolder) ? 'ring-1 ring-accent' : ''}`}>
                            {/* Explicit un-nest target while dragging a subfolder —
                             *  same escape hatch (and wording) as the session zone
                             *  below, always reachable even when the root lane has
                             *  no empty space. */}
                            {draggingNestedFolder && <RootDropHint />}
                            {(() => {
                              const { fresh: freshRootRaw, stale: staleRoot } = splitStale(ungroupedSlots)
                              const { rows: freshRoot, navScope: treeRootScope, container: treeRootContainer } = heldLane(freshRootRaw, 'list', 'tree:root')
                              return (
                                <>
                                  {freshRoot.map((s, i) => {
                                    const nextIsActive = isActiveRow(freshRoot[i + 1])
                                    const isActive = isActiveRow(s)
                                    const showDivider = i < freshRoot.length - 1 && !isActive && !nextIsActive
                                      && !startsAutomaticSection(freshRoot, i + 1)
                                    return (
                                      <Fragment key={sessionRowIdentity(s)}>
                                        {startsAutomaticSection(freshRoot, i) && <PinnedSessionDivider />}
                                        {renderSessionRow(s, 0, showDivider, treeRootScope, treeRootScope, treeRootContainer)}
                                      </Fragment>
                                    )
                                  })}
                                  {!searchRanked && staleExpanded.has('root')
                                    && staleRoot.length > 0 && freshRoot.length > 0
                                    && pinned.has(freshRoot[freshRoot.length - 1].key) && <PinnedSessionDivider />}
                                  {renderStaleSection('root', staleRoot, 0)}
                                  {/* After the dormant expander, so it stays the
                                   *  lane's last line even when rows are folded. */}
                                  {renderOlderSessionsHint('root')}
                                  {renderCrewGroups()}
                                </>
                              )
                            })()}
                            {ungroupedSlots.length === 0 && draggingFolderedSession && <RootDropHint />}
                          </div>
                        )}
                      </DndDroppable>
                    )}
                  </div>
                )}
              </DndDroppable>
              {dragOverlay}
            </DndContext>
          </motion.div>
          </SessionRowWindowContext.Provider>
        ) : (
          // Trello-style horizontal column strip. The columns scroll on their own
          // inside the strip, so the lane itself steps below the floating dock
          // (ListDock) rather than scrolling under it; the hidden-folders line and
          // the board error above the strip stay reachable that way.
          <div className="flex-1 min-h-0 flex flex-col pt-[var(--list-dock-h,0.5rem)]">
          {/* Lane-level fallback ownership (exactly one mount ever renders):
           *  - no columnId (New-menu create): no per-column mount exists;
           *  - board-flat: the columnId-scoped mounts are hidden with folders;
           *  - the error's column was deleted: its mount is gone for good;
           *  - the target FOLDER was deleted mid-flight: no mount anywhere.
           *  With folders shown, the column alive and the folder present, the
           *  column's own mount wins and this line is false. */}
          {folderCreateError && (flatView || !folderCreateError.columnId || !orderedColumns.some(c => c.id === folderCreateError.columnId) || folderCreateMountAbsent(folderCreateError.folderId)) && renderFolderCreateError(folderCreateError.folderId, folderCreateError.columnId)}
          {/* Board writes (delete / reorder / add-after / card drop) report here,
           *  above the strip. Column payloads are server-side, so nothing can be
           *  lost by handing off; the caches were re-synced in the onError. */}
          <ErrorNotice
            title={i18nT('pages.chatSidebar.board_update_failed')}
            message={boardError}
            askAgent
            onDismiss={() => setBoardError('')}
            className="mx-2 mt-2 shrink-0"
            testId="board-error"
          />
          {/* Below the two error notices above, not beside them: this is a
            *  standing fact about the layout, not something that just went
            *  wrong, so it must never push a failure the user has to act on
            *  further down the lane.
            *
            *  The experimental peer-session merge is a FLAT/LIST affordance.
            *  Board columns remain local-slot containers because their tags,
            *  lanes and drop actions are local mutations. Every filtered peer
            *  row is therefore omitted here and represented by this exact count. */}
          {boardPeerCount > 0 && (
            <div className="mx-2 mt-2 px-2 py-1.5 rounded-md bg-info-subtle border border-info/40 text-info text-[11px] flex items-center gap-1.5">
              <Server size={11} aria-hidden="true" className="shrink-0" />
              <span className="min-w-0">
                {i18nT('pages.chatSidebar.remote_sessions_not_shown_in_board_view', { count: boardPeerCount })}
              </span>
            </div>
          )}
          {/* The hide's trace in the board lane, as a row rather than as a tint.
            *
            * The other three lanes end a container with a reveal row, which a board
            * cannot copy: a column draws no folder header for such a row to hang from,
            * and a hidden folder is not a property of any one column anyway — its
            * sessions scatter across all of them, so a per-column row would print the
            * same count once per column. So it sits at the LANE level, beside the notice
            * above that reports the other population a board declines to draw.
            *
            * A row and not just the funnel's tint, because the hide is persistent: it
            * lives in localStorage and survives a reload, so a tint and a hover count are
            * all a returning reader has to account for sessions that are simply fewer
            * than they were. The honest reading of that is deletion.
            *
            * It is a button, and it opens the filter menu, because that menu holds the
            * undo. It also UNSHELVES the menu's folder list on the way: that list is the
            * way back and it is gated behind the shelf, so opening the menu over a
            * rolled-up shelf lands the reader on a dense panel with no folders in it and
            * the word on the button promises something that did not happen. The other
            * lanes' row peeks the folders open in place; this is the same gesture as far
            * as a board can carry it. */}
          {hiddenFolderCount > 0 && (
            <button
              type="button"
              onClick={() => {
                setFoldersShelved(false)
                safeSetItem(FOLDERS_SHELVED_LS_KEY, '0')
                setFilterSortOpen(true)
              }}
              title={i18nT('pages.chatSidebar.show_hidden_folders_from_board', { count: hiddenFolderCount })}
              aria-label={i18nT('pages.chatSidebar.show_hidden_folders_from_board', { count: hiddenFolderCount })}
              data-testid="board-hidden-folders"
              data-hidden-folder-count={String(hiddenFolderCount)}
              className="mx-2 mt-2 px-2 py-1.5 rounded-md bg-warn-subtle border border-warn/40 text-warn-text text-[11px] flex items-center gap-1.5 text-left cursor-pointer hover:bg-warn/20 transition-colors"
            >
              <EyeOff size={11} aria-hidden="true" className="shrink-0" />
              <span className="min-w-0 truncate" data-testid="board-hidden-folders-count">
                {i18nT('pages.chatSidebar.hidden_folder_count', { count: hiddenFolderCount })}
              </span>
              {/* The action, in VISIBLE text and not only in the name. A count plus a
                *  glyph tells a sighted pointer-less reader that rows are withheld and
                *  leaves them to guess the row is tappable, which is the hover-only
                *  failure this row exists to end. `show` is the catalog's own word for
                *  this affordance, so the 13 locales already carry it. The chevron is
                *  decorative: the word beside it already says what happens. */}
              <span className="ml-auto shrink-0 inline-flex items-center gap-0.5 underline decoration-dotted underline-offset-2"
                data-testid="board-hidden-folders-action">
                {i18nT('pages.chatSidebar.show')}
                <ChevronRight size={11} aria-hidden="true" className="shrink-0" />
              </span>
            </button>
          )}
          <div className="relative flex-1 min-h-0 flex flex-col">
          <div ref={attachStripEdges} className="flex-1 overflow-x-auto overflow-y-hidden flex gap-2 p-2" data-testid="column-strip">
            {orderedColumns.map((col, colIdx) => {
              // The board pool already leaves out every row the folder filter conceals,
              // and this one population feeds every render site below: the flat-board
              // rows, every folder block's body through `colSlotKeys`, each block's
              // aggregate count, and the "no sessions" notice. The board lane has no
              // reveal row (a column has no folder header for one to hang from), so the
              // way back is re-checking the folder in the filter menu.
              const colSlots = sidebarRows.lanes.board.filter(s => columnMatches(col, s))
              const colTags = col.tag_ids.map(tid => tagById[tid]).filter(Boolean) as ChatTag[]
              const laneDef = col.source === 'state' ? SESSION_LANES.find(l => l.key === col.state_key) : undefined
              // Only a single-status-tag column can accept a card: dropping onto a
              // derived lane has nothing to write (the backend refuses it too).
              const isStatusLane = !laneDef && colTags.length === 1 && !!colTags[0].status
              return (
                // Board column is a drag-and-drop drop zone (column reorder + session
                // card drop); mouse-only drag handlers, so scope-disable the rule.
                // eslint-disable-next-line jsx-a11y/no-static-element-interactions
                <div key={col.id} data-testid={`column-${col.id}`} className="flex flex-col flex-1 min-w-0 bg-card border border-border rounded-md overflow-hidden" style={{ minWidth: orderedColumns.length > 1 ? '220px' : undefined }}
                  // Clear first, on the way down: a folder block or unfile strip
                  // inside the lane takes the card and stops the dragover, so only
                  // a dragover that reaches the lane itself shows the hint again.
                  onDragOverCapture={laneDef ? () => setDerivedHintCol(c => (c === col.id ? null : c)) : undefined}
                  onDragOver={e => {
                    const types = e.dataTransfer.types
                    // Accept column reorder on the entire column surface
                    if (types.includes('application/mc-column')) {
                      e.preventDefault()
                      return
                    }
                    // A derived lane leaves the drop refused (no preventDefault)
                    // and shows why.
                    if (laneDef && types.includes('text/plain')) {
                      setDerivedHintCol(col.id)
                      return
                    }
                    // Accept session-card drop only on status lanes
                    if (isStatusLane && types.includes('text/plain')) {
                      e.preventDefault()
                      e.currentTarget.classList.add('ring-1', 'ring-accent')
                    }
                  }}
                  onDragLeave={e => {
                    e.currentTarget.classList.remove('ring-1', 'ring-accent')
                    // Moving onto a child also fires dragleave; only leaving the lane clears.
                    if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDerivedHintCol(c => (c === col.id ? null : c))
                  }}
                  onDrop={e => {
                    e.currentTarget.classList.remove('ring-1', 'ring-accent')
                    setDerivedHintCol(null)
                    // Column reorder takes priority
                    const draggedCol = e.dataTransfer.getData('application/mc-column')
                    if (draggedCol && draggedCol !== col.id) {
                      e.preventDefault()
                      const ids = orderedColumns.map(c => c.id).filter(id => id !== draggedCol)
                      ids.splice(colIdx, 0, draggedCol)
                      reorderColumnsMutation.mutate(ids)
                      return
                    }
                    if (!isStatusLane) return
                    e.preventDefault()
                    const k = e.dataTransfer.getData('text/plain')
                    if (k) dropSlotMutation.mutate({ slot: k, columnId: col.id })
                  }}>
                  <div className="relative flex items-center gap-1 p-2 border-b border-border bg-bg-elevated">
                    {/* Reorder handle: mouse-only drag source for column reordering. */}
                    {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
                    <span draggable
                      className="cursor-grab text-muted hover:text-text shrink-0"
                      onDragStart={e => { e.dataTransfer.setData('application/mc-column', col.id); e.dataTransfer.effectAllowed = 'move' }}
                      title={i18nT('pages.chatSidebar.drag_to_reorder')}>
                      <GripVertical size={12} />
                    </span>
                    <div className="flex flex-wrap gap-1 items-center flex-1 min-w-0">
                      {laneDef ? (
                        // A lane's identity is its runtime state, so it shows a
                        // fixed name and accent rather than tag chips — there is
                        // no filter behind it for the user to edit.
                        <span className="inline-flex items-center gap-1.5 min-w-0" title={i18nT('pages.chatSidebar.lane_derived_hint')}>
                          <span className="w-2 h-2 rounded-full shrink-0" style={{ background: laneDef.color }} aria-hidden />
                          <span className="text-[11px] font-semibold uppercase tracking-wider truncate" style={{ color: laneDef.color }}>{i18nT(laneDef.labelKey)}</span>
                        </span>
                      ) : colTags.length === 0 ? (
                        <span className="text-[11px] text-muted font-semibold uppercase tracking-wider">{col.name || (col.include_untagged ? i18nT('pages.chatSidebar.untagged_2') : i18nT('pages.chatSidebar.all_sessions'))}</span>
                      ) : (
                        <>
                          {colTags.map(t => (
                            <span key={t.id} className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium border" style={{ borderColor: t.color, color: t.color, background: t.color + '1a' }}>{t.name}</span>
                          ))}
                          {col.include_untagged && <span className="inline-flex items-center gap-1 px-1.5 py-[1px] rounded-[4px] text-[10px] leading-none font-medium border border-dashed border-muted text-muted" title={i18nT('pages.chatSidebar.also_shows_untagged_sessions')}>{i18nT('pages.chatSidebar.untagged')}</span>}
                        </>
                      )}
                      {col.name && !laneDef && colTags.length > 0 && <span className="text-[11px] text-muted ml-1">· {col.name}</span>}
                      {/* A bare match-all column beside the lanes shows every
                        * session again, so the counts stop summing and cards
                        * appear twice. Seeding deliberately does not delete it
                        * (it is indistinguishable from a column the user added),
                        * so say what it is and let them decide. */}
                      {!laneDef && colTags.length === 0 && !col.name && !col.include_untagged
                        && orderedColumns.some(c => c.source === 'state') && (
                        <span data-testid={`column-duplicates-hint-${col.id}`} className="text-[10px] text-muted ml-1 truncate">
                          · {i18nT('pages.chatSidebar.lane_legacy_column_hint')}
                        </span>
                      )}
                    </div>
                    <span data-testid={`column-count-${col.id}`} className="text-[11px] text-muted shrink-0">{colSlots.length}</span>
                    <button type="button" data-testid={`column-new-folder-${col.id}`} className="text-muted hover:text-accent bg-transparent border-none cursor-pointer shrink-0 p-[2px]" title={i18nT('pages.chatSidebar.new_folder')} aria-label={i18nT('pages.chatSidebar.new_folder')} onClick={() => { setFolderModal({ mode: 'create', parentId: '' }) }}><FolderPlus size={12} /></button>
                    {!laneDef && <button type="button" data-testid={`column-edit-${col.id}`} className="text-muted hover:text-accent bg-transparent border-none cursor-pointer shrink-0 p-[2px]" title={i18nT('pages.chatSidebar.filter_manage_tags')} aria-label={i18nT('pages.chatSidebar.filter_manage_tags')} onClick={() => setColumnEditId(columnEditId === col.id ? null : col.id)}><TagIcon size={12} /></button>}
                    <button
                      type="button"
                      data-testid={`column-add-after-${col.id}`}
                      className="text-muted hover:text-accent bg-transparent border-none cursor-pointer shrink-0 p-[2px] disabled:cursor-wait disabled:opacity-50"
                      title={i18nT('pages.chatSidebar.add_column_after_this_one')}
                      aria-label={i18nT('pages.chatSidebar.add_column_after_this_one')}
                      disabled={addColumnAfterMutation.isPending}
                      onClick={() => addColumnAfterMutation.mutate(col.id)}
                    ><Plus size={12} /></button>
                    <button
                      type="button"
                      data-testid={`column-delete-${col.id}`}
                      className="text-muted hover:text-danger bg-transparent border-none cursor-pointer shrink-0 p-[2px]"
                      title={i18nT('pages.chatSidebar.delete_column')}
                      aria-label={i18nT('pages.chatSidebar.delete_column')}
                      onClick={() => { if (confirm(i18nT('pages.chatSidebar.delete_this_column'))) deleteColumnMutation.mutate(col.id) }}
                    ><X size={12} /></button>
                    {/* Overlays the top of the card list, so the cards under the
                      * pointer do not move while it shows. */}
                    {nativeSessionDrag !== null && derivedHintCol === col.id && (
                      <span role="status" aria-live="polite" data-testid={`column-derived-hint-${col.id}`}
                        className="absolute top-full left-0 right-0 z-10 pointer-events-none px-2 py-1 text-[11px] text-muted border-b border-border bg-bg-elevated">
                        {i18nT('pages.chatSidebar.lane_derived_hint')}
                      </span>
                    )}
                  </div>
                  {/* Column filter popover — portaled to <body> so the column's
                      overflow-hidden ancestor cannot clip it; viewport-anchored
                      to the edit button via popoverPos. */}
                  {columnEditId === col.id && popoverPos && createPortal(
                    /* Non-modal disclosure: role=dialog + a Tab-trap contains keyboard
                       focus, but we deliberately omit aria-modal — the popover has no
                       backdrop and is outside-click-dismissible, so claiming the rest of
                       the page is inert would mislead screen readers. */
                    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- Escape-dismiss and the Tab trap ARE a dialog's documented keyboard operation, and they have to live on the dialog root because the trap reasons about first/last focusable inside it; the onClick only stopPropagation
                    <div ref={columnPopoverRef} role="dialog" aria-label={i18nT('pages.chatSidebar.filter_tags', { name: col.name || 'column' })} tabIndex={-1} data-column-popover={col.id}
                      className="fixed z-[9100] bg-bg-elevated border border-border rounded-lg shadow-lg p-2 min-w-[240px] text-[13px] outline-hidden"
                      style={{ top: popoverPos.top, left: popoverPos.left }}
                      onClick={e => e.stopPropagation()}
                      onKeyDown={e => {
                        if (e.key === 'Escape') { e.stopPropagation(); closeColumnPopover(col.id); return }
                        if (e.key !== 'Tab') return
                        // Trap Tab within the dialog — portal content sits at the end of
                        // <body>, so without this Tab would jump into unrelated page chrome.
                        const root = columnPopoverRef.current
                        if (!root) return
                        const f = Array.from(root.querySelectorAll<HTMLElement>('a[href],button:not([disabled]),input:not([disabled]),[tabindex]:not([tabindex="-1"])'))
                        if (f.length === 0) return
                        const first = f[0], last = f[f.length - 1]
                        const wrapsBackward = e.shiftKey && document.activeElement === first
                        const wrapsForward = !e.shiftKey && document.activeElement === last
                        // A mid-popover Tab is the browser's to move, and not the trap's
                        // to claim. A boundary Tab the IME owns must not cycle focus —
                        // the user is choosing a candidate, not leaving the field —
                        // so `claimKey` (native-event contract in useImeGuard.ts) runs
                        // before the preventDefault() and focus move.
                        if (!wrapsBackward && !wrapsForward) return
                        // `claimSyntheticKey` owns BOTH halves of a decline:
                        // the native event (which document/window listeners
                        // see) and React's own propagation flag (which it
                        // walks when dispatching to component ancestors), so a
                        // declined Tab cannot trigger an ancestor's keyboard
                        // handling.
                        if (!columnPopoverImeLatch.claimSyntheticKey(e)) return
                        e.preventDefault()
                        ;(wrapsBackward ? last : first).focus()
                      }}>
                      <div className="flex items-center justify-between mb-1">
                        <span className="text-[11px] font-semibold text-muted uppercase tracking-wider">{i18nT('pages.chatSidebar.column_filter')}</span>
                        <button className="text-muted hover:text-text bg-transparent border-none cursor-pointer p-0" onClick={() => closeColumnPopover(col.id)} aria-label={i18nT('pages.chatSidebar.close')}><X size={13} /></button>
                      </div>
                      <Input className="w-full py-1 text-[12px] mb-2" placeholder={i18nT('pages.chatSidebar.column_name_optional')} defaultValue={col.name} onBlur={e => { const v = e.target.value.trim(); if (v !== col.name) updateColumnMutation.mutate({ id: col.id, body: { name: v } }) }} />
                      <div className="flex items-center gap-1 mb-2" role="radiogroup" aria-label={i18nT('pages.chatSidebar.match_mode')}>
                        {(['any', 'all', 'none'] as const).map(m => (
                          <button key={m} role="radio" aria-checked={col.mode === m} className={`text-[11px] px-2 py-0.5 rounded cursor-pointer border transition-all ${col.mode === m ? 'border-accent text-accent bg-accent-subtle' : 'border-border text-muted hover:text-text'}`} onClick={() => updateColumnMutation.mutate({ id: col.id, body: { mode: m } })}>{m}</button>
                        ))}
                      </div>
                      <label htmlFor={`column-include-untagged-${col.id}`} className="flex items-center gap-2 px-1 py-1 mb-2 text-[11px] text-muted cursor-pointer select-none hover:text-text" title={i18nT('pages.chatSidebar.also_show_sessions_that_have_no_tags_at_all')}>
                        <input
                          type="checkbox"
                          id={`column-include-untagged-${col.id}`}
                          data-testid={`column-include-untagged-${col.id}`}
                          aria-label={i18nT('pages.chatSidebar.include_untagged_sessions')}
                          checked={!!col.include_untagged}
                          onChange={e => updateColumnMutation.mutate({ id: col.id, body: { include_untagged: e.target.checked } })}
                          className="cursor-pointer"
                        />
                        {i18nT('pages.chatSidebar.include_untagged_sessions')}
                      </label>
                      <TagManagerList
                        mode="column-filter"
                        selectedIds={col.tag_ids}
                        onToggleTag={(_tagId, nextIds) => updateColumnMutation.mutate({ id: col.id, body: { tag_ids: nextIds } })}
                        createTestId={`tag-create-${col.id}`}
                      />
                      <div className="mt-2 flex justify-end">
                        <button className="text-[11px] text-muted hover:text-text bg-transparent border-none cursor-pointer" onClick={() => { updateColumnMutation.mutate({ id: col.id, body: { tag_ids: [] } }) }}>{i18nT('pages.chatSidebar.clear_filter')}</button>
                      </div>
                    </div>,
                    document.body
                  )}
                  <SessionRowWindowScroller className="flex-1 overflow-y-auto scrollbar-none p-1.5 flex flex-col" style={{ scrollbarWidth: 'none' }}>
                    {/* No onDrop here: folder assignment only changes via folder-header drop
                        (into a folder) and the unfile strip below this scroller (out of one).
                        Cross-column drops are handled by the OUTER column onDrop
                        (which only mutates status tags, keeping folder_id intact). */}
                    {(() => {
                      const colSlotKeys = new Set(colSlots.map(sessionRowIdentity))
                      // Show ALL root folders as drop targets, not only those with matching slots.
                      // Empty folders render with "0" count so users see the structure they built.
                      // Root folders in explicit `order`-field order (the sorted
                      // rootFolders memo, same source as list view). Rendering the
                      // raw cache array here made drops appear to revert: a reorder
                      // only rewrites `order` values (array positions are
                      // unchanged), so an unsorted render ignored the new order.
                      //
                      // Flat view inside the board: the same view-only toggle as
                      // the list — folders stop rendering and every matching
                      // session sits directly in the lane, in filteredSlots
                      // order. Cross-lane card drag (the column onDrop above) is
                      // untouched; only folder rendering (and with it folder
                      // reorder/drop, which need folder headers) goes away.
                      // A folder the person unchecked in the filter menu drops out here
                      // for the same reason the tree drops it: the hide is a statement
                      // about the folder, not about one lane, so every lane that renders
                      // folder blocks answers to it. `isFolderHidden` is deliberately NOT
                      // applied -- a board column renders an empty folder header on
                      // purpose, as something to drop onto.
                      const relevantFolders = flatView ? [] : rootFolders.filter(f => !isFolderFilteredOut(f))
                      const { rows: ungrouped, navScope: colLaneScope, container: colHoldContainer } = heldLane(flatView
                        ? colSlots
                        : colSlots.filter(s => {
                            const folderId = sidebarRows.folderOf(s)
                            return !folderId || !folders.find(f => f.id === folderId)
                          }), col.id, `board:${col.id}:ungrouped`)
                      // In flat view folders never render, so an empty lane is
                      // empty — folder structure alone must not suppress the
                      // "no sessions" notice.
                      const hasAny = colSlots.length > 0 || (!flatView && folders.length > 0)
                      return (
                        <>
                          {/* Folder reorder in board view: one DndContext per
                           *  column (folder ids stay unique within it) + the
                           *  header as drag handle. Reorders flow through the
                           *  same global reorderFolders() as list view, so order
                           *  is consistent across columns. Native session-card
                           *  drop (HTML5 DnD) is untouched — it uses drag events,
                           *  not the pointer sensor. Skipped entirely in flat
                           *  view: no folder headers means nothing to drag, and
                           *  an empty context would still mount sensors and a
                           *  body portal per column for nothing. */}
                          {!flatView && (
                          <DndContext sensors={dndSensors} collisionDetection={sidebarCollision} measuring={{ droppable: { strategy: MeasuringStrategy.Always } }} onDragStart={handleSidebarDragStart} onDragEnd={handleSidebarDragEnd} onDragCancel={handleSidebarDragCancel}>
                            <DndActiveProbe report={reportDndActive} />
                            <SortableContext items={relevantFolders.map(f => f.id)} strategy={verticalListSortingStrategy}>
                              {relevantFolders.map(f => <SortableColumnFolder key={f.id} folder={f} columnId={col.id} colSlotKeys={colSlotKeys} subtree={[...(folderSubtrees.get(f.id) ?? collectFolderSubtreeIds(folders, f.id))]} reorderable={folderReorderable} dragWithheld={folderDragWithheld} renderColumnFolder={renderColumnFolder} />)}
                            </SortableContext>
                            {/* Compact ghost follows the pointer while a folder drags —
                             *  same visual as the list-view overlay. DragOverlay renders
                             *  null unless THIS column's DndContext has an active drag,
                             *  so per-column overlays never stack. Portaled to
                             *  document.body: the sidebar rides inside OverlayDrawer's
                             *  morph clip-path, and a clip-path clips fixed-position
                             *  descendants too, so an in-place overlay is erased the
                             *  moment the ghost strays past the drawer edge. React
                             *  portals preserve context, so the overlay still reads
                             *  THIS column's active drag. */}
                            {createPortal(
                              <DragOverlay dropAnimation={null}>
                                {activeDrag?.type === 'folder' ? <FolderDragGhost folder={folders.find(x => x.id === activeDrag.id)} /> : null}
                              </DragOverlay>,
                              document.body,
                            )}
                          </DndContext>
                          )}
                          {ungrouped.map((s, i) => {
                            const isActive = isActiveRow(s)
                            const nextIsActive = isActiveRow(ungrouped[i + 1])
                            const showDivider = i < ungrouped.length - 1 && !isActive && !nextIsActive
                              && !startsAutomaticSection(ungrouped, i + 1)
                            return (
                              <Fragment key={sessionRowIdentity(s)}>
                                {startsAutomaticSection(ungrouped, i) && <PinnedSessionDivider />}
                                {renderSessionRow(s, 0, showDivider, colLaneScope, colLaneScope, colHoldContainer)}
                              </Fragment>
                            )
                          })}
                          {!hasAny && <div className="text-muted text-[12px] text-center py-4">{i18nT('pages.chatSidebar.no_sessions')}</div>}
                        </>
                      )
                    })()}
                  </SessionRowWindowScroller>
                  {/* The way back out of a folder, at the foot of the column and
                    * outside the scroller so it stays in reach however far the
                    * lane is scrolled. Only while a FILED card is in flight: an
                    * unfiled card has nothing to leave, and a column reorder is
                    * not a card. The strip's handlers stop propagation, so the
                    * column onDrop above never sees this release -- the retag
                    * and the unfile stay two gestures. */}
                  {boardDraggingFiledSession && <BoardUnfileDropStrip columnId={col.id} onUnfile={k => moveByDrag(k, null)} />}
                </div>
              )
            })}
          </div>
          {/* Edge cues: lanes continue past a clipped edge of the strip. Each is
            *  a 24 px band, the session tab strip's width, so the lane controls it
            *  overlaps stay legible and clickable. It fades from the foreground
            *  colour at low alpha, not from the page background as the tab
            *  strip's does: the lanes are cards, and in light theme the card and
            *  the page background are both near white, so a fade from bg over a
            *  card shows nothing. The foreground contrasts with the card in every
            *  theme. It is inset to the strip's padding, the cards' own vertical
            *  extent, so it shades the clipped lane and not the page gutter above
            *  and below it. It is fade-only, as the other scroll strips' cues are: the
            *  strip scrolls natively, and an arrow here would look like the
            *  follow-up bar's scroll buttons while a click on it fell through to the
            *  row underneath. */}
          {stripEdges.left && (
            <div aria-hidden="true" data-testid="column-strip-cue-left" className="pointer-events-none absolute left-0 top-2 bottom-2 w-6 z-10 bg-gradient-to-r from-text-strong/15 to-transparent" />
          )}
          {stripEdges.right && (
            <div aria-hidden="true" data-testid="column-strip-cue-right" className="pointer-events-none absolute right-0 top-2 bottom-2 w-6 z-10 bg-gradient-to-l from-text-strong/15 to-transparent" />
          )}
          </div>
          </div>
        )}
      </LayoutGroup>
      </ListDock>

      {/* Drag-move confirmation + undo. Deliberately a SIBLING of the lanes and
          a sibling ABOVE the separator, so it never covers the row that just
          moved and never covers the persistent "Older Sessions" control — the
          footer shifts down by its height while it is up. Session moves and
          folder re-parents share the slot: arming either dismisses the other,
          so at most one offer (and one ⌘Z listener) exists at a time. */}
      <AnimatePresence initial={false}>
        {dragMove?.live && (
          <MoveUndoBar key={dragMove.id} moved={dragMove}
            onUndo={() => undoDragMove(dragMove.id)}
            onHoldChange={undoBar.onHoldChange}
            remainingMs={undoBar.remainingMs}
            paused={undoBar.paused}
            /* Same width ladder as the header's compact/tiny steps: below this the
               prefix + shortcut would eat the row and truncate the destination. */
            compact={paintedSidebarWidth < 220} />
        )}
        {folderMove?.live && (
          <MoveUndoBar key={folderMove.id} moved={folderMove}
            onUndo={() => undoFolderMove(folderMove.id)}
            onHoldChange={folderUndoBar.onHoldChange}
            remainingMs={folderUndoBar.remainingMs}
            paused={folderUndoBar.paused}
            compact={paintedSidebarWidth < 220} />
        )}
      </AnimatePresence>

      {/* When expanded: doubles as the resize handle (accent on hover, drag to resize, dbl-click to collapse).
          When collapsed: just a static 1px divider between sessions and the Older Sessions footer. */}
      {historyOpen ? (
        // Separator that doubles as a Pointer-Events resize handle (drag,
        // mouse/touch/pen) / collapse (double-click); neither gesture is driven
        // from the keyboard on this element.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- the handler the rule sees is onDoubleClick, and the collapse it performs is duplicated on the "Older Sessions" row below (role=button/tabIndex=0, Enter+Space), so the COLLAPSE is keyboard-reachable. The RESIZE is not: usePointerDrag exposes pointer handlers only and this pane has no arrow-key resize anywhere. Giving it one is the ARIA window-splitter keyboard contract — a feature, not a lint fix
        <div
          role="separator"
          aria-orientation="horizontal"
          aria-label={i18nT('pages.chatSidebar.resize_history_pane')}
          {...historyResize}
          onDoubleClick={() => setHistoryOpen(false)}
          className="relative h-[6px] cursor-ns-resize z-10 group/drag flex items-center justify-center select-none"
          style={{ touchAction: 'none' }}
        >
          <div className={`w-full transition-all duration-200 ${historyDragging ? 'h-[2px] bg-accent-hover' : 'h-px bg-border group-hover/drag:h-[2px] group-hover/drag:bg-accent'}`} />
        </div>
      ) : (
        <div className="border-t border-border" />
      )}
      {/* Older Sessions footer — the persistent collapse/expand header for the
          history pane. Whole row is the click target; the Clear button stops
          propagation. */}
      <div
        role="button"
        tabIndex={0}
        {...uiLocation('chat.older-sessions')}
        onClick={() => { if (historyOpen) setHistoryOpen(false); else openHistoryPane() }}
        onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (historyOpen) setHistoryOpen(false); else openHistoryPane() } }}
        /* pt/pb are 14px, not py-3, so this row's top border lands on the same
           baseline as the nav rail's community row ("Star us · Report issue"):
           both cards sit 8px off the shell floor, the rail spends 8+2+24+10 =
           44px below its own hairline, and 14+16+14 matches that exactly. The
           symmetric padding is what keeps the clock and label optically centred
           in the band. */
        className="flex justify-between items-center px-3 pt-[14px] pb-[14px] cursor-pointer select-none"
        aria-expanded={historyOpen}
        aria-controls="history-pane"
        aria-label={i18nT('pages.chatSidebar.older_sessions')}
      >
        <span className="flex items-center gap-1.5 text-[13px] font-semibold text-text-strong leading-none">
          <Clock size={14} className="shrink-0" />
          <span className="leading-none">{i18nT('pages.chatSidebar.older_sessions_2')}</span>
        </span>
        {/* Chevron trails the Clear button so the disclosure glyph is the
            rightmost control, and Clear shifts left by the gap rather than
            being pushed off the row's 12px right inset. The gap is 12px, wider
            than the row's other spacing: Clear is destructive (it wipes closed
            sessions behind a single confirm), so a pointer aimed at the collapse
            glyph must not land on it. This trailing position is the pane's ONE
            deliberate exception to the sidebar's leading-chevron grammar
            (#2887): a section header ends with its own disclosure glyph, while
            row-level disclosures (group headers, hidden-folders reveal, the
            folders filter row) lead with theirs like tree rows everywhere else.
            All four share the same mechanic: a ChevronRight that rotates 90°
            when open — never a Right/Down glyph swap, never a counter-rotation
            when closed. */}
        <span className="flex items-center gap-3 shrink-0">
          {historyOpen && history.length > 0 && (
            <button
              className="px-2 py-0.5 rounded-md border border-border bg-transparent text-muted text-[12px] cursor-pointer hover:text-danger hover:border-danger transition-all"
              onClick={async e => { e.stopPropagation(); if (confirm(i18nT('pages.chatSidebar.clear_closed_sessions_active_tabs_and_pinned_ses'))) { await api.clearSessions(); dispatch(fetchHistory(false)) } }}
            >{i18nT('pages.chatSidebar.clear')}</button>
          )}
          <DisclosureChevron open={historyOpen} size={16} className="text-text-strong" />
        </span>
      </div>
      <AnimatePresence initial={false}>
        {historyOpen && (
          <motion.div
            id="history-pane"
            key="history-pane"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.15, ease: [0.16, 1, 0.3, 1] }}
            className="overflow-hidden"
          >
            <div className="px-2 pb-1">
              <div className="relative">
                <SearchInput className="w-full" placeholder={i18nT('pages.chatSidebar.search_older_sessions')} value={historyFilter} onChange={e => setHistoryFilter(e.target.value)} />
                {historyFilter && (
                  <button type="button" className="absolute right-2 top-1/2 -translate-y-1/2 text-muted hover:text-text cursor-pointer bg-transparent border-none p-0 leading-none transition-colors" onClick={() => setHistoryFilter('')} aria-label={i18nT('pages.chatSidebar.clear_search')}><X size={13} /></button>
                )}
              </div>
              {/* The unresumable-surface notice used to live here. It moved to
                  ChatPage's shared notice slot above the composer (#5925): this
                  pane starts CLOSED (`historyOpen` defaults false), so a notice
                  inside it can only ever be seen by someone who had already
                  opened it -- which is nobody arriving from the command palette,
                  a notification, or ChatPage's own "Continue a previous chat"
                  list. One always-visible site serves all of them. */}
            </div>
            {/* scroll-shadow already fades the top/bottom edge as its
             *  scrollability cue, so the bar itself is redundant here. */}
            <div className="overflow-y-auto scrollbar-none p-2 scroll-shadow" style={{ height: `${historyHeight}px`, scrollbarWidth: 'none' }}>
              {(() => {
                const historyLocalMatch = (s: { title?: string; key: string }) =>
                  ((s.title || '') + s.key).toLowerCase().includes(historyFilter.toLowerCase())
                // Additive rather than a boolean OR: here the backend result IS the
                // source list, so filtering `history` instead would drop backend-only hits.
                // Remote crew sessions are NOT merged here: they are the peer's
                // LIVE slots and join the live sessions list above. Merging them into
                // history as well would render each remote row twice.
                const filteredHistory = (() => {
                  if (!historyFilter) return history
                  if (historyFilter.trim().length >= SEARCH_MIN_CHARS && historySearchResults) {
                    const seen = new Set(historySearchResults.map(s => s.key))
                    return [...historySearchResults,
                            ...history.filter(s => !seen.has(s.key) && historyLocalMatch(s))]
                  }
                  return (historySearchResults ?? history).filter(historyLocalMatch)
                })()
                // One definition of "search active" for every site below: results
                // are present AND the query is still at/above the search threshold.
                // The compound check matters on the clear-X frame: historyFilter
                // empties synchronously but useDebouncedSessionSearch nulls its
                // result in a passive effect (one render later), so a bare
                // `historySearchResults` test would treat that stale frame as an
                // active search and paint date segment headers over a
                // relevance-ordered list.
                const searchActive = historyFilter.trim().length >= SEARCH_MIN_CHARS && !!historySearchResults
                // Hide date segments when the user has an active search — results are
                // Segments only make sense when the list is date-ordered. For name/created
                // sorts (or active search, which is relevance-ranked) they'd interleave.
                const showSegments = !searchActive
                  && (sortKey === 'date-desc' || sortKey === 'date-asc')
                // Active search: keep the backend's relevance ranking (title-boosted;
                // see search_sessions in history.py). Re-sorting search results by the
                // sidebar sort key buried an exact title match under fresher sessions
                // that merely mention the query in their content — and defeated
                // groupHistoryByFolder's documented order-preserving contract. The
                // command palette's Sessions tab already preserves backend order.
                // No search: skip the sort only when the backend already returns
                // date-desc order.
                const sortedHistory = (searchActive || sortKey === 'date-desc') ? filteredHistory : [...filteredHistory].sort((a, b) => compareBySort(a, b, sortKey))
                // An empty pane is reachable whenever every session on disk is
                // already open as a tab (the common case for a light user), so it
                // needs to say so rather than render a search box over blank space.
                // A filtered-to-nothing list is a different statement and reuses the
                // wording the two sibling panes already use for it.
                if (sortedHistory.length === 0) {
                  return (
                    <div className="px-3 py-4 text-[12px] text-muted text-center">
                      {historyFilter
                        ? i18nT('pages.chatSidebar.no_sessions_match')
                        : i18nT('pages.chatSidebar.no_older_sessions')}
                    </div>
                  )
                }
                let prevSeg = ''
                // Derive agent color the same way renderSessionRow does so history rows
                // match the session-row visual language (agent name tinted by source).
                const agentColorFor = (agentName: string): string => {
                  const meta = tintAgents.find(a => a.name === agentName)
                  if (meta?.source === 'package') return 'text-[var(--aim)]'
                  if (meta?.source === 'builtin') return 'text-muted'
                  return 'text-muted'
                }
                const historyRow = (s: (typeof sortedHistory)[number]) => {
                  const displayDate = fmtRelativeTime(s.modified ?? s.created)
                  const agentName = s.agent || defaultAgent || ''
                  // Display vs resolution key, same split as renderSessionRow:
                  // `agentColorFor` must receive the bare name. An archived
                  // session whose JSONL metadata never recorded an agent falls
                  // back to the CURRENT default, which is a different fact from
                  // a session pinned to that same alias — the marker is what
                  // tells them apart (#6529).
                  const agentDisplay = agentName ? agentOrDefaultLabel(s.agent, defaultAgent) : ''
                  const agentColor = agentColorFor(agentName)
                  const isDashboard = s.key.startsWith('dashboard')
                  const channel = slotChannelNamespace(s.key)
                  const surfaceLabel = isDashboard
                    ? i18nT('pages.chatSidebar.dashboard_source')
                    : slotChannelLabel(s.key) || i18nT('pages.chatSidebar.session_source')
                  // Federated-search row from a connected remote instance: its
                  // transcript lives on the other gateway, so activation switches
                  // to that instance's pane instead of resuming a (same-keyed but
                  // unrelated) local session, and the local delete action is
                  // hidden — deleteHistorySession would target the LOCAL file.
                  const remoteInstanceId = (s as { instance_id?: string }).instance_id
                  const remoteInstanceName = (s as { instance_name?: string }).instance_name
                  const activateRow = () => {
                    // A remote row never resumes here, so it can never produce the
                    // unresumable notice above — the pane switch IS its outcome.
                    if (remoteInstanceId) { selectInstance(remoteInstanceId); return }
                    // No post-resolve check here: `resumeFromHistory` itself
                    // records an undisplayable-surface answer on the slice
                    // (#5925), which is what the notice above renders. Keeping
                    // a second copy of that predicate per call site is how the
                    // four sibling entry points ended up giving no feedback at
                    // all while this one did.
                    dispatch(resumeFromHistory({ key: s.key, title: s.title || s.key }))
                  }
                  return (
                    <div className={`group relative flex items-start gap-2.5 pr-4 py-2 rounded-md text-sm transition-all select-none ${remoteInstanceId ? '' : '[@media(hover:none)]:pr-10 '}${!connected ? 'text-muted opacity-50 cursor-not-allowed' : 'text-muted hover:text-text hover:bg-bg-hover cursor-pointer'}`} style={{ paddingLeft: '10px' }} title={s.title || s.key} {...offlineProps(connected, 'resume sessions')} role="button" tabIndex={0} aria-disabled={!connected} onKeyDown={e => {
                      // WCAG 2.1.1: history rows must be resumable via keyboard.
                      if (e.key !== 'Enter' && e.key !== ' ') return
                      if ((e.target as HTMLElement) !== e.currentTarget) return
                      e.preventDefault()
                      if (!connected) return
                      activateRow()
                    }} onMouseDown={e => {
                      // NOTE: pointer activation lives on onMouseDown (not onClick). For a
                      // div[role="button"], browsers do NOT synthesize a click from Enter
                      // (that only happens for native buttons/links — hence the onKeyDown
                      // handler above), and AT activation (e.g. VoiceOver VO+Space)
                      // synthesizes a click INSTEAD of key events. So each path activates
                      // exactly once. Do NOT add an e.detail === 0 guard here or in any
                      // future onClick: AT-synthesized clicks have detail 0 and would be
                      // silently dropped, breaking screen-reader activation.
                      e.preventDefault()
                      if ((e.target as HTMLElement).closest?.('[data-close]')) { if (!remoteInstanceId && confirm(i18nT('pages.chatSidebar.are_you_sure_you_want_to_delete_this_history_ses'))) dispatch(deleteHistorySession(s.key)); return }
                      if (!connected) return
                      activateRow()
                    }}>
                      {/* Platform glyph — fills the left column that session rows reserve for the unread dot */}
                      <span role="img" className="shrink-0 flex items-center justify-center self-center text-muted" title={surfaceLabel} aria-label={surfaceLabel}>
                        {isDashboard
                          ? <Monitor size={12} />
                          : channel === 'unified'
                            ? <MessageSquare size={12} />
                            : <ChannelBrandIcon channel={channel ?? ''} size={12} />
                        }
                      </span>
                      <div className="flex-1 min-w-0 overflow-hidden">
                        <div className={`session-agent-label text-[11px] font-semibold truncate leading-tight flex items-center gap-1 ${agentColor}`}>
                          <span className="truncate" title={agentDisplay || undefined}>{agentDisplay || '\u00A0'}</span>
                          {/* Remote-crew marker. Tinted `info` + a server glyph rather
                              than the neutral chip styling every other meta chip uses:
                              this row's transcript lives on ANOTHER MACHINE, which is a
                              different claim from "has this tag" and the one the user
                              must not misread. The glyph is the non-colour half of the
                              cue, so the distinction survives a colour-vision
                              deficiency; it is aria-hidden because the crew name beside
                              it already names the target.

                              The tooltip names the OUTCOME, and it is the opposite of a
                              live peer row's. Activating this row calls
                              `selectInstance` — the pane switch IS its outcome — whereas
                              a live peer row opens the session HERE. The two carry the
                              same pill, so without this the identical marker would mean
                              two different clicks. */}
                          {remoteInstanceName && (
                            <RemoteCrewChip
                              name={remoteInstanceName}
                              label={i18nT('pages.chatSidebar.on_instance', { name: remoteInstanceName })}
                              title={i18nT('pages.chatSidebar.opens_on_crew_switches_there', { name: remoteInstanceName })}
                            />
                          )}
                          {s.memory_mode === 'incognito' && <span className="text-muted" title={i18nT('pages.chatSidebar.incognito_no_memory_writes')}><EyeOff size={10} /></span>}
                          {s.memory_mode === 'temporary' && <span className="text-aim" title={i18nT('pages.chatSidebar.temporary_no_memory_reads_or_writes')}><VenetianMask size={10} /></span>}
                          {displayDate && <span className="ml-auto text-[11px] text-muted font-normal shrink-0">{displayDate}</span>}
                        </div>
                        <div className="text-[13px] leading-snug line-clamp-2 break-words">{s.title || s.key}</div>
                      </div>
                      {/* Floating hover button group — matches session-row pattern.
                          Hidden for remote rows: deleteHistorySession targets the
                          LOCAL session file, which for a remote row is at best a
                          same-keyed unrelated conversation. */}
                      {!remoteInstanceId && <div className="absolute top-1/2 -translate-y-1/2 right-1.5 opacity-0 group-hover:opacity-100 [@media(hover:none)]:opacity-100 group-focus-visible:opacity-100 group-has-[:focus-visible]:opacity-100 transition-all flex items-center gap-0.5 rounded-md p-1 bg-card border border-border shadow-sm">
                        <button type="button" title={i18nT('pages.chatSidebar.delete_history_session')} aria-label={i18nT('pages.chatSidebar.delete_history_session')} className="text-[12px] text-muted cursor-pointer p-[4px] rounded hover:text-danger hover:bg-danger-subtle transition-all bg-transparent border-none" onMouseDown={e => e.stopPropagation()} onClick={e => { e.stopPropagation(); if (confirm(i18nT('pages.chatSidebar.are_you_sure_you_want_to_delete_this_history_ses'))) dispatch(deleteHistorySession(s.key)) }}><X size={12} /></button>
                      </div>}
                    </div>
                  )
                }
                // Folder-grouped view: during an active content search, regroup the
                // relevance-ranked results under collapsible folder headers (+ Unfiled)
                // by the folder each session was filed in, instead of date segments.
                if (searchActive) {
                  return groupHistoryByFolder(sortedHistory, folders, folderSortMode).map(({ key: gid, folder, rows }) => {
                    const collapsed = collapsedHistoryGroups.has(gid)
                    const groupName = folder ? folder.name : i18nT('pages.chatSidebar.unfiled')
                    return (
                      <Fragment key={gid}>
                        <button type="button" aria-expanded={!collapsed} aria-label={collapsed ? i18nT('pages.chatSidebar.expand_group_results', { group: groupName }) : i18nT('pages.chatSidebar.collapse_group_results', { group: groupName })} className="w-full flex items-center gap-1.5 px-2 pt-3 pb-1 text-[11px] font-semibold text-muted select-none bg-transparent border-none cursor-pointer hover:text-text first:pt-1" onClick={() => setCollapsedHistoryGroups(prev => { const next = new Set(prev); if (next.has(gid)) next.delete(gid); else next.add(gid); return next })}>
                          <DisclosureChevron open={!collapsed} size={12} />
                          {folder ? <FolderGlyph color={folder.color} icon={folder.icon} size={12} open={!collapsed} /> : <Folder size={12} className="text-muted shrink-0" />}
                          <span className="truncate">{folder ? folder.name : i18nT('pages.chatSidebar.unfiled')}</span>
                          <span className="ml-0.5 text-muted font-normal tabular-nums">· {rows.length}</span>
                        </button>
                        {!collapsed && rows.map((s, i) => (
                          <Fragment key={historyRowIdentity(s)}>
                            {historyRow(s)}
                            {i < rows.length - 1 && <div className="mx-3 border-b border-border" />}
                          </Fragment>
                        ))}
                      </Fragment>
                    )
                  })
                }
                return sortedHistory.map((s, idx) => {
                  const tsForSegment = s.modified ?? s.created
                  const seg = dateSegment(tsForSegment)
                  const showHeader = showSegments && seg !== prevSeg
                  prevSeg = seg
                  // Divider between consecutive rows — but not before a segment header
                  // (the header itself separates), and not after the last row.
                  const isLast = idx === sortedHistory.length - 1
                  const nextSeg = !isLast ? dateSegment(sortedHistory[idx + 1].modified ?? sortedHistory[idx + 1].created) : seg
                  const showDivider = !isLast && (!showSegments || nextSeg === seg)
                  return (
                    <Fragment key={historyRowIdentity(s)}>
                      {showHeader && (
                        <div className="px-2 pt-3 pb-1 text-[11px] font-semibold text-muted uppercase tracking-[.06em] select-none first:pt-1">{seg}</div>
                      )}
                      {historyRow(s)}
                      {showDivider && <div className="mx-3 border-b border-border" />}
                    </Fragment>
                  )
                })
              })()}
              {/* Load-more uses onMouseDown+preventDefault to trigger without stealing
                  focus from the transcript; scope-disable the static-interaction rule. */}
              {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
              {historyHasMore && <div className="flex justify-center py-2 text-accent text-[13px] font-medium cursor-pointer hover:bg-accent-subtle rounded-md" onMouseDown={e => { e.preventDefault(); dispatch(fetchHistory(true)) }}>{i18nT('pages.chatSidebar.load_more')}</div>}
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* One folder create/settings modal for the whole sidebar. Rendered here
       *  rather than per-row so a folder shown in several board columns can only
       *  ever open one, and so the ProjectPicker it hosts has a single owner. */}
      {folderModal && (
        <FolderConfigModal
          open={true}
          mode={folderModal.mode}
          parentId={folderModal.mode === 'create' ? folderModal.parentId : undefined}
          folder={folderModal.mode === 'edit' ? folders.find(f => f.id === folderModal.folderId) : undefined}
          folders={folders}
          installedAgents={installedAgents}
          globalDefaultAgent={defaultAgent}
          availableTags={tagsData}
          availableTagsFailed={tagsQueryFailed}
          onRetryTags={() => { void refetchTags() }}
          onClose={() => setFolderModal(null)}
          onSubmit={async draft => {
            // AWAIT the mutation and only close on success. The backend rejects a
            // free-typed project_dir (not absolute / not an existing directory /
            // sensitive) and a multi-emoji icon with a 400; closing optimistically
            // discarded the whole draft with no feedback. Rethrowing lets the modal
            // stay open and render the reason.
            if (folderModal.mode === 'create') {
              await createFolderMutation.mutateAsync({
                name: draft.name,
                parentId: folderModal.parentId || undefined,
                projectDir: draft.projectDir,
                defaultAgent: draft.defaultAgent,
                color: draft.color,
                icon: draft.icon,
                tags: draft.tags,
                steeringDirs: draft.steeringDirs,
              })
              // Creating a folder while flat view is on would otherwise appear
              // to do nothing (flat rendering skips folder blocks in both the
              // list and the board columns). Exit flat view so the new folder
              // is visible, whichever entry point created it.
              if (flatView) {
                setFlatView(false)
                safeSetItem(FLAT_VIEW_LS_KEY, '0')
              }
            } else {
              // Build the PATCH from what the USER edited (draft.touched, measured
              // against what the modal opened with) — NOT from a diff against live
              // cache, whose shape would revert any field another client changed
              // mid-edit.
              const touched = new Set(draft.touched)
              const body: Record<string, unknown> = {}
              if (touched.has('name')) body.name = draft.name
              if (touched.has('projectDir')) body.project_dir = draft.projectDir
              if (touched.has('defaultAgent')) body.default_agent = draft.defaultAgent
              // '' is a legitimate color instruction: it clears back to gray.
              if (touched.has('color')) body.color = draft.color
              // regenerate_icon and a manual icon are mutually exclusive on the
              // backend; the modal keeps them exclusive in the draft, and this
              // branch keeps them exclusive on the wire.
              if (draft.regenerateIcon) {
                body.regenerate_icon = true
              } else if (touched.has('icon')) {
                // '' clears back to the default glyph.
                body.icon = draft.icon
              }
              // An empty array is a legitimate instruction too: it clears the
              // folder's tags.
              if (touched.has('tags')) body.tags = draft.tags
              // '' / [] clears here as well: PATCH steering_dirs:[] removes the
              // folder's extra steering directories (server resolves effective
              // dirs from folder_id, so nothing resolved is sent).
              if (touched.has('steeringDirs')) body.steering_dirs = draft.steeringDirs
              if (Object.keys(body).length > 0) {
                await updateFolderMutation.mutateAsync({ id: folderModal.folderId, body })
              }
            }
            setFolderModal(null)
          }}
        />
      )}
    </div>
  )
}

export default memo(ChatSidebar)

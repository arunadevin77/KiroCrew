/**
 * The MCP management page's data model: what the gateway status and launch-preview
 * routes carry, the error-code-to-copy tables, the ONE derivation of a row's state and
 * the evidence verdict that flags a server shared against its assessment, plus the
 * wait the Share all action runs on the measurement pass. Tables and pure functions,
 * except `waitForMeasurePass`, which polls the pass and writes each reading into the
 * query cache; the page and its views import from here.
 */
import { type QueryClient } from '@tanstack/react-query'
import {
  api,
  ApiError,
  type McpManagedServer,
} from '../../api/client'

export const DOCS_URL =
  'https://github.com/kirodotdev/KiroCrew/blob/main/docs/architecture/design-notes/mcp-stub-decoupling.md'

export type GatewayStatus = {
  enabled: boolean
  stub: string[]
  stub_count: number
  running: boolean
  ping_ok: boolean
  supported: boolean
  // Stubbed servers whose launch the gateway has not recorded yet, keyed by
  // name. Optional: an older gateway does not send it.
  launch_refused?: Record<string, LaunchRefusal>
}

/** A shared server whose current launch is not recorded yet; it waits for a confirm. */
export type LaunchRefusal = {
  reason: 'added_outside_dashboard' | 'changed_needs_reapproval'
  /** The launch approved before it changed, as the approval store displays it. */
  approved_commands?: string[][]
  approved_envs?: string[][]
}

/** What a server runs, read only when its Details are opened. */
export type LaunchPreview = {
  name: string
  commands: string[][]
  envs: string[][]
  complete: boolean
  expected_launch?: string
}

/** Catalog key per refusal class the gateway sends in `share_blocked`. */
export const SHARE_BLOCKED_KEY: Record<string, string> = {
  credential_vending: 'pages.mcpManagement.share_blocked_credentials',
  needs_tenant_nonce: 'pages.mcpManagement.share_blocked_own_copy',
  computer_use: 'pages.mcpManagement.share_blocked_computer',
}

function errorCode(error: unknown): unknown {
  if (!(error instanceof ApiError)) return undefined
  try {
    return JSON.parse(error.body)?.code
  } catch {
    return undefined
  }
}

export function stubErrorKey(error: unknown): string {
  const code = errorCode(error)
  if (code === 'launch_unresolved') return 'pages.mcpManagement.stub_error_launch_unresolved'
  if (code === 'launch_over_cap') return 'pages.mcpManagement.stub_error_launch_over_cap'
  if (code === 'approval_write_failed') return 'pages.mcpManagement.stub_error_approval_write_failed'
  if (code === 'share_refused') return 'pages.mcpManagement.stub_error_share_refused'
  return 'pages.mcpManagement.stub_failed'
}

/** The two sub-views: the switches, and the evidence behind them. */
export type McpView = 'servers' | 'assessment'

/**
 * Catalog KEYS, not prose: the strict i18n lint reads inside ALL-CAPS module
 * constants. An unrecognised tier falls back to "not measured".
 */
export const STRENGTH_LABEL_KEY: Record<string, string> = {
  refuted: 'pages.mcpManagement.assessment.strength_refuted',
  disqualified: 'pages.mcpManagement.assessment.strength_disqualified',
  declared: 'pages.mcpManagement.assessment.strength_declared',
  measured: 'pages.mcpManagement.assessment.strength_measured',
  no_objection: 'pages.mcpManagement.assessment.strength_no_objection',
  unknown: 'pages.mcpManagement.assessment.strength_unknown',
}

/** One key per reason code the verdict engine emits. */
export const REASON_LABEL_KEY: Record<string, string> = {
  observed_hazard: 'pages.mcpManagement.assessment.reason_observed_hazard',
  not_stdio: 'pages.mcpManagement.assessment.reason_not_stdio',
  session_bound_by_construction:
    'pages.mcpManagement.assessment.reason_session_bound_by_construction',
  rotating_secret_env: 'pages.mcpManagement.assessment.reason_rotating_secret_env',
  not_probed: 'pages.mcpManagement.assessment.reason_not_probed',
  degrades_when_shared: 'pages.mcpManagement.assessment.reason_degrades_when_shared',
  handshake_not_reproducible:
    'pages.mcpManagement.assessment.reason_handshake_not_reproducible',
  declares_caller_identity: 'pages.mcpManagement.assessment.reason_declares_caller_identity',
  all_tools_read_only: 'pages.mcpManagement.assessment.reason_all_tools_read_only',
  preflight_passed: 'pages.mcpManagement.assessment.reason_preflight_passed',
  preflight_not_run: 'pages.mcpManagement.assessment.reason_preflight_not_run',
  no_objection_found: 'pages.mcpManagement.assessment.reason_no_objection_found',
  no_tool_annotations: 'pages.mcpManagement.assessment.reason_no_tool_annotations',
  no_tools_listed: 'pages.mcpManagement.assessment.reason_no_tools_listed',
}

/**
 * The states a row can be in, as a closed set -- deliberately not the i18n keys,
 * so "is this row shared" is a type check rather than a string compare against a
 * key that lives in 13 catalogs.
 */
type McpRowState = 'no_stub' | 'needs_reapproval' | 'direct_env' | 'shared' | 'stub' | 'direct'

const STATE_LABEL_KEY: Record<McpRowState, string> = {
  no_stub: 'pages.mcpManagement.state_no_stub',
  needs_reapproval: 'pages.mcpManagement.state_needs_reapproval',
  direct_env: 'pages.mcpManagement.state_direct_env',
  shared: 'pages.mcpManagement.state_shared',
  stub: 'pages.mcpManagement.state_stub',
  direct: 'pages.mcpManagement.state_direct',
}

/**
 * What the server is running as, as ONE function used by both sub-views. The
 * chip, its colour and the assessment's warning all read this answer.
 */
export function rowState(
  s: McpManagedServer,
  sharingOn: boolean,
  refused?: Record<string, LaunchRefusal>,
): McpRowState {
  if (!s.can_stub) return 'no_stub'
  if (s.stub && refused?.[s.name]) return 'needs_reapproval'
  if (s.stub && s.pooling_blocked_by_env === true) return 'direct_env'
  if (s.stub && sharingOn) return 'shared'
  if (s.stub) return 'stub'
  return 'direct'
}

export function stateLabelKey(state: McpRowState): string {
  return STATE_LABEL_KEY[state]
}

/** Evidence tiers that argue AGAINST sharing, as opposed to merely not endorsing it. */
export const CONTRARY_STRENGTHS = new Set(['refuted', 'disqualified'])

/** True when the server is shared right now and the evidence argues against it. */
export function sharedWithoutSupport(
  s: McpManagedServer,
  sharingOn: boolean,
  refused?: Record<string, LaunchRefusal>,
): boolean {
  if (rowState(s, sharingOn, refused) !== 'shared') return false
  const rec = s.recommendation
  if (!rec) return false
  return CONTRARY_STRENGTHS.has(rec.strength)
}

/** How long the batch action waits for an uncapped measurement pass, in ms. */
const MEASURE_WAIT_MS = 4 * 60 * 1000
const MEASURE_POLL_MS = 1500

/**
 * Poll the measurement pass until it stops. True when it finished in time. Each
 * reading lands in the SAME cache entry `MeasureControl` reads, so the page has
 * one source of displayed progress.
 */
export async function waitForMeasurePass(qc: QueryClient): Promise<boolean> {
  const deadline = Date.now() + MEASURE_WAIT_MS
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, MEASURE_POLL_MS))
    try {
      const p = await api.mcpMeasureProgress()
      qc.setQueryData(['mcp-measure-progress'], p)
      if (!p.running) return true
    } catch {
      // A failed read is not a finished pass; the deadline ends the loop.
    }
  }
  return false
}

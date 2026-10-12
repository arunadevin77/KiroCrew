/**
 * Evidence for the spec-hooks notice row in Sessions chat.
 *
 * Two rows the PR adds:
 * 1. run-nowhere: a session on claude or codex (backends with no hook channel)
 *    whose agent spec carries hooks — the row names the events that won't run.
 * 2. withheld: a goose or opencode session whose CHECKOUT spec sets hooks that
 *    Crew withholds because the project directory is not trusted.
 *
 * Both rows:
 * - appear in Sessions chat (the standard ChatPage transcript view)
 * - are HIDDEN in crewmate chat (meta.kind = "spec_hooks" added to CREWMATE_HIDDEN_NOTICE_KINDS)
 *
 * Scenes render the real NoticeCard in both dark and light themes.
 *
 *   ?scene=run-nowhere  — the no-hook-channel notice (claude/codex default)
 *   ?scene=withheld     — the withheld-project-hooks notice (goose/opencode default)
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n/all'
import NoticeCard from '../src/pages/chat/NoticeCard'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') === 'withheld' ? 'withheld' : 'run-nowhere'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n()

const RUN_NOWHERE =
  'ℹ Agent my-agent has hooks on userPromptSubmit, stop. This backend does not run ' +
  'an agent\'s hooks, so they have no effect in this session.'

const WITHHELD =
  'ℹ This project\'s own spec for agent my-agent sets hooks. Crew does not run commands ' +
  'from a project checkout, so those hooks do not run in this session. Hooks from your ' +
  'user-level spec of that name still run.'

const content = scene === 'withheld' ? WITHHELD : RUN_NOWHERE
const title = scene === 'withheld'
  ? 'Withheld project-spec hooks notice (goose / opencode, trusted project path absent)'
  : 'Spec hooks run-nowhere notice (claude / codex, no hook channel)'

const root = document.getElementById('root')!
createRoot(root).render(
  <div className="p-4 max-w-2xl space-y-4">
    <p className="text-xs text-muted font-mono">{title}</p>
    <NoticeCard content={content} />
    <p className="text-xs text-muted">
      {scene === 'withheld'
        ? 'This row shows in Sessions chat; it is hidden in crewmate chat (CREWMATE_HIDDEN_NOTICE_KINDS).'
        : 'This row shows in Sessions chat; it is hidden in crewmate chat (CREWMATE_HIDDEN_NOTICE_KINDS).'}
    </p>
  </div>,
)

import { ExternalLink } from 'lucide-react'
import { ACP_BACKEND_KAS, ACP_BACKEND_KIRO } from '../../api/acpBackend'
import { i18nT } from '../../i18n/t'

export const KIRO_MODEL_SETTINGS_URL = 'https://app.kiro.dev/settings/model'

/** Whether the selected ACP backend runs its own model fallback before KiroCrew's. */
export function harnessAppliesOwnFallback(acpBackend: string | undefined): boolean {
  const backend = acpBackend ?? ACP_BACKEND_KIRO
  return backend === ACP_BACKEND_KIRO || backend === ACP_BACKEND_KAS
}

/**
 * Note above the fallback settings: on the Kiro CLI and kiro-agent harnesses the
 * per-model fallbacks managed on app.kiro.dev run inside the harness first, and the
 * settings on this page apply on top. Renders nothing on other harnesses, where
 * KiroCrew's own fallback is the only one.
 */
export function KiroHarnessFallbackNote({ acpBackend }: { acpBackend: string | undefined }) {
  if (!harnessAppliesOwnFallback(acpBackend)) return null
  return (
    <div className="mb-4 rounded-md border border-border bg-bg-elevated px-3 py-2 flex flex-col gap-1" data-testid="kiro-harness-fallback-note">
      <span className="text-[12px] font-medium text-text">{i18nT('pages.settings.chatPanel.kiro_harness_fallback_title')}</span>
      <span className="text-[12px] text-muted leading-relaxed">{i18nT('pages.settings.chatPanel.kiro_harness_fallback_body')}</span>
      <a
        href={KIRO_MODEL_SETTINGS_URL}
        target="_blank" rel="noopener noreferrer"
        className="text-[12px] text-accent hover:underline w-fit inline-flex items-center gap-1"
      >
        {i18nT('pages.settings.chatPanel.kiro_harness_fallback_link')}
        <ExternalLink size={12} aria-hidden />
      </a>
    </div>
  )
}

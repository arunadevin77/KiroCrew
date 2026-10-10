import { useTranslation } from 'react-i18next'
import { Badge } from '../../components/ui'

/**
 * The ONE label a staged crewmate dashboard is drawn under, wherever it is drawn:
 * the chat's side panel (`DashboardPreviewPanel`) and the crewmate's Dashboard tab
 * on `/members`. It says the page is a preview and that nothing is applied until
 * the person answers in chat. A status region, so a screen reader hears it on
 * arrival. The host hides it once the staged page is gone (`onPreviewGone`),
 * because "not applied" would then be false.
 */
export default function DashboardPreviewLabel() {
  const { t } = useTranslation()
  return (
    <div role="status" className="flex flex-wrap items-center gap-x-2 gap-y-1 px-3 py-2 border-b border-border" data-testid="dashboard-preview-label">
      <Badge variant="warn" data-testid="dashboard-preview-badge">{t('pages.chat.dashboardPreviewPanel.badge')}</Badge>
      <span className="text-xs text-muted min-w-0 basis-full">{t('pages.chat.dashboardPreviewPanel.hint')}</span>
    </div>
  )
}

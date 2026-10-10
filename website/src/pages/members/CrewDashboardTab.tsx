import { lazy, Suspense, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import ErrorBoundary from '../../components/ErrorBoundary'
import DashboardPreviewLabel from '../chat/DashboardPreviewLabel'

const CrewDynamicDashboard = lazy(() => import('./CrewDynamicDashboard'))

/** The crewmate's Dashboard tab: the crewmate's DYNAMIC DASHBOARD and nothing else.
 *
 * The page is the crewmate's own copy of a template, whose every number comes
 * either from a crew-log fold the gateway read or from an agentic value the
 * crewmate wrote and the host type-checked against the template's manifest. The
 * page runs its own JS in the crew webview sandbox, so it can draw a chart; it
 * cannot invent a number, because it has no channel that produces one.
 *
 * The tab holds the dashboard alone and offers no view switch. The raw crew-log
 * record is a Developer Mode surface, reached from the chat panel's own Crew log
 * view, so the thread's slot is not part of this tab's input. */
export default function CrewDashboardTab({ slug, member, displayName, onAct, onOpenProfileTab, preview = false }: {
  slug: string
  member: string
  displayName: string
  /** Put a reply the page offered into this crewmate's chat box. */
  onAct?: (text: string) => void
  /** Called when the person clicks the "Layout locked · Profile" badge to go
   *  straight to the Profile tab where the lock switch lives. */
  onOpenProfileTab?: () => void
  /** Show the STAGED page under the shared preview label instead of the live one. */
  preview?: boolean
}) {
  const { t } = useTranslation()
  // Once the staged page is applied or expires, "not applied" would be false. The
  // tab stays on the closed notice rather than switching to the live page: an
  // unlabelled live page under a preview link would read as the preview itself.
  const [previewGone, setPreviewGone] = useState(false)
  // A later preview link starts labelled again.
  useEffect(() => { if (!preview) setPreviewGone(false) }, [preview])
  return (
    <div className="h-full min-h-0 flex flex-col" data-testid="crew-dashboard-tab">
      {preview && !previewGone && <DashboardPreviewLabel />}
      <ErrorBoundary>
        <Suspense fallback={<p role="status" className="text-sm text-muted">{t('pages.membersPage.webview_rendering')}</p>}>
          {/* Keyed on the slug: a different crewmate is a different instance, a
              different manifest and a different minted document, and the held
              "last good page" must not survive the switch -- one crewmate's
              numbers under another's name is the one thing this surface must
              never show. Keyed on `preview` for the same reason: the staged page
              and the live one are two documents, and the held page of one must
              never be drawn under the other's label. */}
          <CrewDynamicDashboard
            key={`${slug}:${preview ? 'preview' : 'live'}`}
            target={{ kind: 'member', slug, member }}
            displayName={displayName || member}
            onAct={onAct}
            onOpenProfileTab={onOpenProfileTab}
            preview={preview}
            onPreviewGone={preview ? setPreviewGone : undefined}
          />
        </Suspense>
      </ErrorBoundary>
    </div>
  )
}

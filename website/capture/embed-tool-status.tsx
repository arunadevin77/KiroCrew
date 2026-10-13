/**
 * Isolated capture entry for two ChatEmbed defects: a finished tool call kept
 * the loader icon, and a single pending approval previewed only 150 chars.
 *
 * Mounts the REAL ChatEmbed against the real stylesheet, theme tokens and live
 * i18n catalog; the transcript comes from the capture script's route
 * interception (gateway-free). The script fulfils the slot read with a running
 * turn holding one finished call (its result on `meta.output`, as the backend
 * persists it) and one call parked on an approval whose command is long.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatEmbed from '../src/app-sdk/ChatEmbed'
import { AppScopedApiProvider } from '../src/app-sdk/scopedApi'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
})

async function main() {
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <div className="min-h-screen bg-bg text-text p-4" data-capture-root>
            <div className="w-[720px] h-[620px]" data-capture-box>
              <AppScopedApiProvider
                appName="capture-app"
                allowedApiPaths={['/api/chat*']}
                allowedEvents={[]}
                navigateFn={() => {}}
              >
                <ChatEmbed slotKey="capture-app-embed" startAtBottom />
              </AppScopedApiProvider>
            </div>
          </div>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

main()

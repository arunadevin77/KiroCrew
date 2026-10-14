/**
 * The off-screen draft restore's knowledge note (send() in pages/chat/page/composerSend.ts)
 * is a catalog key. A catalog value may not start with a space, so the call site prepends
 * it; the English the user reads is exactly the sentence the restore notice always carried.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { i18nT } from '../i18n/t'

describe('the knowledge-context note in the draft-restore notice', () => {
  it('reads, in English, exactly as the restore notice has always appended it', () => {
    expect(' ' + i18nT('pages.chatPage.knowledge_context_not_kept'))
      .toBe(' Its knowledge context was not kept — re-pick it before you resend.')
  })

  it('is composed with its leading space at the one call site', () => {
    const send = readFileSync(join(__dirname, 'chat', 'page', 'composerSend.ts'), 'utf8')
    expect(send.match(/' ' \+ i18nT\('pages\.chatPage\.knowledge_context_not_kept'\)/g)).toHaveLength(1)
  })
})

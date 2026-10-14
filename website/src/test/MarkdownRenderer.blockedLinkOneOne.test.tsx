import { it, expect } from 'vitest'
import { render, fireEvent, within } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import fixture from './fixtures/blockedLinkOneOne.json'
import { expectCardListsHostRecords } from './blockedLinkCardAsserts'

// Backend output of TestBlockedLinkCardRecords._one_presigned_one_key_split_flush
// (test/test_chat_runner_coverage.py), byte for byte: ONE presigned S3 link
// split at &X-Amz-Date (a placeholder with no record of its own) beside ONE
// same-host URL whose key a delta redacted (a record the text shows no
// placeholder for). One placeholder and one record, of different addresses.
// The chip names the host alone: one record on the host does not prove it is
// this chip's link. The card explains that record and never presents it as the
// report.csv link the chip stands for.
it('one placeholder and one record of different addresses: the record shows its own address, not as the clicked link', () => {
  const { getAllByTestId, getByTestId } = render(
    <MarkdownRenderer content={fixture.content} blockedLinks={fixture.blocked_links} slotKey="s1" />)
  expect(getAllByTestId('blocked-link-inspect')).toHaveLength(1)
  const host = fixture.blocked_links[0].domain
  fireEvent.click(getAllByTestId('blocked-link-inspect')[0])
  expectCardListsHostRecords(getByTestId('blocked-link-card'), fixture.blocked_links, host)
  expect(getByTestId('blocked-link-card').textContent).not.toContain('report.csv')
})

it('chip label does not name a record as the removed link', () => {
  const { getAllByTestId } = render(
    <MarkdownRenderer content={fixture.content} blockedLinks={fixture.blocked_links} slotKey="s1" />)
  const chips = getAllByTestId('blocked-link-chip')
  expect(chips).toHaveLength(1)
  const rec = fixture.blocked_links[0]
  expect(within(chips[0]).getByTestId('blocked-link-target').textContent).toBe(rec.domain)
  expect(chips[0].textContent).not.toContain(rec.path)
  expect(within(chips[0]).queryByTestId('blocked-link-query')).toBeNull()
})

it('neither the chip nor the lone record\'s card names /other-a as the removed link', () => {
  // Nothing proves the one record is the chip's link, so no surface presents
  // its path as the removed address: the card keeps main's lone-record shape
  // (Destination host, reason, the withheld note) with no address line.
  const { getByTestId } = render(
    <MarkdownRenderer content={fixture.content} blockedLinks={fixture.blocked_links} slotKey="s1" />)
  fireEvent.click(getByTestId('blocked-link-inspect'))
  const card = getByTestId('blocked-link-card')
  expect(within(card).queryByTestId('blocked-link-entry-target')).toBeNull()
  expect(within(card).queryByTestId('blocked-link-review')).toBeNull()
  expect(card.textContent).toContain(fixture.blocked_links[0].domain)
})

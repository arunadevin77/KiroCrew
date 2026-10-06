import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CommentList, type InlineComment } from '../components/CommentOverlay'

// A box comment's row carries the number drawn on its box over the image, so a
// reader with several boxes can match a box to its row by looking. The number
// is the comment's place in the pending list, the same one the image layer
// draws, so a text comment above it still counts.

const COMMENTS: InlineComment[] = [
  { id: 'c1', anchor: 'first paragraph', text: 'tighten this' },
  { id: 'c2', anchor: 'Schedule page', text: 'too tight', image: { src: 'a.png', x: 0.34, y: 0.33, w: 0.2, h: 0.2 } },
]

describe('CommentList image rows', () => {
  it('names the box by its number and the image, with no coordinates', () => {
    render(<CommentList comments={COMMENTS} onEdit={vi.fn()} onRemove={vi.fn()} onSubmitAll={vi.fn()} />)
    expect(screen.getByText('Box 2 on "Schedule page"')).toBeInTheDocument()
    expect(screen.queryByText(/34%/)).toBeNull()
  })
})

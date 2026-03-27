import { describe, expect, it } from 'vitest'

import { assistantPendingAnchorMs, isAtomicityRelevantObjective, latestPendingTurn } from './taskConversation'

describe('taskConversation', () => {
  it('anchors pending elapsed time to queued_at before created_at', () => {
    const queuedAt = '2026-03-27T18:00:00.000Z'
    const createdAt = '2026-03-27T18:05:00.000Z'
    expect(assistantPendingAnchorMs({ queued_at: queuedAt, created_at: createdAt })).toBe(new Date(queuedAt).getTime())
  })

  it('returns the most recent pending turn', () => {
    expect(
      latestPendingTurn([
        { created_at: '2026-03-27T18:00:00.000Z', pending: true },
        { created_at: '2026-03-27T18:01:00.000Z', pending: false },
        { created_at: '2026-03-27T18:02:00.000Z', pending: true },
      ]),
    ).toEqual({
      created_at: '2026-03-27T18:02:00.000Z',
      pending: true,
    })
  })

  it('keeps blocking failed objectives in the atomicity queue', () => {
    expect(
      isAtomicityRelevantObjective({
        unresolved_failed_count: 1,
        task_counts: { active: 0, pending: 0 },
        atomic_generation: { status: 'completed' },
        workflow: { current_stage: 'review' },
      }),
    ).toBe(true)
  })

  it('filters out promotion-only history with no active atomicity work', () => {
    expect(
      isAtomicityRelevantObjective({
        unresolved_failed_count: 0,
        task_counts: { active: 0, pending: 0 },
        atomic_generation: { status: 'completed' },
        workflow: { current_stage: 'review' },
      }),
    ).toBe(false)
  })
})

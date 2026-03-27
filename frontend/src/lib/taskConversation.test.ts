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

  it('falls back to created_at or now when queued_at is missing or invalid', () => {
    const createdAt = '2026-03-27T18:05:00.000Z'
    expect(assistantPendingAnchorMs({ created_at: createdAt })).toBe(new Date(createdAt).getTime())
    expect(assistantPendingAnchorMs({ queued_at: 'not-a-date' }, 1234)).toBe(1234)
  })

  it('returns null when there is no pending turn and includes other atomicity-relevant states', () => {
    expect(latestPendingTurn([{ pending: false }])).toBeNull()
    expect(
      isAtomicityRelevantObjective({
        unresolved_failed_count: 0,
        task_counts: { active: 1, pending: 0 },
        atomic_generation: { status: 'completed' },
        workflow: { current_stage: 'review' },
      }),
    ).toBe(true)
    expect(
      isAtomicityRelevantObjective({
        unresolved_failed_count: 0,
        task_counts: { active: 0, pending: 0 },
        atomic_generation: { status: 'running' },
        workflow: { current_stage: 'review' },
      }),
    ).toBe(true)
    expect(
      isAtomicityRelevantObjective({
        unresolved_failed_count: 0,
        task_counts: { active: 0, pending: 0 },
        atomic_generation: { status: 'completed' },
        workflow: { current_stage: 'planning' },
      }),
    ).toBe(true)
  })

  it('falls back cleanly when no timestamps or objective fields are present', () => {
    expect(assistantPendingAnchorMs(undefined, 999)).toBe(999)
    expect(
      isAtomicityRelevantObjective({
        workflow: { current_stage: 'execution' },
      }),
    ).toBe(true)
    expect(
      isAtomicityRelevantObjective({}),
    ).toBe(false)
  })
})

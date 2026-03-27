import { describe, expect, it } from 'vitest'

import { buildContextChangeDetail, resolveVisibleContext } from './contextState'

describe('contextState', () => {
  it('falls back to persisted objective context when the route has no objective param', () => {
    expect(
      resolveVisibleContext({
        currentProjectId: '',
        currentObjectiveId: '',
        lastProjectId: 'project_1',
        lastObjectiveId: 'objective_9',
      }),
    ).toEqual({
      projectId: 'project_1',
      objectiveId: 'objective_9',
    })
  })

  it('prefers current route params over persisted context', () => {
    expect(
      resolveVisibleContext({
        currentProjectId: 'project_live',
        currentObjectiveId: 'objective_live',
        lastProjectId: 'project_old',
        lastObjectiveId: 'objective_old',
      }),
    ).toEqual({
      projectId: 'project_live',
      objectiveId: 'objective_live',
    })
  })

  it('builds a stable context-change payload', () => {
    expect(buildContextChangeDetail('project_a', 'objective_b')).toEqual({
      projectId: 'project_a',
      objectiveId: 'objective_b',
    })
  })
})

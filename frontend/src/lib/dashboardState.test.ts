import { describe, expect, it } from 'vitest'

import { dashboardEventLink, projectDashboardState, projectTaskStatus, unresolvedObjectiveCount } from './dashboardState'

describe('dashboardState', () => {
  it('routes task-backed recent signals into the atomicity task focus view', () => {
    expect(dashboardEventLink({ project_id: 'project_1', objective_id: 'objective_2', task_id: 'task_3' })).toEqual({
      to: {
        name: 'objective-atomic',
        params: { projectId: 'project_1', objectiveId: 'objective_2' },
        query: { taskId: 'task_3' },
      },
    })
  })

  it('routes objective-only recent signals to the objective overview', () => {
    expect(dashboardEventLink({ project_id: 'project_1', objective_id: 'objective_2' })).toEqual({
      to: {
        name: 'objective',
        params: { projectId: 'project_1', objectiveId: 'objective_2' },
      },
    })
  })

  it('computes unresolved objective counts and task status summaries', () => {
    const project = {
      objectives: [{ status: 'resolved' }, { status: 'paused' }, { status: 'open' }],
      tasks_by_status: { active: 1, pending: 2, failed: 3, completed: 4 },
    }
    expect(unresolvedObjectiveCount(project)).toBe(2)
    expect(projectTaskStatus(project)).toEqual({
      active: 1,
      pending: 2,
      failed: 3,
      completed: 4,
    })
  })

  it('marks projects with unresolved idle work as needing attention', () => {
    expect(
      projectDashboardState({
        objectives: [{ status: 'paused' }],
        tasks_by_status: { active: 0, pending: 0, failed: 0, completed: 9 },
      }),
    ).toEqual({
      tone: 'warning',
      label: 'Needs attention',
      detail: '1 unresolved objectives remain even though no work is currently running.',
    })
  })

  it('covers running, queued, historical, and quiet project states', () => {
    expect(
      projectDashboardState({
        objectives: [{ status: 'resolved' }],
        tasks_by_status: { active: 2, pending: 0, failed: 0, completed: 0 },
      }).label,
    ).toBe('Running')

    expect(
      projectDashboardState({
        objectives: [{ status: 'resolved' }],
        tasks_by_status: { active: 0, pending: 2, failed: 0, completed: 0 },
      }).label,
    ).toBe('Queued')

    expect(
      projectDashboardState({
        objectives: [{ status: 'resolved' }],
        tasks_by_status: { active: 0, pending: 0, failed: 2, completed: 4 },
      }).label,
    ).toBe('History to review')

    expect(
      projectDashboardState({
        objectives: [{ status: 'resolved' }],
        tasks_by_status: { active: 0, pending: 0, failed: 0, completed: 4 },
      }).label,
    ).toBe('Quiet')
  })

  it('returns an empty link when a recent signal lacks objective context', () => {
    expect(dashboardEventLink({ project_id: 'project_1' })).toEqual({})
  })
})

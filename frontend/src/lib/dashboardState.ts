export type DashboardEvent = {
  project_id?: string
  objective_id?: string
  task_id?: string
}

export type ProjectSummaryLike = {
  objectives?: Array<{ status?: string }>
  tasks_by_status?: Record<string, number>
}

export function dashboardEventLink(event: DashboardEvent) {
  if (!event.project_id || !event.objective_id) return {}
  if (event.task_id) {
    return {
      to: {
        name: 'objective-atomic',
        params: { projectId: event.project_id, objectiveId: event.objective_id },
        query: { taskId: event.task_id },
      },
    }
  }
  return {
    to: {
      name: 'objective',
      params: { projectId: event.project_id, objectiveId: event.objective_id },
    },
  }
}

export function unresolvedObjectiveCount(project: ProjectSummaryLike): number {
  return (project.objectives || []).filter((objective) => objective.status !== 'resolved').length
}

export function projectTaskStatus(project: ProjectSummaryLike) {
  const status = project.tasks_by_status || {}
  return {
    completed: Number(status.completed || 0),
    active: Number(status.active || 0),
    pending: Number(status.pending || 0),
    failed: Number(status.failed || 0),
  }
}

export function projectDashboardState(project: ProjectSummaryLike) {
  const tasks = projectTaskStatus(project)
  const unresolved = unresolvedObjectiveCount(project)
  if (tasks.active > 0) {
    return { tone: 'info', label: 'Running', detail: 'The harness is actively executing work in this project.' }
  }
  if (tasks.pending > 0) {
    return { tone: 'warning', label: 'Queued', detail: 'This project has pending work waiting for execution or review.' }
  }
  if (unresolved > 0) {
    return { tone: 'warning', label: 'Needs attention', detail: `${unresolved} unresolved objectives remain even though no work is currently running.` }
  }
  if (tasks.failed > 0) {
    return { tone: 'surface-variant', label: 'History to review', detail: 'Execution is complete, but the project contains failed task history.' }
  }
  return { tone: 'success', label: 'Quiet', detail: 'No active or queued work is present for this project.' }
}

export type ContextStateInput = {
  currentProjectId?: string
  currentObjectiveId?: string
  lastProjectId?: string
  lastObjectiveId?: string
}

export type ResolvedContextState = {
  projectId: string
  objectiveId: string
}

export function resolveVisibleContext(input: ContextStateInput): ResolvedContextState {
  return {
    projectId: input.currentProjectId || input.lastProjectId || '',
    objectiveId: input.currentObjectiveId || input.lastObjectiveId || '',
  }
}

export function buildContextChangeDetail(projectId: string, objectiveId: string): { projectId: string; objectiveId: string } {
  return { projectId, objectiveId }
}

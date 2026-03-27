export type ConversationTurn = {
  queued_at?: string
  created_at?: string
  pending?: boolean
}

export type ObjectiveLike = {
  unresolved_failed_count?: number
  task_counts?: {
    active?: number
    pending?: number
  }
  atomic_generation?: {
    status?: string
  }
  workflow?: {
    current_stage?: string
  }
}

export function assistantPendingAnchorMs(turn: ConversationTurn | null | undefined, nowMs = Date.now()): number {
  const anchorRaw = turn?.queued_at || turn?.created_at || ''
  const anchorMs = anchorRaw ? new Date(anchorRaw).getTime() : nowMs
  return Number.isNaN(anchorMs) ? nowMs : anchorMs
}

export function latestPendingTurn(turns: ConversationTurn[]): ConversationTurn | null {
  const pendingTurns = turns.filter((turn) => Boolean(turn.pending))
  return pendingTurns[pendingTurns.length - 1] || null
}

export function isAtomicityRelevantObjective(objective: ObjectiveLike): boolean {
  const counts = objective.task_counts || {}
  const generation = objective.atomic_generation || {}
  const currentStage = String(objective.workflow?.current_stage || '')
  const unresolvedFailed = Number(objective.unresolved_failed_count || 0)
  if (unresolvedFailed > 0) return true
  if (Number(counts.active || 0) > 0 || Number(counts.pending || 0) > 0) return true
  if (generation.status === 'running') return true
  if (currentStage === 'planning' || currentStage === 'execution') return true
  return false
}

export type AssistantTurnLike = {
  pending?: boolean
}

export function hasAssistantPending(turns: AssistantTurnLike[]): boolean {
  return turns.some((turn) => Boolean(turn.pending))
}

export function assistantSendButtonState(isSubmitting: boolean): { disabled: boolean; label: string } {
  return {
    disabled: isSubmitting,
    label: isSubmitting ? 'Sending…' : 'Send',
  }
}

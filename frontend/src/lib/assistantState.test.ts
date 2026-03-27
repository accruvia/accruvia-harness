import { describe, expect, it } from 'vitest'

import { assistantSendButtonState, hasAssistantPending } from './assistantState'

describe('assistantState', () => {
  it('detects existing pending turns in conversation history', () => {
    expect(hasAssistantPending([{ pending: false }, { pending: true }])).toBe(true)
    expect(hasAssistantPending([{ pending: false }, {}])).toBe(false)
  })

  it('does not mark the send button as sending just because history has pending turns', () => {
    const state = assistantSendButtonState(false)
    expect(state.disabled).toBe(false)
    expect(state.label).toBe('Send')
  })

  it('marks the send button as sending only while the current draft is submitting', () => {
    const state = assistantSendButtonState(true)
    expect(state.disabled).toBe(true)
    expect(state.label).toBe('Sending…')
  })
})

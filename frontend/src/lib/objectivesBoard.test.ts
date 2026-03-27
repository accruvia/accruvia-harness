import { describe, expect, it } from 'vitest'

import { filterObjectives, sortObjectives } from './objectivesBoard'

describe('objectivesBoard', () => {
  it('sorts resolved objectives to the bottom of the global board', () => {
    const sorted = sortObjectives([
      { id: '3', title: 'Resolved item', status: 'resolved' },
      { id: '1', title: 'Investigating item', status: 'investigating' },
      { id: '2', title: 'Open item', status: 'open' },
    ])
    expect(sorted.map((item) => item.id)).toEqual(['1', '2', '3'])
  })

  it('filters objectives by project and free-text query', () => {
    const filtered = filterObjectives(
      [
        { id: '1', title: 'Context Management', status: 'open', project_name: 'accruvia-harness' },
        { id: '2', title: 'Billing API', status: 'paused', project_name: 'other-project' },
      ],
      'accruvia-harness',
      'context',
    )
    expect(filtered.map((item) => item.id)).toEqual(['1'])
  })

  it('keeps same-status objectives alphabetized and returns all items for an empty query', () => {
    const sorted = sortObjectives([
      { id: '2', title: 'Zulu', status: 'open' },
      { id: '1', title: 'Alpha', status: 'open' },
    ])
    expect(sorted.map((item) => item.id)).toEqual(['1', '2'])

    const filtered = filterObjectives(sorted, '', '')
    expect(filtered.map((item) => item.id)).toEqual(['1', '2'])
  })

  it('handles missing status and title fields while still filtering by query', () => {
    const sorted = sortObjectives([
      { id: '1', project_name: 'alpha' },
      { id: '2', title: 'Beta', status: 'open', project_name: 'alpha' },
    ])
    expect(sorted.map((item) => item.id)).toEqual(['2', '1'])

    const filtered = filterObjectives(sorted, 'alpha', 'beta')
    expect(filtered.map((item) => item.id)).toEqual(['2'])
  })
})

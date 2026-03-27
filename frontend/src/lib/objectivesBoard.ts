export type ObjectiveBoardItem = {
  id: string
  title?: string
  status?: string
  project_id?: string
  project_name?: string
}

export function sortObjectives(items: ObjectiveBoardItem[]): ObjectiveBoardItem[] {
  const rank: Record<string, number> = { executing: 0, planning: 1, investigating: 2, open: 3, paused: 4, resolved: 5 }
  return [...items].sort((left, right) => {
    const delta = (rank[left.status || ''] ?? 99) - (rank[right.status || ''] ?? 99)
    if (delta !== 0) return delta
    return String(left.title || '').localeCompare(String(right.title || ''))
  })
}

export function filterObjectives(items: ObjectiveBoardItem[], projectFilter: string, query: string): ObjectiveBoardItem[] {
  const normalizedQuery = query.trim().toLowerCase()
  return items.filter((objective) => {
    const matchesProject = !projectFilter || objective.project_name === projectFilter
    if (!matchesProject) return false
    if (!normalizedQuery) return true
    const haystack = `${objective.title || ''} ${objective.project_name || ''} ${objective.status || ''}`.toLowerCase()
    return haystack.includes(normalizedQuery)
  })
}

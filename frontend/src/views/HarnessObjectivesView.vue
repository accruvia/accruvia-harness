<template>
  <v-container fluid class="pa-6">
    <div class="d-flex align-center mb-6">
      <div>
        <div class="text-caption text-uppercase text-on-surface-variant">Harness Workspace</div>
        <h1 class="text-h4 font-weight-bold text-on-surface">Objectives</h1>
      </div>
    </div>

    <HarnessSectionNav />

    <div class="d-flex align-center justify-space-between flex-wrap ga-3 mt-6 mb-4">
      <h2 class="text-subtitle-2 text-uppercase text-on-surface-variant tracking-wide">Global Objectives Board</h2>
      <div class="text-caption text-on-surface-variant">{{ objectives.length }} objectives</div>
    </div>

    <div class="d-flex flex-column ga-3">
      <v-card v-for="objective in objectives" :key="objective.id" color="surface-light" class="pa-4">
        <div class="d-flex align-center flex-wrap ga-3 mb-2">
          <v-chip :color="statusColor(objective.status)" size="x-small" label>{{ objective.status }}</v-chip>
          <div class="text-caption text-uppercase text-on-surface-variant">{{ objective.project_name }}</div>
        </div>
        <h3 class="text-subtitle-1 font-weight-medium text-on-surface mb-2">{{ objective.title }}</h3>
        <div class="d-flex ga-4 flex-wrap text-caption text-on-surface-variant mb-3">
          <span>{{ objective.task_counts?.active || 0 }} active</span>
          <span>{{ objective.task_counts?.pending || 0 }} pending</span>
          <span>{{ objective.task_counts?.completed || 0 }} completed</span>
          <span>{{ objective.task_counts?.failed || 0 }} failed</span>
        </div>
        <div class="d-flex flex-wrap ga-2">
          <v-btn size="small" variant="tonal" prepend-icon="$bookOpenVariant" :to="{ name: 'objective', params: { projectId: objective.project_id, objectiveId: objective.id } }">Overview</v-btn>
          <v-btn size="small" variant="tonal" prepend-icon="$sourceBranch" :to="{ name: 'objective-atomic', params: { projectId: objective.project_id, objectiveId: objective.id } }">Atomicity</v-btn>
          <v-btn size="small" variant="tonal" prepend-icon="$rocketLaunch" :to="{ name: 'objective-promotion', params: { projectId: objective.project_id, objectiveId: objective.id } }">Promotion</v-btn>
        </div>
      </v-card>
    </div>
  </v-container>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useApi } from '../composables/useApi'
import HarnessSectionNav from '../components/HarnessSectionNav.vue'

const { data: harness, fetch: fetchHarness } = useApi<any>('/api/harness')

const objectives = computed(() => {
  const projects = harness.value?.projects || []
  return projects
    .flatMap((project: any) => (project.objectives || []).map((objective: any) => ({
      ...objective,
      project_id: project.id,
      project_name: project.name,
    })))
    .sort((left: any, right: any) => {
      const rank: Record<string, number> = { executing: 0, planning: 1, investigating: 2, open: 3, paused: 4, resolved: 5 }
      const delta = (rank[left.status] ?? 99) - (rank[right.status] ?? 99)
      if (delta !== 0) return delta
      return String(left.title || '').localeCompare(String(right.title || ''))
    })
})

function statusColor(status: string) {
  const colors: Record<string, string> = {
    resolved: 'success',
    executing: 'info',
    planning: 'warning',
    paused: 'error',
    open: 'on-surface-variant',
    investigating: 'info',
  }
  return colors[status] || 'on-surface-variant'
}

onMounted(() => {
  void fetchHarness()
})
</script>

<template>
  <v-container fluid class="pa-6">
    <div class="mb-6">
      <div class="text-caption text-uppercase text-on-surface-variant">Harness Workspace</div>
      <h1 class="text-h4 font-weight-bold text-on-surface">Atomicity</h1>
    </div>

    <HarnessSectionNav />

    <div class="d-flex flex-column ga-3 mt-6">
      <v-card v-for="objective in objectives" :key="objective.id" color="surface-light" class="pa-4">
        <div class="d-flex align-center justify-space-between flex-wrap ga-3 mb-2">
          <div>
            <div class="text-caption text-uppercase text-on-surface-variant">{{ objective.project_name }}</div>
            <h3 class="text-subtitle-1 font-weight-medium text-on-surface">{{ objective.title }}</h3>
          </div>
          <v-chip :color="activity(objective).tone" variant="tonal" size="small">{{ activity(objective).label }}</v-chip>
        </div>
        <div class="text-body-2 text-on-surface-variant mb-3">{{ activity(objective).detail }}</div>
        <div class="d-flex ga-4 flex-wrap text-caption text-on-surface-variant mb-3">
          <span>{{ objective.task_counts?.active || 0 }} active</span>
          <span>{{ objective.task_counts?.pending || 0 }} pending</span>
          <span>{{ objective.task_counts?.completed || 0 }} completed</span>
          <span>{{ objective.task_counts?.failed || 0 }} failed</span>
        </div>
        <div class="d-flex flex-wrap ga-2">
          <v-btn size="small" variant="tonal" prepend-icon="$sourceBranch" :to="{ name: 'objective-atomic', params: { projectId: objective.project_id, objectiveId: objective.id } }">Open Atomicity</v-btn>
          <v-btn size="small" variant="tonal" prepend-icon="$bookOpenVariant" :to="{ name: 'objective', params: { projectId: objective.project_id, objectiveId: objective.id } }">Overview</v-btn>
        </div>
      </v-card>
    </div>
  </v-container>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useApi } from '../composables/useApi'
import HarnessSectionNav from '../components/HarnessSectionNav.vue'

const { data, fetch } = useApi<any>('/api/atomicity')

const objectives = computed(() => data.value?.objectives || [])

function firstFailedCheck(checkGroup: any) {
  return (checkGroup?.checks || []).find((check: any) => !check.ok)
}

function activity(objective: any) {
  const counts = objective.task_counts || {}
  if ((counts.active || 0) > 0) {
    return { tone: 'info', label: `${counts.active} active tasks`, detail: 'The harness is currently executing atomic units for this objective.' }
  }
  const generation = objective.atomic_generation || {}
  if (generation.status === 'running') {
    return { tone: 'info', label: 'Generating atomic units', detail: generation.phase || 'The harness is deriving or refining atomic units from the Mermaid.' }
  }
  const blocker = firstFailedCheck(objective.execution_gate)
  if (blocker?.key === 'interrogation_complete') {
    return { tone: 'warning', label: 'Waiting on interrogation', detail: blocker.detail }
  }
  if (blocker?.key === 'mermaid_finished') {
    return { tone: 'warning', label: 'Waiting on Mermaid', detail: blocker.detail }
  }
  if ((counts.pending || 0) > 0) {
    return { tone: 'warning', label: `${counts.pending} pending tasks`, detail: 'Atomic tasks exist but are waiting to run.' }
  }
  if ((counts.failed || 0) > 0) {
    return { tone: 'error', label: `${counts.failed} failed tasks`, detail: 'Atomic work has failed tasks that need remediation or review.' }
  }
  if ((counts.completed || 0) > 0) {
    return { tone: 'success', label: 'Atomic work completed', detail: 'Atomic execution has completed for this objective.' }
  }
  return { tone: 'surface-variant', label: 'Idle', detail: 'No atomic generation or task execution is active right now.' }
}

onMounted(() => {
  void fetch()
})
</script>

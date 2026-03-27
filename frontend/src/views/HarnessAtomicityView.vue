<template>
  <v-container fluid class="pa-6">
    <div class="mb-6">
      <div class="page-kicker">Harness workspace</div>
      <h1 class="page-title">Atomicity</h1>
    </div>

    <HarnessSectionNav />

    <v-row class="mt-6">
      <v-col cols="12" lg="7">
        <div class="d-flex align-center justify-space-between flex-wrap ga-3 mb-4">
          <h2 class="section-title">Atomic work queue</h2>
          <div class="section-meta">{{ objectives.length }} objectives</div>
        </div>

        <div class="objective-grid">
          <button
            v-for="objective in objectives"
            :key="objective.id"
            type="button"
            class="objective-tile"
            :class="{ active: selectedObjective?.id === objective.id }"
            @click="selectedId = objective.id"
          >
            <div class="d-flex align-center justify-space-between ga-3 mb-2">
              <div class="tile-project">{{ objective.project_name }}</div>
              <v-chip :color="activity(objective).tone" size="x-small" variant="tonal">{{ activity(objective).label }}</v-chip>
            </div>
            <div class="tile-title">{{ objective.title }}</div>
            <div class="tile-copy mt-2">{{ activity(objective).detail }}</div>
            <div class="tile-stats mt-3">
              <span>{{ objective.task_counts?.active || 0 }} active</span>
              <span>{{ objective.task_counts?.pending || 0 }} pending</span>
              <span>{{ objective.task_counts?.completed || 0 }} completed</span>
              <span>{{ objective.task_counts?.failed || 0 }} failed</span>
            </div>
          </button>
        </div>
      </v-col>

      <v-col cols="12" lg="5">
        <div class="detail-panel">
          <v-card v-if="selectedObjective" color="surface-light" class="pa-5">
            <div class="d-flex align-center justify-space-between ga-3 mb-3">
              <div>
                <div class="panel-project">{{ selectedObjective.project_name }}</div>
                <h2 class="panel-title">{{ selectedObjective.title }}</h2>
              </div>
              <v-chip :color="activity(selectedObjective).tone" variant="tonal">{{ activity(selectedObjective).label }}</v-chip>
            </div>

            <div class="detail-stat-grid mb-4">
              <div class="detail-stat">
                <div class="label">Generation</div>
                <div class="value">{{ generationLabel(selectedObjective) }}</div>
              </div>
              <div class="detail-stat">
                <div class="label">Current Stage</div>
                <div class="value">{{ selectedObjective.workflow?.current_stage || 'planning' }}</div>
              </div>
              <div class="detail-stat">
                <div class="label">Active</div>
                <div class="value">{{ selectedObjective.task_counts?.active || 0 }}</div>
              </div>
              <div class="detail-stat">
                <div class="label">Failed</div>
                <div class="value">{{ selectedObjective.task_counts?.failed || 0 }}</div>
              </div>
            </div>

            <div class="panel-label mb-2">Current constraint</div>
            <div class="panel-copy mb-4">{{ activity(selectedObjective).detail }}</div>

            <div v-if="blockingReason(selectedObjective)" class="detail-callout mb-4">
              <div class="panel-label mb-1">Blocking reason</div>
              <div class="text-body-2 text-on-surface">{{ blockingReason(selectedObjective) }}</div>
            </div>

            <div class="d-flex flex-wrap ga-2">
              <v-btn size="small" prepend-icon="$sourceBranch" :to="{ name: 'objective-atomic', params: { projectId: selectedObjective.project_id, objectiveId: selectedObjective.id } }">Open Atomicity</v-btn>
              <v-btn size="small" variant="tonal" prepend-icon="$bookOpenVariant" :to="{ name: 'objective', params: { projectId: selectedObjective.project_id, objectiveId: selectedObjective.id } }">Overview</v-btn>
            </div>
          </v-card>
        </div>
      </v-col>
    </v-row>
  </v-container>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useApi } from '../composables/useApi'
import HarnessSectionNav from '../components/HarnessSectionNav.vue'

const { data, fetch } = useApi<any>('/api/atomicity')
const selectedId = ref('')
const objectives = computed(() => data.value?.objectives || [])
const selectedObjective = computed(() => objectives.value.find((objective: any) => objective.id === selectedId.value) || objectives.value[0] || null)

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

function blockingReason(objective: any) {
  return firstFailedCheck(objective.execution_gate)?.detail || ''
}

function generationLabel(objective: any) {
  const generation = objective.atomic_generation || {}
  if (generation.status === 'running') return 'running'
  if (generation.status === 'completed') return `v${generation.diagram_version || '?'} done`
  if (generation.status === 'failed') return 'failed'
  return 'idle'
}

onMounted(async () => {
  await fetch()
  if (!selectedId.value && objectives.value[0]?.id) selectedId.value = objectives.value[0].id
})
</script>

<style scoped>
.objective-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 1rem;
}

.objective-tile {
  border: 1px solid rgba(125, 94, 67, 0.12);
  border-radius: 16px;
  background: rgba(255, 251, 245, 0.82);
  padding: 1rem;
  text-align: left;
  color: rgb(var(--v-theme-on-surface));
  transition: border-color 160ms ease, transform 160ms ease, box-shadow 160ms ease;
}

.objective-tile:hover {
  border-color: rgba(179, 92, 46, 0.28);
  transform: translateY(-1px);
}

.objective-tile.active {
  border-color: rgba(179, 92, 46, 0.42);
  box-shadow: 0 10px 30px rgba(179, 92, 46, 0.08);
}

.tile-title {
  font-size: 1.02rem;
  font-weight: 600;
  line-height: 1.4;
}

.tile-copy,
.panel-copy,
.tile-stats,
.page-kicker,
.section-meta,
.tile-project,
.panel-project,
.section-title,
.panel-label {
  color: rgb(var(--v-theme-on-surface-variant));
}

.page-kicker,
.tile-project,
.panel-project,
.section-meta,
.panel-label {
  font-size: 0.78rem;
}

.page-title {
  margin-top: 0.15rem;
  font-size: 2rem;
  font-weight: 650;
  color: rgb(var(--v-theme-on-surface));
}

.section-title {
  font-size: 0.96rem;
  font-weight: 600;
}

.tile-copy,
.panel-copy {
  font-size: 0.92rem;
  line-height: 1.5;
}

.tile-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 0.85rem;
  font-size: 0.82rem;
}

.detail-panel {
  position: sticky;
  top: 1.5rem;
}

.panel-title {
  margin-top: 0.25rem;
  font-size: 1.55rem;
  font-weight: 620;
  line-height: 1.25;
  color: rgb(var(--v-theme-on-surface));
}

.detail-stat-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.75rem;
}

.detail-stat,
.detail-callout {
  border: 1px solid rgba(125, 94, 67, 0.12);
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.48);
  padding: 0.85rem;
}

.detail-stat .label {
  font-size: 0.74rem;
  color: rgb(var(--v-theme-on-surface-variant));
}

.detail-stat .value {
  margin-top: 0.25rem;
  font-size: 1rem;
  font-weight: 600;
  color: rgb(var(--v-theme-on-surface));
}
</style>

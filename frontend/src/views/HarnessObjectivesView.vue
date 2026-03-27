<template>
  <v-container fluid class="pa-6">
    <div class="d-flex align-center mb-6">
      <div>
        <div class="page-kicker">Harness workspace</div>
        <h1 class="page-title">Objectives</h1>
      </div>
    </div>

    <HarnessSectionNav />

    <v-row class="mt-6">
      <v-col cols="12" lg="7">
        <div class="d-flex align-center justify-space-between flex-wrap ga-3 mb-4">
          <h2 class="section-title">Global objectives board</h2>
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
            <div class="d-flex align-center justify-space-between ga-3 mb-3">
              <v-chip :color="statusColor(objective.status)" size="x-small" variant="tonal">{{ statusLabel(objective.status) }}</v-chip>
              <div class="tile-project">{{ objective.project_name }}</div>
            </div>
            <div class="tile-title">{{ objective.title }}</div>
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
              <v-chip :color="statusColor(selectedObjective.status)" variant="tonal">{{ statusLabel(selectedObjective.status) }}</v-chip>
            </div>

            <div class="detail-stat-grid mb-4">
              <div class="detail-stat">
                <div class="label">Active</div>
                <div class="value">{{ selectedObjective.task_counts?.active || 0 }}</div>
              </div>
              <div class="detail-stat">
                <div class="label">Pending</div>
                <div class="value">{{ selectedObjective.task_counts?.pending || 0 }}</div>
              </div>
              <div class="detail-stat">
                <div class="label">Completed</div>
                <div class="value">{{ selectedObjective.task_counts?.completed || 0 }}</div>
              </div>
              <div class="detail-stat">
                <div class="label">Failed</div>
                <div class="value">{{ selectedObjective.task_counts?.failed || 0 }}</div>
              </div>
            </div>

            <div class="panel-label mb-2">What this means</div>
            <div class="panel-copy mb-4">{{ objectiveSummary(selectedObjective) }}</div>

            <div class="d-flex flex-wrap ga-2">
              <v-btn size="small" prepend-icon="$bookOpenVariant" :to="{ name: 'objective', params: { projectId: selectedObjective.project_id, objectiveId: selectedObjective.id } }">Overview</v-btn>
              <v-btn size="small" variant="tonal" prepend-icon="$sourceBranch" :to="{ name: 'objective-atomic', params: { projectId: selectedObjective.project_id, objectiveId: selectedObjective.id } }">Atomicity</v-btn>
              <v-btn size="small" variant="tonal" prepend-icon="$rocketLaunch" :to="{ name: 'objective-promotion', params: { projectId: selectedObjective.project_id, objectiveId: selectedObjective.id } }">Promotion</v-btn>
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

const { data: harness, fetch: fetchHarness } = useApi<any>('/api/harness')
const selectedId = ref('')

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
      const delta = (rank[left.status] ?? 99) - (ranksafe(rank, right.status))
      if (delta !== 0) return delta
      return String(left.title || '').localeCompare(String(right.title || ''))
    })
})

const selectedObjective = computed(() => {
  return objectives.value.find((objective: any) => objective.id === selectedId.value) || objectives.value[0] || null
})

function ranksafe(rank: Record<string, number>, status: string) {
  return rank[status] ?? 99
}

function statusColor(status: string) {
  const colors: Record<string, string> = {
    resolved: 'success',
    executing: 'info',
    planning: 'warning',
    paused: 'error',
    open: 'secondary',
    investigating: 'info',
  }
  return colors[status] || 'on-surface-variant'
}

function statusLabel(status: string) {
  return String(status || '').replaceAll('_', ' ')
}

function objectiveSummary(objective: any) {
  const counts = objective.task_counts || {}
  if ((counts.active || 0) > 0) return 'The harness is actively working on this objective right now.'
  if ((counts.pending || 0) > 0) return 'This objective has queued work that is waiting to execute.'
  if (objective.status === 'paused') return 'This objective is paused and likely needs an operator decision before work resumes.'
  if (objective.status === 'investigating') return 'This objective has entered planning, but atomic work has not started yet.'
  if (objective.status === 'open') return 'This objective exists but has not started meaningful planning or execution work yet.'
  if ((counts.failed || 0) > 0) return 'Execution is complete, but failed task history remains attached to this objective.'
  return 'This objective is resolved and currently quiet.'
}

onMounted(async () => {
  await fetchHarness()
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

.tile-project,
.panel-project,
.section-meta,
.page-kicker {
  font-size: 0.78rem;
  color: rgb(var(--v-theme-on-surface-variant));
}

.page-kicker,
.panel-project {
  letter-spacing: 0.02em;
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
  color: rgb(var(--v-theme-on-surface-variant));
}

.tile-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 0.85rem;
  font-size: 0.82rem;
  color: rgb(var(--v-theme-on-surface-variant));
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

.panel-label {
  font-size: 0.8rem;
  color: rgb(var(--v-theme-on-surface-variant));
}

.panel-copy {
  font-size: 0.95rem;
  line-height: 1.55;
  color: rgb(var(--v-theme-on-surface-variant));
}

.detail-stat-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.75rem;
}

.detail-stat {
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
  font-size: 1.08rem;
  font-weight: 600;
  color: rgb(var(--v-theme-on-surface));
}
</style>

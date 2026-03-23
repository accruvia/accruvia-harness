<template>
  <v-container fluid class="pa-6">
    <div class="d-flex align-center mb-8">
      <div>
        <h1 class="text-h3 font-weight-bold text-on-surface">Accruvia Harness</h1>
        <p class="text-body-2 text-on-surface-variant mt-1">
          System state: <v-chip color="success" size="x-small" label>NOMINAL</v-chip>
        </p>
      </div>
      <v-spacer />
      <v-chip variant="outlined" size="small" class="font-mono">
        {{ version?.commit?.slice(0, 7) || '...' }}
      </v-chip>
    </div>

    <!-- Metrics -->
    <v-row class="mb-8">
      <v-col v-for="stat in metrics" :key="stat.label" cols="3">
        <v-card color="surface-light" class="pa-5">
          <div class="text-caption text-uppercase text-on-surface-variant tracking-wide">{{ stat.label }}</div>
          <div class="text-h3 font-weight-bold mt-1">{{ stat.value }}</div>
        </v-card>
      </v-col>
    </v-row>

    <!-- Project Cards -->
    <h2 class="text-h6 text-uppercase text-on-surface-variant mb-4 tracking-wide">Portfolio Overview</h2>
    <v-row>
      <v-col v-for="project in projects" :key="project.project.id" cols="12" md="6" lg="4">
        <v-card
          color="surface-light"
          class="pa-5 cursor-pointer"
          :to="{ name: 'project', params: { projectId: project.project.id } }"
        >
          <div class="d-flex align-center mb-3">
            <h3 class="text-subtitle-1 font-weight-bold text-on-surface">{{ project.project.name }}</h3>
            <v-spacer />
            <v-chip
              :color="project.metrics.tasks_by_status.failed > 0 ? 'error' : 'success'"
              size="x-small"
              label
            >
              {{ project.metrics.tasks_by_status.failed > 0 ? 'ISSUES' : 'HEALTHY' }}
            </v-chip>
          </div>
          <p class="text-body-2 text-on-surface-variant mb-4">{{ project.project.description }}</p>
          <div class="d-flex ga-4 text-caption text-on-surface-variant">
            <span>{{ project.metrics.tasks_by_status.completed || 0 }} done</span>
            <span>{{ project.metrics.tasks_by_status.active || 0 }} active</span>
            <span>{{ project.metrics.tasks_by_status.pending || 0 }} pending</span>
            <span v-if="project.metrics.tasks_by_status.failed" class="text-error">
              {{ project.metrics.tasks_by_status.failed }} failed
            </span>
          </div>
        </v-card>
      </v-col>
    </v-row>
  </v-container>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useApi } from '../composables/useApi'

const { data: version, fetch: fetchVersion } = useApi<any>('/api/version')
const { data: harness, fetch: fetchHarness } = useApi<any>('/api/harness')

const projects = computed(() => harness.value?.projects || [])
const metrics = computed(() => {
  const p = harness.value?.projects || []
  const totals = { completed: 0, active: 0, pending: 0, failed: 0 }
  for (const proj of p) {
    const s = proj.metrics?.tasks_by_status || {}
    totals.completed += s.completed || 0
    totals.active += s.active || 0
    totals.pending += s.pending || 0
    totals.failed += s.failed || 0
  }
  return [
    { label: 'Total Projects', value: String(p.length).padStart(2, '0') },
    { label: 'Active Tasks', value: String(totals.active).padStart(2, '0') },
    { label: 'Completed', value: String(totals.completed) },
    { label: 'Failed', value: String(totals.failed).padStart(2, '0') },
  ]
})

onMounted(() => {
  fetchVersion()
  fetchHarness()
})
</script>

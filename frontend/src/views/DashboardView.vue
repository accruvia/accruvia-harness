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
      <v-col v-for="stat in metrics" :key="stat.label" cols="12" sm="6" lg="3">
        <v-card color="surface-light" class="pa-5">
          <div class="text-caption text-uppercase text-on-surface-variant tracking-wide">{{ stat.label }}</div>
          <div class="text-h3 font-weight-bold mt-1">{{ stat.value }}</div>
        </v-card>
      </v-col>
    </v-row>

    <!-- Project Cards -->
    <h2 class="text-h6 text-uppercase text-on-surface-variant mb-4 tracking-wide">Portfolio Overview</h2>
    <v-row>
      <v-col v-for="project in projects" :key="project.id" cols="12" md="6" lg="4">
        <v-card
          color="surface-light"
          class="pa-5 cursor-pointer"
          :to="{ name: 'project', params: { projectId: project.id } }"
        >
          <div class="d-flex align-center mb-3">
            <h3 class="text-subtitle-1 font-weight-bold text-on-surface">{{ project.name }}</h3>
            <v-spacer />
            <v-chip
              :color="taskStatus(project).failed > 0 ? 'error' : 'success'"
              size="x-small"
              label
            >
              {{ taskStatus(project).failed > 0 ? 'ISSUES' : 'HEALTHY' }}
            </v-chip>
          </div>
          <p class="text-body-2 text-on-surface-variant mb-4">
            {{ project.description || 'No description provided.' }}
          </p>
          <div class="d-flex ga-4 text-caption text-on-surface-variant">
            <span>{{ taskStatus(project).completed }} done</span>
            <span>{{ taskStatus(project).active }} active</span>
            <span>{{ taskStatus(project).pending }} pending</span>
            <span v-if="taskStatus(project).failed" class="text-error">
              {{ taskStatus(project).failed }} failed
            </span>
          </div>
        </v-card>
      </v-col>
      <v-col v-if="loading && !projects.length" cols="12" md="6" lg="4">
        <v-card color="surface-light" class="pa-5">
          <h3 class="text-subtitle-1 font-weight-bold text-on-surface">Loading projects...</h3>
          <p class="text-body-2 text-on-surface-variant mb-4">Fetching harness summary in the background.</p>
          <div class="d-flex ga-4 text-caption text-on-surface-variant">
            <span>-- done</span>
            <span>-- active</span>
            <span>-- pending</span>
          </div>
        </v-card>
      </v-col>
    </v-row>
  </v-container>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useApi } from '../composables/useApi'

const { data: version, fetch: fetchVersion } = useApi<any>('/api/version')
const { data: harness, fetch: fetchHarness } = useApi<any>('/api/harness')
const { data: projectList, fetch: fetchProjectList } = useApi<any>('/api/projects')
const loading = ref(true)
const cachedHarness = ref<any | null>(null)

const HARNESS_CACHE_KEY = 'accruvia.dashboard.harness'

const projects = computed(() => {
  const source = harness.value?.projects || cachedHarness.value?.projects || projectList.value?.projects || []
  return source.filter((project: any) => project?.id && project?.name)
})

function taskStatus(project: any) {
  const status = project?.tasks_by_status || {}
  return {
    completed: status.completed || 0,
    active: status.active || 0,
    pending: status.pending || 0,
    failed: status.failed || 0,
  }
}

const metrics = computed(() => {
  const source = harness.value || cachedHarness.value
  const counts = source?.global_counts || {}
  const activeObjectives = source?.active_objectives || []
  return [
    { label: 'Total Projects', value: String(projects.value.length).padStart(2, '0') },
    { label: 'Active Objectives', value: source ? String(activeObjectives.length).padStart(2, '0') : '--' },
    { label: 'Completed', value: source ? String(counts.completed || 0) : '--' },
    { label: 'Failed', value: source ? String(counts.failed || 0).padStart(2, '0') : '--' },
  ]
})

async function refreshHarness() {
  const payload = await fetchHarness()
  if (payload) {
    cachedHarness.value = payload
    globalThis.localStorage.setItem(HARNESS_CACHE_KEY, JSON.stringify(payload))
  }
  loading.value = false
}

onMounted(() => {
  const cached = globalThis.localStorage.getItem(HARNESS_CACHE_KEY)
  if (cached) {
    try {
      cachedHarness.value = JSON.parse(cached)
    } catch {
      globalThis.localStorage.removeItem(HARNESS_CACHE_KEY)
    }
  }
  void fetchVersion()
  void fetchProjectList()
  void refreshHarness()
})
</script>

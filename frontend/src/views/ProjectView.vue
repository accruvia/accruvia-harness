<template>
  <v-container fluid class="pa-6">
    <div class="d-flex align-center mb-6">
      <v-btn icon="$arrowLeft" variant="text" size="small" :to="{ name: 'dashboard' }" />
      <div class="ml-3">
        <div class="text-caption text-on-surface-variant text-uppercase">Active Project</div>
        <h1 class="text-h4 font-weight-bold text-on-surface">{{ projectName }}</h1>
      </div>
      <v-spacer />
      <v-btn
        v-if="!supervisor?.supervisor?.state || supervisor.supervisor.state !== 'running'"
        color="primary"
        prepend-icon="$play"
        @click="startSupervisor"
      >Start Supervisor</v-btn>
      <v-chip v-else color="success" prepend-icon="$checkCircle" label>
        Supervisor Running
      </v-chip>
    </div>

    <ProjectSectionNav :project-id="props.projectId" />

    <v-row class="mt-2">
      <!-- Objectives Board -->
      <v-col cols="12" lg="8">
        <div class="d-flex align-center justify-space-between flex-wrap ga-3 mb-4">
          <h2 class="text-subtitle-2 text-uppercase text-on-surface-variant tracking-wide">
            Objectives Tracking Board
          </h2>
          <label class="sort-control">
            <span class="text-caption text-uppercase text-on-surface-variant">Sort By</span>
            <select v-model="sortMode">
              <option value="active-first">Active First</option>
              <option value="resolved-first">Resolved First</option>
              <option value="title">Title</option>
            </select>
          </label>
        </div>
        <div class="d-flex flex-column ga-3">
          <v-card v-if="loadingSummary && !sortedObjectives.length" color="surface-light" class="pa-4">
            <h3 class="text-subtitle-1 font-weight-medium text-on-surface">Loading objectives...</h3>
            <p class="text-body-2 text-on-surface-variant mt-2">
              Rendering project summary first, then loading objective detail in the background.
            </p>
          </v-card>
          <v-card
            v-for="obj in sortedObjectives"
            :key="obj.id"
            color="surface-light"
            class="pa-4"
          >
            <div class="d-flex align-center mb-2">
              <v-chip :color="statusColor(obj.status)" size="x-small" label class="mr-3 text-uppercase font-mono">
                {{ obj.status }}
              </v-chip>
              <h3 class="text-subtitle-1 font-weight-medium text-on-surface">{{ obj.title }}</h3>
            </div>

            <!-- Gate Checks -->
            <div v-if="objectivesLoaded" class="d-flex ga-2 flex-wrap mb-2">
              <v-chip
                v-for="check in gateChecks(obj)"
                :key="check.key"
                :color="check.ok ? 'success' : 'surface-variant'"
                size="x-small"
                variant="tonal"
                :prepend-icon="check.ok ? '$check' : '$clockOutline'"
              >
                {{ check.label }}
              </v-chip>
            </div>

            <!-- Review Verdict Pills -->
            <div v-if="objectivesLoaded && reviewPackets(obj).length" class="d-flex ga-1 flex-wrap">
              <v-chip
                v-for="pkt in reviewPackets(obj)"
                :key="pkt.dimension"
                :color="pkt.verdict === 'pass' ? 'success' : pkt.verdict === 'concern' ? 'warning' : 'error'"
                size="x-small"
                variant="tonal"
              >
                {{ dimensionLabel(pkt.dimension) }}: {{ pkt.verdict }}
              </v-chip>
            </div>

            <div class="d-flex flex-wrap ga-2 mt-4">
              <v-btn
                size="small"
                variant="tonal"
                prepend-icon="$bookOpenVariant"
                :to="{ name: 'objective', params: { projectId: props.projectId, objectiveId: obj.id } }"
              >
                Overview
              </v-btn>
              <v-btn
                size="small"
                variant="tonal"
                prepend-icon="$sourceBranch"
                :to="{ name: 'objective-atomic', params: { projectId: props.projectId, objectiveId: obj.id } }"
              >
                Atomicity
              </v-btn>
              <v-btn
                size="small"
                variant="tonal"
                prepend-icon="$rocketLaunch"
                :to="{ name: 'objective-promotion', params: { projectId: props.projectId, objectiveId: obj.id } }"
              >
                Promotion
              </v-btn>
            </div>
          </v-card>
        </div>
      </v-col>

      <!-- Right Panel -->
      <v-col cols="12" lg="4">
        <!-- Supervisor Status -->
        <v-card color="surface-light" class="pa-4 mb-4">
          <div class="text-caption text-uppercase text-on-surface-variant mb-2">Supervisor</div>
          <div class="d-flex align-center">
            <v-icon
              :color="supervisor?.supervisor?.state === 'running' ? 'success' : 'on-surface-variant'"
              size="small"
              class="mr-2"
            >
              {{ supervisor?.supervisor?.state === 'running' ? '$circle' : '$circleOutline' }}
            </v-icon>
            <span class="text-body-2">{{ supervisor?.supervisor?.state || 'stopped' }}</span>
          </div>
          <div v-if="supervisor?.supervisor?.processed_count" class="text-caption text-on-surface-variant mt-1">
            {{ supervisor.supervisor.processed_count }} tasks processed
          </div>
        </v-card>

        <!-- Live Activity -->
        <v-card color="surface-light" class="pa-4">
          <div class="d-flex align-center mb-3">
            <div class="text-caption text-uppercase text-on-surface-variant">Live Activity</div>
            <v-spacer />
            <v-chip color="success" size="x-small" variant="tonal" prepend-icon="$accessPoint">LIVE</v-chip>
          </div>
          <div class="d-flex flex-column ga-2">
            <div v-for="task in recentTasks" :key="task.id" class="d-flex align-center">
              <v-icon
                :color="task.status === 'completed' ? 'success' : task.status === 'active' ? 'info' : task.status === 'failed' ? 'error' : 'on-surface-variant'"
                size="x-small"
                class="mr-2"
              >$circle</v-icon>
              <span class="text-body-2 text-truncate">{{ task.title }}</span>
            </div>
          </div>
        </v-card>
      </v-col>
    </v-row>
  </v-container>
</template>

<script setup lang="ts">
import { computed, onActivated, onDeactivated, ref } from 'vue'
import { useApi, post, useSSE } from '../composables/useApi'
import ProjectSectionNav from '../components/ProjectSectionNav.vue'

const props = defineProps<{ projectId: string }>()

const { data: summary, fetch: fetchSummary } = useApi<any>(`/api/projects/${props.projectId}/summary`)
const { data: objectivesDetail, fetch: fetchObjectivesDetail } = useApi<any>(`/api/projects/${props.projectId}/objectives`)
const { data: supervisor, fetch: fetchSupervisor } = useApi<any>(`/api/projects/${props.projectId}/supervisor`)
const loadingSummary = ref(true)
const objectivesLoaded = ref(false)
const sortMode = ref<'active-first' | 'resolved-first' | 'title'>('active-first')

const projectName = computed(() => {
  return objectivesDetail.value?.project?.name || summary.value?.project?.name || '...'
})

const objectives = computed(() => objectivesDetail.value?.objectives || summary.value?.objectives || [])
const sortedObjectives = computed(() => {
  const rankActiveFirst: Record<string, number> = {
    executing: 0,
    planning: 1,
    investigating: 2,
    open: 3,
    paused: 4,
    resolved: 5,
  }
  const rankResolvedFirst: Record<string, number> = {
    resolved: 0,
    executing: 1,
    planning: 2,
    investigating: 3,
    open: 4,
    paused: 5,
  }

  return [...objectives.value].sort((left: any, right: any) => {
    if (sortMode.value === 'title') {
      return String(left.title || '').localeCompare(String(right.title || ''))
    }

    const ranks = sortMode.value === 'resolved-first' ? rankResolvedFirst : rankActiveFirst
    const statusDelta = (ranks[left.status] ?? 99) - (ranks[right.status] ?? 99)
    if (statusDelta !== 0) return statusDelta
    return String(left.title || '').localeCompare(String(right.title || ''))
  })
})

const recentTasks = computed(() => {
  const tasks = summary.value?.tasks || []
  return tasks.slice(-8).reverse()
})

function gateChecks(obj: any) {
  return (obj.execution_gate?.checks || []).filter((c: any) => !c.key?.endsWith('_placeholder'))
}

function reviewPackets(obj: any) {
  const rounds = obj.promotion_review?.review_rounds || []
  if (!rounds.length) return []
  return rounds[0].packets || []
}

function dimensionLabel(dim: string) {
  const labels: Record<string, string> = {
    intent_fidelity: 'Intent',
    unit_test_coverage: 'QA',
    integration_e2e_coverage: 'E2E',
    security: 'Security',
    devops: 'DevOps',
    atomic_fidelity: 'Atomic',
    code_structure: 'Arch',
  }
  return labels[dim] || dim
}

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

async function startSupervisor() {
  await post(`/api/projects/${props.projectId}/supervise`)
  fetchSupervisor()
}

const { connect, disconnect } = useSSE(() => {
  void fetchSummary()
  void fetchObjectivesDetail().then(() => {
    objectivesLoaded.value = true
  })
  void fetchSupervisor()
})

async function loadProjectShell() {
  await fetchSupervisor()
  await fetchSummary()
  loadingSummary.value = false
}

function loadObjectivesDeferred() {
  const run = async () => {
    await fetchObjectivesDetail()
    objectivesLoaded.value = true
  }
  if ('requestIdleCallback' in globalThis) {
    globalThis.requestIdleCallback(() => {
      void run()
    }, { timeout: 1500 })
    return
  }
  globalThis.setTimeout(() => {
    void run()
  }, 0)
}

onActivated(() => {
  void loadProjectShell()
  loadObjectivesDeferred()
  connect()
})

onDeactivated(() => disconnect())
</script>

<style scoped>
.sort-control {
  display: inline-flex;
  align-items: center;
  gap: 0.65rem;
}

.sort-control select {
  border: 1px solid rgba(125, 94, 67, 0.2);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.78);
  color: rgb(var(--v-theme-on-surface));
  padding: 0.45rem 0.85rem;
  font: inherit;
  font-size: 0.9rem;
}
</style>

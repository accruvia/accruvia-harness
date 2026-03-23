<template>
  <v-container fluid class="pa-6">
    <div class="d-flex align-center mb-6">
      <v-btn icon="mdi-arrow-left" variant="text" size="small" :to="{ name: 'dashboard' }" />
      <div class="ml-3">
        <div class="text-caption text-on-surface-variant text-uppercase">Active Project</div>
        <h1 class="text-h4 font-weight-bold text-on-surface">{{ workspace?.project?.name || '...' }}</h1>
      </div>
      <v-spacer />
      <v-btn
        v-if="!supervisor?.supervisor?.state || supervisor.supervisor.state !== 'running'"
        color="primary"
        prepend-icon="mdi-play"
        @click="startSupervisor"
      >Start Supervisor</v-btn>
      <v-chip v-else color="success" prepend-icon="mdi-check-circle" label>
        Supervisor Running
      </v-chip>
    </div>

    <v-row>
      <!-- Objectives Board -->
      <v-col cols="12" lg="8">
        <h2 class="text-subtitle-2 text-uppercase text-on-surface-variant mb-4 tracking-wide">
          Objectives Tracking Board
        </h2>
        <div class="d-flex flex-column ga-3">
          <v-card
            v-for="obj in objectives"
            :key="obj.id"
            color="surface-light"
            class="pa-4 cursor-pointer"
            :to="{ name: 'objective', params: { projectId, objectiveId: obj.id } }"
          >
            <div class="d-flex align-center mb-2">
              <v-chip :color="statusColor(obj.status)" size="x-small" label class="mr-3 text-uppercase font-mono">
                {{ obj.status }}
              </v-chip>
              <h3 class="text-subtitle-1 font-weight-medium text-on-surface">{{ obj.title }}</h3>
            </div>

            <!-- Gate Checks -->
            <div class="d-flex ga-2 flex-wrap mb-2">
              <v-chip
                v-for="check in gateChecks(obj)"
                :key="check.key"
                :color="check.ok ? 'success' : 'surface-variant'"
                size="x-small"
                variant="tonal"
                :prepend-icon="check.ok ? 'mdi-check' : 'mdi-clock-outline'"
              >
                {{ check.label }}
              </v-chip>
            </div>

            <!-- Review Verdict Pills -->
            <div v-if="reviewPackets(obj).length" class="d-flex ga-1 flex-wrap">
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
              {{ supervisor?.supervisor?.state === 'running' ? 'mdi-circle' : 'mdi-circle-outline' }}
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
            <v-chip color="success" size="x-small" variant="tonal" prepend-icon="mdi-access-point">LIVE</v-chip>
          </div>
          <div class="d-flex flex-column ga-2">
            <div v-for="task in recentTasks" :key="task.id" class="d-flex align-center">
              <v-icon
                :color="task.status === 'completed' ? 'success' : task.status === 'active' ? 'info' : task.status === 'failed' ? 'error' : 'on-surface-variant'"
                size="x-small"
                class="mr-2"
              >mdi-circle</v-icon>
              <span class="text-body-2 text-truncate">{{ task.title }}</span>
            </div>
          </div>
        </v-card>
      </v-col>
    </v-row>
  </v-container>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useApi, post, useSSE } from '../composables/useApi'

const props = defineProps<{ projectId: string }>()

const { data: workspace, fetch: fetchWorkspace } = useApi<any>(`/api/projects/${props.projectId}/workspace`)
const { data: supervisor, fetch: fetchSupervisor } = useApi<any>(`/api/projects/${props.projectId}/supervisor`)

const objectives = computed(() => workspace.value?.objectives || [])
const recentTasks = computed(() => {
  const tasks = workspace.value?.tasks || []
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
  fetchWorkspace()
  fetchSupervisor()
})

onMounted(() => {
  fetchWorkspace()
  fetchSupervisor()
  connect()
})

onUnmounted(() => disconnect())
</script>

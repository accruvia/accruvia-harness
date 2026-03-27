<template>
  <v-container fluid class="pa-6">
    <div class="d-flex align-center mb-3">
      <v-btn
        icon="$arrowLeft"
        variant="text"
        size="small"
        :to="{ name: 'project', params: { projectId: props.projectId } }"
      />
      <div class="ml-3">
        <div class="text-caption text-uppercase text-on-surface-variant">Atomicity Workspace</div>
        <h1 class="text-h4 font-weight-bold text-on-surface">{{ objective?.title || '...' }}</h1>
      </div>
    </div>

    <ObjectiveSectionNav :project-id="props.projectId" :objective-id="props.objectiveId" />

    <v-row class="mt-2">
      <v-col cols="12" lg="4">
        <v-card color="surface-light" class="pa-5 mb-4">
          <div class="text-caption text-uppercase text-on-surface-variant mb-3">Workflow</div>
          <div class="d-flex flex-wrap ga-2">
            <v-chip
              v-for="stage in workflowStages"
              :key="stage.key"
              :color="stage.current ? 'primary' : stage.ready ? 'success' : 'surface-variant'"
              variant="tonal"
              size="small"
            >
              {{ stage.label }}
            </v-chip>
          </div>
        </v-card>

        <v-card color="surface-light" class="pa-5 mb-4">
          <div class="d-flex align-center mb-3">
            <div class="text-caption text-uppercase text-on-surface-variant">Execution Gate</div>
            <v-spacer />
            <v-chip :color="blockingChecks.length ? 'warning' : 'success'" size="x-small" label>
              {{ blockingChecks.length ? `${blockingChecks.length} blocked` : 'clear' }}
            </v-chip>
          </div>
          <div v-if="blockingChecks.length" class="d-flex flex-column ga-2">
            <div
              v-for="check in blockingChecks"
              :key="check.key"
              class="gate-card"
            >
              <div class="text-body-2 font-weight-medium">{{ check.label }}</div>
              <div class="text-caption text-on-surface-variant">{{ check.detail || 'Still blocking execution.' }}</div>
            </div>
          </div>
          <div v-else class="text-body-2 text-on-surface-variant">
            This objective is past its execution gates. The remaining work is in the task board below.
          </div>
        </v-card>

        <v-card color="surface-light" class="pa-5">
          <div class="text-caption text-uppercase text-on-surface-variant mb-3">Current Shape</div>
          <div class="text-body-2 text-on-surface mb-3">
            {{ objective?.summary || intent?.intent_summary || 'No objective summary recorded yet.' }}
          </div>
          <div class="text-caption text-on-surface-variant mb-1">Diagram</div>
          <div class="text-body-2">
            {{ diagram?.summary || diagram?.status || 'No architecture workspace artifact yet.' }}
          </div>
        </v-card>
      </v-col>

      <v-col cols="12" lg="8">
        <div class="task-summary-grid mb-4">
          <v-card
            v-for="stat in taskStats"
            :key="stat.label"
            color="surface-light"
            class="pa-4"
          >
            <div class="text-caption text-uppercase text-on-surface-variant">{{ stat.label }}</div>
            <div class="text-h5 font-weight-bold mt-2">{{ stat.value }}</div>
          </v-card>
        </div>

        <v-card color="surface-light" class="pa-5">
          <div class="d-flex align-center mb-4">
            <div>
              <div class="text-caption text-uppercase text-on-surface-variant">Atomic Tasks</div>
              <h2 class="text-h6 text-on-surface mt-1">What is happening now</h2>
            </div>
            <v-spacer />
            <v-chip color="primary" variant="tonal">{{ tasks.length }} slices</v-chip>
          </div>

          <div v-if="!tasks.length" class="text-body-2 text-on-surface-variant">
            No linked tasks exist yet for this objective.
          </div>

          <div v-else class="d-flex flex-column ga-3">
            <div
              v-for="task in orderedTasks"
              :key="task.id"
              class="task-card"
            >
              <div class="d-flex align-start">
                <div class="status-rail" :class="statusClass(task.status)" />
                <div class="ml-4 flex-grow-1">
                  <div class="d-flex align-center flex-wrap ga-2 mb-2">
                    <h3 class="text-subtitle-1 font-weight-medium">{{ task.title }}</h3>
                    <v-chip :color="statusColor(task.status)" size="x-small" label>
                      {{ task.status }}
                    </v-chip>
                    <v-chip color="surface-variant" variant="tonal" size="x-small">
                      {{ task.strategy || 'unspecified strategy' }}
                    </v-chip>
                  </div>
                  <div class="text-caption text-on-surface-variant">
                    Updated {{ formatTimestamp(task.updated_at) }}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </v-card>
      </v-col>
    </v-row>
  </v-container>
</template>

<script setup lang="ts">
import { computed, onActivated, onDeactivated } from 'vue'
import { useApi, useSSE } from '../composables/useApi'
import ObjectiveSectionNav from '../components/ObjectiveSectionNav.vue'

const props = defineProps<{ projectId: string; objectiveId: string }>()

const { data: summary, fetch: fetchSummary } = useApi<any>(`/api/projects/${props.projectId}/summary`)
const { data: detail, fetch: fetchDetail } = useApi<any>(`/api/projects/${props.projectId}/objectives/${props.objectiveId}`)

const objective = computed(() => {
  return detail.value?.objective
    || (summary.value?.objectives || []).find((item: any) => item.id === props.objectiveId)
})

const tasks = computed(() => {
  return detail.value?.tasks
    || (summary.value?.tasks || []).filter((item: any) => item.objective_id === props.objectiveId)
})

const orderedTasks = computed(() => {
  const rank: Record<string, number> = { active: 0, pending: 1, failed: 2, completed: 3 }
  return [...tasks.value].sort((left: any, right: any) => {
    const statusDelta = (rank[left.status] ?? 9) - (rank[right.status] ?? 9)
    if (statusDelta !== 0) return statusDelta
    return String(right.updated_at || '').localeCompare(String(left.updated_at || ''))
  })
})

const workflowStages = computed(() => {
  const workflow = objective.value?.workflow || {}
  const current = workflow.current_stage
  return [
    { key: 'planning', label: 'Planning', ready: !!workflow.planning?.ready, current: current === 'planning' },
    { key: 'execution', label: 'Execution', ready: !!workflow.execution?.ready, current: current === 'execution' },
    { key: 'review', label: 'Review', ready: !!workflow.review?.ready, current: current === 'review' },
    { key: 'promotion', label: 'Promotion', ready: !!workflow.promotion?.ready, current: current === 'promotion' },
  ]
})

const blockingChecks = computed(() => {
  return (objective.value?.execution_gate?.checks || []).filter(
    (check: any) => !check.ok && !String(check.key || '').endsWith('_placeholder'),
  )
})

const intent = computed(() => objective.value?.intent_model)
const diagram = computed(() => objective.value?.diagram)

const taskStats = computed(() => {
  const counts = { active: 0, pending: 0, failed: 0, completed: 0 }
  for (const task of tasks.value) {
    if (task.status in counts) {
      counts[task.status as keyof typeof counts] += 1
    }
  }
  return [
    { label: 'Active', value: counts.active },
    { label: 'Pending', value: counts.pending },
    { label: 'Failed', value: counts.failed },
    { label: 'Completed', value: counts.completed },
  ]
})

function statusColor(status: string) {
  const colors: Record<string, string> = {
    completed: 'success',
    active: 'info',
    failed: 'error',
    pending: 'warning',
  }
  return colors[status] || 'surface-variant'
}

function statusClass(status: string) {
  return `status-${status || 'unknown'}`
}

function formatTimestamp(value: string) {
  if (!value) return 'recently'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'recently'
  return date.toLocaleString()
}

const { connect, disconnect } = useSSE(() => {
  void fetchSummary()
  void fetchDetail()
})

onActivated(() => {
  void fetchSummary()
  void fetchDetail()
  connect()
})

onDeactivated(() => disconnect())
</script>

<style scoped>
.task-summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 1rem;
}

.gate-card,
.task-card {
  border: 1px solid rgba(125, 94, 67, 0.14);
  border-radius: 16px;
  background: rgba(255, 251, 245, 0.76);
  padding: 1rem;
}

.status-rail {
  width: 6px;
  min-height: 58px;
  border-radius: 999px;
  background: rgba(125, 94, 67, 0.18);
}

.status-rail.status-active {
  background: rgb(var(--v-theme-info));
}

.status-rail.status-pending {
  background: rgb(var(--v-theme-warning));
}

.status-rail.status-failed {
  background: rgb(var(--v-theme-error));
}

.status-rail.status-completed {
  background: rgb(var(--v-theme-success));
}
</style>

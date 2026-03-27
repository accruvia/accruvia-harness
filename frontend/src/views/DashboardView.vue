<template>
  <v-container fluid class="pa-6">
    <div class="d-flex align-center mb-8">
      <div>
        <div class="text-caption text-uppercase text-on-surface-variant">Harness Command</div>
        <h1 class="text-h3 font-weight-bold text-on-surface">Operator Dashboard</h1>
        <p class="text-body-2 text-on-surface-variant mt-1">
          {{ operatorSummary }}
        </p>
      </div>
      <v-spacer />
      <v-chip :color="systemTone" variant="tonal" size="small">
        {{ systemLabel }}
      </v-chip>
      <v-chip variant="outlined" size="small" class="font-mono ml-2">
        {{ version?.commit?.slice(0, 7) || '...' }}
      </v-chip>
    </div>

    <HarnessSectionNav class="mb-6" />

    <v-row class="mb-8">
      <v-col v-for="stat in metrics" :key="stat.label" cols="12" sm="6" lg="3">
        <v-card
          color="surface-light"
          class="pa-5 metric-card"
          :class="{ 'metric-card-active': activeLens === stat.key }"
          @click="activeLens = stat.key"
        >
          <div class="text-caption text-uppercase text-on-surface-variant tracking-wide">{{ stat.label }}</div>
          <div class="text-h3 font-weight-bold mt-1">{{ stat.value }}</div>
          <div class="text-caption text-on-surface-variant mt-2">{{ stat.detail }}</div>
        </v-card>
      </v-col>
    </v-row>

    <v-row class="mb-8">
      <v-col cols="12" lg="7">
        <div class="d-flex align-center mb-4">
          <div>
            <h2 class="text-h6 text-uppercase text-on-surface-variant tracking-wide">{{ activeLensMeta.title }}</h2>
            <div class="text-body-2 text-on-surface-variant mt-1">{{ activeLensMeta.detail }}</div>
          </div>
          <v-spacer />
          <v-chip size="x-small" variant="tonal">{{ spotlightItems.length }} items</v-chip>
        </div>
        <div class="d-flex flex-column ga-3">
          <v-card v-if="!spotlightItems.length" color="surface-light" class="pa-5">
            <div class="text-subtitle-1 font-weight-medium text-on-surface">No urgent operator actions</div>
            <div class="text-body-2 text-on-surface-variant mt-2">
              {{ activeLensMeta.empty }}
            </div>
          </v-card>
          <v-card
            v-for="item in spotlightItems"
            :key="`${item.project_id}-${item.id}`"
            color="surface-light"
            class="pa-5"
            :to="{ name: 'objective', params: { projectId: item.project_id, objectiveId: item.id } }"
          >
            <div class="d-flex align-start justify-space-between ga-3">
              <div>
                <div class="text-caption text-uppercase text-on-surface-variant">{{ item.project_name }}</div>
                <h3 class="text-subtitle-1 font-weight-bold text-on-surface mt-1">{{ item.title }}</h3>
              </div>
              <v-chip :color="item.tone" size="x-small" label>{{ item.label }}</v-chip>
            </div>
            <div class="text-body-2 text-on-surface mt-3">{{ item.detail }}</div>
            <div class="d-flex ga-4 flex-wrap text-caption text-on-surface-variant mt-3">
              <span>{{ item.status_label }}</span>
              <span>{{ item.task_counts.active || 0 }} active</span>
              <span>{{ item.task_counts.pending || 0 }} pending</span>
              <span>{{ item.task_counts.failed || 0 }} failed</span>
              <span>{{ item.task_counts.completed || 0 }} completed</span>
            </div>
          </v-card>
        </div>
      </v-col>

      <v-col cols="12" lg="5">
        <div class="d-flex align-center mb-4">
          <h2 class="text-h6 text-uppercase text-on-surface-variant tracking-wide">Recent Signals</h2>
        </div>
        <v-card color="surface-light" class="pa-5">
          <div class="d-flex flex-column ga-3">
            <div v-for="event in recentSignals" :key="`${event.created_at}-${event.text}`" class="signal-row">
              <v-icon :color="event.tone" size="x-small" class="mt-1">$circle</v-icon>
              <div class="ml-3 flex-grow-1 min-w-0">
                <div class="text-body-2 text-on-surface">{{ event.text }}</div>
                <div class="text-caption text-on-surface-variant">
                  {{ event.project_name }}<span v-if="event.objective_title"> · {{ event.objective_title }}</span>
                  · {{ formatTimestamp(event.created_at) }}
                </div>
              </div>
            </div>
          </div>
        </v-card>
      </v-col>
    </v-row>

    <div class="d-flex align-center mb-4">
      <h2 class="text-h6 text-uppercase text-on-surface-variant tracking-wide">Project State</h2>
    </div>
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
            <v-chip :color="projectState(project).tone" size="x-small" label>
              {{ projectState(project).label }}
            </v-chip>
          </div>
          <div class="text-body-2 text-on-surface-variant mb-4">
            {{ projectState(project).detail }}
          </div>
          <div class="d-flex ga-4 text-caption text-on-surface-variant flex-wrap">
            <span>{{ unresolvedCount(project) }} unresolved objectives</span>
            <span>{{ taskStatus(project).active }} active</span>
            <span>{{ taskStatus(project).pending }} pending</span>
            <span v-if="taskStatus(project).failed" class="text-error">{{ taskStatus(project).failed }} failed</span>
          </div>
        </v-card>
      </v-col>
      <v-col v-if="loading && !projects.length" cols="12" md="6" lg="4">
        <v-card color="surface-light" class="pa-5">
          <h3 class="text-subtitle-1 font-weight-bold text-on-surface">Loading harness state...</h3>
          <p class="text-body-2 text-on-surface-variant mt-2">Fetching the live operator queue and recent signals.</p>
        </v-card>
      </v-col>
    </v-row>
  </v-container>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useApi } from '../composables/useApi'
import HarnessSectionNav from '../components/HarnessSectionNav.vue'

const { data: version, fetch: fetchVersion } = useApi<any>('/api/version')
const { data: harness, fetch: fetchHarness } = useApi<any>('/api/harness')
const { data: projectList, fetch: fetchProjectList } = useApi<any>('/api/projects')
const loading = ref(true)
const cachedHarness = ref<any | null>(null)
const activeLens = ref<'attention' | 'running' | 'unresolved' | 'failures'>('attention')

const HARNESS_CACHE_KEY = 'accruvia.dashboard.harness'

const harnessSource = computed(() => harness.value || cachedHarness.value || {})

const projects = computed(() => {
  const source = harnessSource.value?.projects || projectList.value?.projects || []
  return source.filter((project: any) => project?.id && project?.name)
})

const objectivesById = computed(() => {
  const entries = projects.value.flatMap((project: any) =>
    (project.objectives || []).map((objective: any) => [
      objective.id,
      { ...objective, project_name: objective.project_name || project.name, project_id: objective.project_id || project.id },
    ]),
  )
  return new Map(entries)
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

function unresolvedCount(project: any) {
  return (project.objectives || []).filter((objective: any) => objective.status !== 'resolved').length
}

const attentionItems = computed(() => {
  const items = projects.value.flatMap((project: any) =>
    (project.objectives || []).map((objective: any) => {
      const taskCounts = objective.task_counts || {}
      const active = Number(taskCounts.active || 0)
      const pending = Number(taskCounts.pending || 0)
      const failed = Number(taskCounts.failed || 0)
      const unresolvedFailed = Number(objective.unresolved_failed_count || 0)
      const unresolved = objective.status !== 'resolved'

      if (active > 0) {
        return {
          ...objective,
          tone: 'info',
          label: active === 1 ? 'Running now' : `${active} tasks running`,
          detail: 'The harness is actively executing work here.',
          priority: 0,
          status_label: 'Execution in progress',
        }
      }
      if (pending > 0) {
        return {
          ...objective,
          tone: 'warning',
          label: pending === 1 ? 'Queued work' : `${pending} queued`,
          detail: 'Tasks exist for this objective but are waiting to run or clear a gate.',
          priority: 1,
          status_label: 'Work is queued',
        }
      }
      if (objective.status === 'paused') {
        return {
          ...objective,
          tone: 'warning',
          label: 'Paused objective',
          detail: unresolvedFailed > 0
            ? 'This objective is paused and still has blocking failed tasks that need a disposition or retry decision.'
            : 'This objective is paused. Decide whether to resume it or leave it parked.',
          priority: 2,
          status_label: 'Paused by workflow state',
        }
      }
      if (objective.status === 'investigating') {
        return {
          ...objective,
          tone: 'warning',
          label: 'Needs planning input',
          detail: 'This objective has been opened for investigation but no atomic work has started yet.',
          priority: 3,
          status_label: 'Still in planning',
        }
      }
      if (objective.status === 'open') {
        return {
          ...objective,
          tone: 'surface-variant',
          label: 'Open, untouched',
          detail: 'This objective exists but the harness has not started planning or execution work.',
          priority: 4,
          status_label: 'Not yet started',
        }
      }
      if (unresolvedFailed > 0 && unresolved) {
        return {
          ...objective,
          tone: 'error',
          label: unresolvedFailed === 1 ? '1 blocking failed task' : `${unresolvedFailed} blocking failed tasks`,
          detail: 'This objective cannot advance until failed work is retried or explicitly dispositioned.',
          priority: 5,
          status_label: 'Failed work present',
        }
      }
      return null
    }),
  ).filter(Boolean) as any[]

  return items.sort((left, right) => {
    const priorityDelta = left.priority - right.priority
    if (priorityDelta !== 0) return priorityDelta
    return String(left.title || '').localeCompare(String(right.title || ''))
  })
})

const unresolvedItems = computed(() => {
  return projects.value.flatMap((project: any) =>
    (project.objectives || [])
      .filter((objective: any) => objective.status !== 'resolved')
      .map((objective: any) => {
        const counts = objective.task_counts || {}
        const unresolvedFailed = Number(objective.unresolved_failed_count || 0)
        let label = 'Unresolved'
        let tone = 'warning'
        let detail = 'This objective has not yet reached a resolved state.'
        if (objective.status === 'paused') {
          label = 'Paused'
          detail = 'This objective is paused and may need an operator decision to resume or close it.'
        } else if (objective.status === 'investigating') {
          label = 'Planning'
          detail = 'This objective is still in planning and needs the harness to complete upstream prep work.'
        } else if (objective.status === 'open') {
          label = 'Open'
          tone = 'secondary'
          detail = 'This objective exists but has not started meaningful harness work yet.'
        } else if ((counts.active || 0) > 0) {
          label = 'Running'
          tone = 'info'
          detail = 'The harness is actively executing work for this unresolved objective.'
        } else if (unresolvedFailed > 0) {
          label = 'Blocked by failed work'
          tone = 'error'
          detail = 'This unresolved objective still has blocking failed tasks that need a retry or explicit disposition.'
        }
        return {
          ...objective,
          project_name: objective.project_name || project.name,
          tone,
          label,
          detail,
          status_label: `Status: ${objective.status}`,
        }
      }),
  ).sort((left: any, right: any) => String(left.title || '').localeCompare(String(right.title || '')))
})

const runningItems = computed(() => {
  return projects.value.flatMap((project: any) =>
    (project.objectives || [])
      .filter((objective: any) => Number(objective.task_counts?.active || 0) > 0)
      .map((objective: any) => ({
        ...objective,
        tone: 'info',
        label: `${objective.task_counts.active} active`,
        detail: 'The harness is actively executing tasks for this objective.',
        status_label: `Status: ${objective.status}`,
      })),
  )
})

const failureItems = computed(() => {
  return projects.value.flatMap((project: any) =>
    (project.objectives || [])
      .filter((objective: any) => Number(objective.task_counts?.failed || 0) > 0)
      .map((objective: any) => ({
        ...objective,
        tone: Number(objective.unresolved_failed_count || 0) > 0 ? 'error' : 'surface-variant',
        label: Number(objective.unresolved_failed_count || 0) > 0
          ? (Number(objective.unresolved_failed_count) === 1 ? '1 blocking failed task' : `${objective.unresolved_failed_count} blocking failed tasks`)
          : (objective.task_counts.failed === 1 ? '1 historical failed task' : `${objective.task_counts.failed} historical failed tasks`),
        detail: Number(objective.unresolved_failed_count || 0) > 0
          ? 'These failures are still blocking workflow and need a retry or explicit disposition.'
          : 'These failures are retained as history, but they are not currently blocking progress.',
        status_label: `Status: ${objective.status}`,
      })),
  ).sort((left: any, right: any) => Number(right.task_counts?.failed || 0) - Number(left.task_counts?.failed || 0))
})

const spotlightItems = computed(() => {
  if (activeLens.value === 'running') return runningItems.value
  if (activeLens.value === 'unresolved') return unresolvedItems.value
  if (activeLens.value === 'failures') return failureItems.value
  return attentionItems.value
})

const activeLensMeta = computed(() => {
  if (activeLens.value === 'running') {
    return {
      title: 'Running Now',
      detail: 'Work the harness is actively executing at this moment.',
      empty: 'No objectives are actively executing right now.',
    }
  }
  if (activeLens.value === 'unresolved') {
    return {
      title: 'Unresolved Objectives',
      detail: 'Everything that has not yet reached a resolved state.',
      empty: 'There are no unresolved objectives in the harness right now.',
    }
  }
  if (activeLens.value === 'failures') {
    return {
      title: 'Failed Task History',
      detail: 'Objectives with failed task outcomes that may need review or remediation.',
      empty: 'No failed task history is currently visible in the harness.',
    }
  }
  return {
    title: 'Attention Now',
    detail: 'The objectives most likely to need operator direction next.',
    empty: 'The harness is not actively executing work and there are no queued or blocked tasks requiring intervention.',
  }
})

const recentSignals = computed(() => {
  const events = harnessSource.value?.recent_events || []
  return events.slice(0, 12).map((event: any) => {
    const objective = objectivesById.value.get(event.objective_id)
    return {
      ...event,
      objective_title: objective?.title || '',
      tone:
        event.event_type === 'task_failed'
          ? 'error'
          : event.event_type === 'task_completed'
            ? 'success'
            : event.event_type === 'task_active'
              ? 'info'
              : 'warning',
    }
  })
})

const metrics = computed(() => {
  const counts = harnessSource.value?.global_counts || {}
  const unresolvedObjectives = projects.value.reduce((sum: number, project: any) => sum + unresolvedCount(project), 0)
  const runningProjects = projects.value.filter((project: any) => taskStatus(project).active > 0).length
  const failedTasks = Number(counts.failed || 0)
  return [
    {
      key: 'attention',
      label: 'Needs Attention',
      value: String(attentionItems.value.length).padStart(2, '0'),
      detail: 'Objectives that are paused, blocked, queued, or otherwise need operator focus.',
    },
    {
      key: 'running',
      label: 'Running Projects',
      value: String(runningProjects).padStart(2, '0'),
      detail: 'Projects with active harness execution happening right now.',
    },
    {
      key: 'unresolved',
      label: 'Unresolved Objectives',
      value: String(unresolvedObjectives).padStart(2, '0'),
      detail: 'Objectives that are not yet fully resolved.',
    },
    {
      key: 'failures',
      label: 'Failed Task History',
      value: String(failedTasks).padStart(2, '0'),
      detail: 'Total failed task outcomes recorded across the harness.',
    },
  ]
})

const systemTone = computed(() => {
  if (attentionItems.value.length > 0) return 'warning'
  if ((harnessSource.value?.global_counts?.active || 0) > 0) return 'info'
  return 'success'
})

const systemLabel = computed(() => {
  if (attentionItems.value.length > 0) return 'Operator attention needed'
  if ((harnessSource.value?.global_counts?.active || 0) > 0) return 'Execution active'
  return 'Stable and idle'
})

const operatorSummary = computed(() => {
  if (attentionItems.value.length === 0) {
    return 'No urgent operator interventions are visible. The harness is currently quiet.'
  }
  const top = attentionItems.value[0]
  return `${attentionItems.value.length} items need attention. Start with ${top.title} in ${top.project_name}.`
})

function projectState(project: any) {
  const tasks = taskStatus(project)
  const unresolved = unresolvedCount(project)
  if (tasks.active > 0) {
    return { tone: 'info', label: 'Running', detail: 'The harness is actively executing work in this project.' }
  }
  if (tasks.pending > 0) {
    return { tone: 'warning', label: 'Queued', detail: 'This project has pending work waiting for execution or review.' }
  }
  if (unresolved > 0) {
    return { tone: 'warning', label: 'Needs attention', detail: `${unresolved} unresolved objectives remain even though no work is currently running.` }
  }
  if (tasks.failed > 0) {
    return { tone: 'surface-variant', label: 'History to review', detail: 'Execution is complete, but the project contains failed task history.' }
  }
  return { tone: 'success', label: 'Quiet', detail: 'No active or queued work is present for this project.' }
}

function formatTimestamp(value: string) {
  if (!value) return 'recently'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'recently'
  const diffMinutes = Math.floor((Date.now() - date.getTime()) / 60000)
  if (diffMinutes < 1) return 'just now'
  if (diffMinutes < 60) return `${diffMinutes} min ago`
  const diffHours = Math.floor(diffMinutes / 60)
  if (diffHours < 24) return `${diffHours} hr ago`
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

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

<style scoped>
.signal-row {
  display: flex;
  align-items: flex-start;
}

.metric-card {
  cursor: pointer;
  transition: border-color 160ms ease, background-color 160ms ease, transform 160ms ease;
  border: 1px solid transparent;
}

.metric-card:hover {
  transform: translateY(-1px);
  border-color: rgba(125, 94, 67, 0.18);
}

.metric-card-active {
  border-color: rgba(179, 92, 46, 0.38);
  background: rgba(255, 247, 238, 0.92);
}
</style>

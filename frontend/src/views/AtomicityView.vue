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
        <div class="page-kicker">Atomicity workspace</div>
        <h1 class="page-title">{{ objective?.title || '...' }}</h1>
      </div>
    </div>

    <ObjectiveSectionNav :project-id="props.projectId" :objective-id="props.objectiveId" />

    <v-row class="mt-2">
      <v-col cols="12" lg="4">
        <div class="column-kicker mb-3">Objective state</div>

        <v-card color="surface-light" class="pa-5 mb-4">
          <div class="section-title mb-3">Workflow</div>
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
            <div class="section-title">Execution gate</div>
            <v-spacer />
            <v-chip :color="blockingChecks.length ? 'warning' : 'success'" size="x-small" variant="tonal">
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
          <div class="section-title mb-3">Current shape</div>
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
        <div class="column-kicker mb-3">Atomic task queue</div>

        <div class="task-summary-grid mb-4">
          <v-card
            v-for="stat in taskStats"
            :key="stat.key"
            color="surface-light"
            class="pa-4 stat-card"
            :class="{ active: selectedTaskFilter === stat.key }"
            @click="toggleTaskFilter(stat.key)"
          >
            <div class="stat-label">{{ stat.label }}</div>
            <div class="stat-value mt-2">{{ stat.value }}</div>
            <div class="stat-helper mt-2">
              {{ selectedTaskFilter === stat.key ? 'Showing only this status' : `Show only ${stat.label.toLowerCase()} tasks` }}
            </div>
          </v-card>
        </div>

        <v-card v-if="blockingFailedTasks.length" color="surface-light" class="pa-5 mb-4">
          <div class="d-flex align-center mb-4">
            <div>
              <div class="section-title">Blocking failed tasks</div>
              <h2 class="panel-title mt-1">What you need to do here</h2>
            </div>
            <v-spacer />
            <v-chip color="error" variant="tonal">{{ blockingFailedTasks.length }} blocking</v-chip>
          </div>

          <div class="text-body-2 text-on-surface mb-4">
            Promotion is blocked until each failed task below is either retried or explicitly waived with rationale.
          </div>

          <div class="d-flex flex-column ga-3">
            <div
              v-for="failedTask in blockingFailedTasks"
              :key="failedTask.task_id"
              class="failed-task-card"
            >
              <div class="d-flex align-start justify-space-between ga-3 mb-2">
                <div>
                  <div class="text-subtitle-1 font-weight-medium">{{ failedTask.title || failedTask.task_id }}</div>
                  <div class="text-caption text-on-surface-variant">Failed task for this objective</div>
                </div>
                <v-chip color="error" size="x-small" variant="tonal">blocking</v-chip>
              </div>

              <div class="text-body-2 text-on-surface mb-3">
                {{ failedTaskPrompt(failedTask) }}
              </div>

              <div v-if="taskInsights[failedTask.task_id]" class="failure-insight mb-3">
                <div class="insight-label">Why it failed</div>
                <div v-if="taskInsights[failedTask.task_id].analysis_summary" class="insight-copy">
                  {{ taskInsights[failedTask.task_id].analysis_summary }}
                </div>
                <div v-if="taskInsights[failedTask.task_id].failure_message" class="insight-copy">
                  Raw failure: {{ taskInsights[failedTask.task_id].failure_message }}
                </div>
                <div v-if="taskInsights[failedTask.task_id].root_cause_hint" class="insight-copy">
                  Root-cause hint: {{ taskInsights[failedTask.task_id].root_cause_hint }}
                </div>
              </div>

              <div class="d-flex flex-wrap ga-2">
                <v-btn
                  size="small"
                  color="primary"
                  :loading="actionTaskId === failedTask.task_id && actionKind === 'retry'"
                  @click="retryFailedTask(failedTask.task_id)"
                >
                  Retry task
                </v-btn>
                <v-btn
                  size="small"
                  variant="tonal"
                  color="warning"
                  :loading="actionTaskId === failedTask.task_id && actionKind === 'waive'"
                  @click="waiveFailedTask(failedTask)"
                >
                  Waive with rationale
                </v-btn>
                <v-btn
                  size="small"
                  variant="tonal"
                  color="secondary"
                  @click="openTaskAssistant(failedTask.task_id)"
                >
                  Ask Harness
                </v-btn>
              </div>
            </div>
          </div>

          <div v-if="actionMessage" class="action-feedback mt-4">{{ actionMessage }}</div>
          <div v-if="actionError" class="action-error mt-3">{{ actionError }}</div>
        </v-card>

        <v-card color="surface-light" class="pa-5">
          <div class="d-flex align-center mb-4">
            <div>
              <div class="section-title">Atomic tasks</div>
              <h2 class="panel-title mt-1">{{ taskPanelTitle }}</h2>
            </div>
            <v-spacer />
            <div class="d-flex align-center ga-2">
              <v-chip v-if="selectedTaskFilter" color="surface-variant" variant="tonal" size="small">
                Filter: {{ selectedTaskFilter }}
              </v-chip>
              <v-chip color="primary" variant="tonal">{{ filteredTasks.length }} slices</v-chip>
            </div>
          </div>

          <div v-if="!tasks.length" class="text-body-2 text-on-surface-variant">
            No linked tasks exist yet for this objective.
          </div>

          <div v-else-if="!filteredTasks.length" class="text-body-2 text-on-surface-variant">
            No {{ selectedTaskFilter }} tasks are visible for this objective right now.
          </div>

          <div v-else class="d-flex flex-column ga-3">
            <div
              v-for="task in filteredTasks"
              :key="task.id"
              class="task-card"
            >
              <div class="d-flex align-start">
                <div class="status-rail" :class="statusClass(task.status)" />
                <div class="ml-4 flex-grow-1">
                  <div class="d-flex align-center flex-wrap ga-2 mb-2">
                    <h3 class="text-subtitle-1 font-weight-medium">{{ task.title }}</h3>
                    <v-chip :color="statusColor(task.status)" size="x-small" variant="tonal">
                      {{ task.status }}
                    </v-chip>
                    <v-chip color="surface-variant" variant="tonal" size="x-small">
                      {{ task.strategy || 'unspecified strategy' }}
                    </v-chip>
                  </div>
                  <div class="text-caption text-on-surface-variant">
                    Updated {{ formatTimestamp(task.updated_at) }}
                  </div>
                  <div class="mt-3">
                    <v-btn size="small" variant="tonal" color="secondary" @click="openTaskAssistant(task.id)">
                      Ask Harness
                    </v-btn>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </v-card>
      </v-col>
    </v-row>

    <div v-if="assistantOpen" class="assistant-backdrop" @click="closeTaskAssistant" />
    <aside v-if="assistantOpen" class="assistant-drawer">
      <div class="assistant-header">
        <div>
          <div class="assistant-kicker">Task conversation</div>
          <h2 class="assistant-title">{{ assistantTask?.title || 'Ask Harness' }}</h2>
        </div>
        <button type="button" class="assistant-close" @click="closeTaskAssistant">Close</button>
      </div>

      <div v-if="assistantInsight" class="assistant-section">
        <div class="assistant-section-title">Why it failed</div>
        <div v-if="assistantInsight.analysis_summary" class="assistant-copy">{{ assistantInsight.analysis_summary }}</div>
        <div v-if="assistantInsight.failure_message" class="assistant-copy">Raw failure: {{ assistantInsight.failure_message }}</div>
        <div v-if="assistantInsight.root_cause_hint" class="assistant-copy">Root-cause hint: {{ assistantInsight.root_cause_hint }}</div>
      </div>

      <div class="assistant-section">
        <div class="assistant-section-title">Quick questions</div>
        <div class="assistant-prompt-list">
          <button
            v-for="prompt in starterPrompts"
            :key="prompt"
            type="button"
            class="assistant-prompt"
            @click="sendTaskQuestion(prompt)"
          >
            {{ prompt }}
          </button>
        </div>
      </div>

      <div v-if="assistantPending" class="assistant-section assistant-status">
        <div class="assistant-section-title">Harness is still working</div>
        <div class="assistant-copy">A previous task question is still in flight. Opening this drawer does not send a new message.</div>
        <div class="assistant-copy assistant-copy-muted">Elapsed: {{ assistantPendingElapsed }}</div>
      </div>

      <div class="assistant-section assistant-transcript">
        <div class="assistant-section-title">Conversation</div>
        <div v-if="assistantLoading" class="assistant-empty">Loading task context…</div>
        <div v-else-if="!assistantTurns.length" class="assistant-empty">
          No task-specific conversation yet. Ask a question and the harness will answer in this task’s context.
        </div>
        <div v-else ref="assistantTurnsEl" class="assistant-turns" @scroll="onAssistantTranscriptScroll">
          <div
            v-for="turn in assistantTurns"
            :key="turn.id || `${turn.created_at}-${turn.role}-${turn.text}`"
            class="assistant-turn"
            :class="[ `assistant-turn-${turn.role}`, { 'assistant-turn-pending': turn.pending, 'assistant-turn-failed': turn.failed } ]"
          >
            <div class="assistant-turn-role">{{ turn.role === 'operator' ? 'You' : 'Harness' }}</div>
            <div class="assistant-turn-text">{{ turn.text }}</div>
            <div class="assistant-turn-time">{{ formatTimestamp(turn.created_at) }}</div>
          </div>
        </div>
      </div>

      <form class="assistant-composer" @submit.prevent="submitTaskQuestion">
        <label class="assistant-section-title" for="task-question">Ask Harness about this task</label>
        <textarea
          id="task-question"
          v-model="assistantInput"
          class="assistant-textarea"
          placeholder="Ask what failed, whether to retry, or what evidence matters."
          rows="4"
        />
        <div class="assistant-actions">
          <div v-if="assistantError" class="assistant-error">{{ assistantError }}</div>
          <button type="submit" class="assistant-send" :disabled="assistantSubmitting">
            {{ assistantSubmitting ? 'Sending…' : 'Send' }}
          </button>
        </div>
      </form>
    </aside>
  </v-container>
</template>

<script setup lang="ts">
import { computed, nextTick, onActivated, onDeactivated, ref, watch } from 'vue'
import { post, useApi, useSSE } from '../composables/useApi'
import ObjectiveSectionNav from '../components/ObjectiveSectionNav.vue'
import { useRoute } from 'vue-router'

const props = defineProps<{ projectId: string; objectiveId: string }>()
const route = useRoute()

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
const blockingFailedTasks = computed(() => {
  const failedTasks = objective.value?.promotion_review?.failed_tasks || []
  return failedTasks.filter((task: any) => task.effective_status === 'blocking')
})

const selectedTaskFilter = ref('')
const actionTaskId = ref('')
const actionKind = ref('')
const actionMessage = ref('')
const actionError = ref('')
const taskInsights = ref<Record<string, any>>({})
const assistantOpen = ref(false)
const assistantTaskId = ref('')
const assistantTurns = ref<any[]>([])
const assistantTurnsEl = ref<HTMLElement | null>(null)
const assistantInsight = ref<any | null>(null)
const assistantLoading = ref(false)
const assistantPending = ref(false)
const assistantSubmitting = ref(false)
const assistantInput = ref('')
const assistantError = ref('')
const assistantPendingStartedAt = ref(0)
const assistantPendingTick = ref(Date.now())

let assistantPendingTimer: number | null = null
let assistantConversationPollTimer: number | null = null
let assistantQueryHandled = ''
let assistantShouldStickToBottom = true

const taskStats = computed(() => {
  const counts = { active: 0, pending: 0, failed: 0, completed: 0 }
  for (const task of tasks.value) {
    if (task.status in counts) {
      counts[task.status as keyof typeof counts] += 1
    }
  }
  return [
    { key: 'active', label: 'Active', value: counts.active },
    { key: 'pending', label: 'Pending', value: counts.pending },
    { key: 'failed', label: 'Failed', value: counts.failed },
    { key: 'completed', label: 'Completed', value: counts.completed },
  ]
})

const filteredTasks = computed(() => {
  if (!selectedTaskFilter.value) return orderedTasks.value
  return orderedTasks.value.filter((task: any) => task.status === selectedTaskFilter.value)
})

const taskPanelTitle = computed(() => {
  if (!selectedTaskFilter.value) return 'What is happening now'
  return `${selectedTaskFilter.value[0].toUpperCase()}${selectedTaskFilter.value.slice(1)} tasks`
})

const assistantTask = computed(() => tasks.value.find((task: any) => task.id === assistantTaskId.value) || null)
const starterPrompts = computed(() => [
  'What failed here?',
  'Should I retry or waive this?',
  'Summarize the latest run in plain English.',
  'What evidence should I inspect next?',
])

const assistantPendingElapsed = computed(() => {
  if (!assistantPending.value || !assistantPendingStartedAt.value) return '0s'
  const elapsedMs = Math.max(0, assistantPendingTick.value - assistantPendingStartedAt.value)
  const totalSeconds = Math.floor(elapsedMs / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`
})

function persistContext() {
  globalThis.localStorage?.setItem('accruvia:last-project-id', props.projectId)
  globalThis.localStorage?.setItem('accruvia:last-objective-id', props.objectiveId)
  globalThis.dispatchEvent(new CustomEvent('accruvia-context-change', { detail: { projectId: props.projectId, objectiveId: props.objectiveId } }))
}

const latestPendingTurn = computed(() => {
  const pendingTurns = assistantTurns.value.filter((turn: any) => turn.pending)
  return pendingTurns[pendingTurns.length - 1] || null
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

function failedTaskPrompt(task: any) {
  const disposition = task?.disposition?.kind
  if (disposition) {
    return `A prior disposition was recorded as ${String(disposition).replaceAll('_', ' ')}, but this task is still blocking promotion. Review and resolve it here.`
  }
  return 'This failed task still needs an explicit operator decision. Retry it if the implementation should run again, or waive it if it is obsolete and should not block promotion.'
}

function toggleTaskFilter(filterKey: string) {
  selectedTaskFilter.value = selectedTaskFilter.value === filterKey ? '' : filterKey
}

function startAssistantPending() {
  const anchorRaw = latestPendingTurn.value?.queued_at || latestPendingTurn.value?.created_at || ''
  const anchorMs = anchorRaw ? new Date(anchorRaw).getTime() : Date.now()
  assistantPending.value = true
  assistantPendingStartedAt.value = Number.isNaN(anchorMs) ? Date.now() : anchorMs
  assistantPendingTick.value = assistantPendingStartedAt.value
  if (assistantPendingTimer !== null) globalThis.clearInterval(assistantPendingTimer)
  assistantPendingTimer = globalThis.setInterval(() => {
    assistantPendingTick.value = Date.now()
  }, 1000)
}

function scrollAssistantToBottom(force = false) {
  const el = assistantTurnsEl.value
  if (!el) return
  if (!force && !assistantShouldStickToBottom) return
  el.scrollTop = el.scrollHeight
}

function onAssistantTranscriptScroll() {
  const el = assistantTurnsEl.value
  if (!el) return
  assistantShouldStickToBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 24
}

function stopAssistantPending() {
  assistantPending.value = false
  assistantPendingStartedAt.value = 0
  if (assistantPendingTimer !== null) {
    globalThis.clearInterval(assistantPendingTimer)
    assistantPendingTimer = null
  }
}

function stopAssistantConversationPolling() {
  if (assistantConversationPollTimer !== null) {
    globalThis.clearInterval(assistantConversationPollTimer)
    assistantConversationPollTimer = null
  }
}

function syncAssistantPendingState() {
  const hasPending = assistantTurns.value.some((turn: any) => turn.pending)
  if (hasPending) {
    if (!assistantPending.value) startAssistantPending()
  } else {
    stopAssistantPending()
    stopAssistantConversationPolling()
  }
}

async function fetchJson(url: string, options?: RequestInit) {
  const response = await globalThis.fetch(url, options)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload?.error || `${response.status} ${response.statusText}`)
  }
  return payload
}

async function refreshAssistantConversation() {
  if (!assistantTaskId.value) return
  const conversation = await fetchJson(`/api/tasks/${encodeURIComponent(assistantTaskId.value)}/conversation`)
  assistantTurns.value = conversation.turns || []
  syncAssistantPendingState()
  await nextTick()
  scrollAssistantToBottom()
}

function ensureAssistantConversationPolling() {
  if (assistantConversationPollTimer !== null) return
  assistantConversationPollTimer = globalThis.setInterval(() => {
    if (!assistantOpen.value || !assistantTaskId.value) {
      stopAssistantConversationPolling()
      return
    }
    void refreshAssistantConversation()
  }, 3000)
}

async function refreshObjective() {
  await fetchSummary()
  await fetchDetail()
  void preloadBlockingTaskInsights()
}

async function retryFailedTask(taskId: string) {
  actionTaskId.value = taskId
  actionKind.value = 'retry'
  actionError.value = ''
  actionMessage.value = ''
  try {
    await post(`/api/tasks/${encodeURIComponent(taskId)}/retry`)
    actionMessage.value = 'The failed task was requeued. The harness can resume work on this objective.'
    await refreshObjective()
  } catch (error: any) {
    actionError.value = error?.message || 'Could not retry the failed task.'
  } finally {
    actionTaskId.value = ''
    actionKind.value = ''
  }
}

async function waiveFailedTask(task: any) {
  const taskId = String(task?.task_id || '')
  if (!taskId) return
  const rationale = globalThis.prompt(
    `Why should "${task.title || taskId}" be waived as obsolete?`,
    'Superseded by newer work or no longer relevant to the current objective path.',
  )
  if (rationale === null) return
  actionTaskId.value = taskId
  actionKind.value = 'waive'
  actionError.value = ''
  actionMessage.value = ''
  try {
    await post(`/api/tasks/${encodeURIComponent(taskId)}/failed-disposition`, {
      disposition: 'waive_obsolete',
      rationale,
    })
    actionMessage.value = 'The failed task was waived with rationale and should no longer block promotion.'
    await refreshObjective()
  } catch (error: any) {
    actionError.value = error?.message || 'Could not waive the failed task.'
  } finally {
    actionTaskId.value = ''
    actionKind.value = ''
  }
}

async function preloadBlockingTaskInsights() {
  for (const task of blockingFailedTasks.value) {
    const taskId = String(task.task_id || '')
    if (!taskId || taskInsights.value[taskId]) continue
    try {
      const payload = await fetchJson(`/api/tasks/${encodeURIComponent(taskId)}/insight`)
      taskInsights.value = { ...taskInsights.value, [taskId]: payload }
    } catch {
      // Defer deeper inspection to explicit task questions.
    }
  }
}

async function openTaskAssistant(taskId: string) {
  persistContext()
  assistantOpen.value = true
  assistantTaskId.value = taskId
  assistantLoading.value = true
  assistantError.value = ''
  try {
    const [conversation, insight] = await Promise.all([
      fetchJson(`/api/tasks/${encodeURIComponent(taskId)}/conversation`),
      fetchJson(`/api/tasks/${encodeURIComponent(taskId)}/insight`),
    ])
    assistantTurns.value = conversation.turns || []
    assistantInsight.value = insight
    taskInsights.value = { ...taskInsights.value, [taskId]: insight }
    syncAssistantPendingState()
    if (assistantTurns.value.some((turn: any) => turn.pending)) ensureAssistantConversationPolling()
    assistantShouldStickToBottom = true
    await nextTick()
    scrollAssistantToBottom(true)
  } catch (error: any) {
    assistantError.value = error?.message || 'Could not load task conversation.'
  } finally {
    assistantLoading.value = false
  }
}

async function openAssistantFromRoute() {
  const taskId = typeof route.query.taskId === 'string' ? route.query.taskId : ''
  if (!taskId || taskId === assistantQueryHandled) return
  const exists = tasks.value.some((task: any) => task.id === taskId) || blockingFailedTasks.value.some((task: any) => task.task_id === taskId)
  if (!exists) return
  assistantQueryHandled = taskId
  await openTaskAssistant(taskId)
}

function closeTaskAssistant() {
  assistantOpen.value = false
  stopAssistantPending()
  stopAssistantConversationPolling()
  assistantInput.value = ''
  assistantError.value = ''
}

async function sendTaskQuestion(text: string) {
  const task = assistantTask.value
  const body = text.trim()
  if (!task || !body) return
  const submittedAt = new Date().toISOString()
  const operatorTurn = { id: `operator-${submittedAt}`, role: 'operator', text: body, created_at: submittedAt }
  const pendingTurn = {
    id: `pending-${submittedAt}`,
    role: 'harness',
    text: 'Waiting on harness response…',
    created_at: submittedAt,
    queued_at: submittedAt,
    pending: true,
  }
  assistantTurns.value = [...assistantTurns.value, operatorTurn, pendingTurn]
  assistantInput.value = ''
  assistantSubmitting.value = true
  startAssistantPending()
  ensureAssistantConversationPolling()
  assistantError.value = ''
  try {
    await nextTick()
    scrollAssistantToBottom(true)
    await post(`/api/projects/${encodeURIComponent(props.projectId)}/comments`, {
      text: body,
      author: 'ui',
      objective_id: props.objectiveId,
      task_id: task.id,
    })
    await refreshAssistantConversation()
  } catch (error: any) {
    const failureText = error?.message || 'Could not send the question to the harness.'
    assistantTurns.value = assistantTurns.value.map((turn: any) =>
      turn.id === pendingTurn.id
        ? {
            ...turn,
            text: failureText,
            pending: false,
            failed: true,
          }
        : turn,
    )
    assistantError.value = failureText
  } finally {
    assistantSubmitting.value = false
    syncAssistantPendingState()
  }
}

async function submitTaskQuestion() {
  await sendTaskQuestion(assistantInput.value)
}

const { connect, disconnect } = useSSE(() => {
  void fetchSummary()
  void fetchDetail()
  void preloadBlockingTaskInsights()
})

onActivated(() => {
  persistContext()
  void fetchSummary()
  void fetchDetail()
  void preloadBlockingTaskInsights()
  void openAssistantFromRoute()
  connect()
})

onDeactivated(() => {
  disconnect()
  stopAssistantPending()
  stopAssistantConversationPolling()
})

watch([tasks, () => route.query.taskId], () => {
  void openAssistantFromRoute()
})
</script>

<style scoped>
.task-summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 1rem;
}

.column-kicker {
  font-size: 0.78rem;
  font-weight: 600;
  color: rgb(var(--v-theme-on-surface-variant));
}

.page-kicker {
  font-size: 0.78rem;
  color: rgb(var(--v-theme-on-surface-variant));
}

.page-title {
  margin-top: 0.15rem;
  font-size: 2rem;
  font-weight: 650;
  color: rgb(var(--v-theme-on-surface));
}

.section-title,
.stat-label {
  font-size: 0.8rem;
  color: rgb(var(--v-theme-on-surface-variant));
}

.section-title {
  font-weight: 600;
}

.panel-title {
  font-size: 1.15rem;
  font-weight: 620;
  color: rgb(var(--v-theme-on-surface));
}

.stat-card {
  cursor: pointer;
  border: 1px solid rgba(125, 94, 67, 0.12);
  transition: border-color 160ms ease, transform 160ms ease, box-shadow 160ms ease;
}

.stat-card:hover {
  border-color: rgba(179, 92, 46, 0.24);
  transform: translateY(-1px);
}

.stat-card.active {
  border-color: rgba(179, 92, 46, 0.4);
  box-shadow: 0 10px 24px rgba(179, 92, 46, 0.08);
}

.stat-helper {
  font-size: 0.76rem;
  color: rgb(var(--v-theme-on-surface-variant));
}

.failed-task-card,
.gate-card,
.task-card {
  border: 1px solid rgba(125, 94, 67, 0.14);
  border-radius: 16px;
  background: rgba(255, 251, 245, 0.76);
}

.failed-task-card {
  padding: 1rem;
}

.action-feedback {
  font-size: 0.92rem;
  color: rgb(var(--v-theme-success));
}

.action-error {
  font-size: 0.92rem;
  color: rgb(var(--v-theme-error));
}

.failure-insight {
  border: 1px solid rgba(125, 94, 67, 0.12);
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.45);
  padding: 0.85rem;
}

.insight-label {
  font-size: 0.76rem;
  font-weight: 600;
  color: rgb(var(--v-theme-on-surface-variant));
  margin-bottom: 0.45rem;
}

.insight-copy {
  font-size: 0.9rem;
  line-height: 1.45;
  color: rgb(var(--v-theme-on-surface));
}

.insight-copy + .insight-copy {
  margin-top: 0.45rem;
}

.stat-value {
  font-size: 1.2rem;
  font-weight: 600;
  color: rgb(var(--v-theme-on-surface));
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

.assistant-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(47, 36, 27, 0.24);
  z-index: 40;
}

.assistant-drawer {
  position: fixed;
  top: 0;
  right: 0;
  width: min(480px, 100vw);
  height: 100vh;
  background: rgb(var(--v-theme-surface));
  box-shadow: -16px 0 40px rgba(47, 36, 27, 0.18);
  z-index: 41;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 1.2rem;
  gap: 1rem;
}

.assistant-header,
.assistant-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}

.assistant-kicker,
.assistant-section-title,
.assistant-turn-role {
  font-size: 0.76rem;
  font-weight: 600;
  color: rgb(var(--v-theme-on-surface-variant));
}

.assistant-title {
  margin-top: 0.2rem;
  font-size: 1.2rem;
  font-weight: 620;
  color: rgb(var(--v-theme-on-surface));
}

.assistant-close,
.assistant-send,
.assistant-prompt {
  border: 1px solid rgba(125, 94, 67, 0.14);
  border-radius: 12px;
  background: rgba(255, 251, 245, 0.9);
  color: rgb(var(--v-theme-on-surface));
  padding: 0.55rem 0.8rem;
  font-size: 0.84rem;
  font-weight: 600;
}

.assistant-send {
  background: rgb(var(--v-theme-primary));
  border-color: rgb(var(--v-theme-primary));
  color: rgb(var(--v-theme-on-primary));
}

.assistant-send:disabled {
  opacity: 0.6;
}

.assistant-section {
  border: 1px solid rgba(125, 94, 67, 0.12);
  border-radius: 16px;
  background: rgba(255, 251, 245, 0.72);
  padding: 1rem;
}

.assistant-copy,
.assistant-empty,
.assistant-turn-text,
.assistant-turn-time,
.assistant-error {
  font-size: 0.9rem;
  line-height: 1.45;
}

.assistant-copy + .assistant-copy {
  margin-top: 0.45rem;
}

.assistant-copy-muted {
  color: rgb(var(--v-theme-on-surface-variant));
}

.assistant-prompt-list {
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem;
  margin-top: 0.7rem;
}

.assistant-transcript {
  min-height: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
}

.assistant-turns {
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 0.8rem;
  margin-top: 0.7rem;
  min-height: 0;
  height: 100%;
  padding-right: 0.2rem;
}

.assistant-turn {
  border-radius: 14px;
  padding: 0.9rem;
  background: rgba(255, 255, 255, 0.6);
}

.assistant-turn-operator {
  background: rgba(225, 212, 191, 0.5);
}

.assistant-turn-pending {
  border: 1px dashed rgba(179, 92, 46, 0.24);
}

.assistant-turn-failed {
  border: 1px solid rgba(186, 55, 42, 0.24);
}

.assistant-turn-text {
  margin-top: 0.35rem;
  color: rgb(var(--v-theme-on-surface));
  white-space: pre-wrap;
}

.assistant-status {
  border-color: rgba(179, 92, 46, 0.2);
  background: rgba(255, 245, 236, 0.9);
}

.assistant-turn-time {
  margin-top: 0.4rem;
  color: rgb(var(--v-theme-on-surface-variant));
}

.assistant-composer {
  display: grid;
  gap: 0.65rem;
  flex: 0 0 auto;
  padding-bottom: 0.2rem;
}

.assistant-textarea {
  width: 100%;
  border: 1px solid rgba(125, 94, 67, 0.18);
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.8);
  padding: 0.8rem 0.9rem;
  color: rgb(var(--v-theme-on-surface));
  resize: vertical;
}

@media (max-width: 960px) {
  .assistant-drawer {
    width: 100vw;
  }
}
</style>

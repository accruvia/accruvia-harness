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
              <v-chip
                :color="activity(objective).tone"
                size="x-small"
                variant="flat"
                class="activity-chip"
                :class="`activity-chip-${activity(objective).tone}`"
              >
                {{ activity(objective).label }}
              </v-chip>
            </div>
            <div class="tile-title">{{ objective.title }}</div>
            <div class="tile-copy mt-2">{{ activity(objective).detail }}</div>
            <div class="tile-stats mt-3">
              <span>{{ objective.task_counts?.active || 0 }} active</span>
              <span>{{ objective.task_counts?.pending || 0 }} pending</span>
              <span>{{ objective.task_counts?.completed || 0 }} completed</span>
              <span>{{ objective.unresolved_failed_count || 0 }} blocking failed</span>
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
              <v-chip
                :color="activity(selectedObjective).tone"
                variant="flat"
                class="activity-chip"
                :class="`activity-chip-${activity(selectedObjective).tone}`"
              >
                {{ activity(selectedObjective).label }}
              </v-chip>
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
                <div class="label">Blocking Failed</div>
                <div class="value">{{ selectedObjective.unresolved_failed_count || 0 }}</div>
              </div>
              <div class="detail-stat">
                <div class="label">Last activity</div>
                <div class="value">{{ generationLastActivity(selectedObjective) }}</div>
              </div>
            </div>

            <div class="panel-label mb-2">Current constraint</div>
            <div class="panel-copy mb-4">{{ activity(selectedObjective).detail }}</div>

            <div v-if="generationStatusNote(selectedObjective)" class="detail-callout mb-4">
              <div class="panel-label mb-1">Generation status</div>
              <div class="text-body-2 text-on-surface">{{ generationStatusNote(selectedObjective) }}</div>
            </div>

            <div v-if="blockingReason(selectedObjective)" class="detail-callout mb-4">
              <div class="panel-label mb-1">Blocking reason</div>
              <div class="text-body-2 text-on-surface">{{ blockingReason(selectedObjective) }}</div>
            </div>

            <div v-if="blockingFailedTasks(selectedObjective).length" class="detail-callout mb-4">
              <div class="panel-label mb-2">Do this here</div>
              <div
                v-for="failedTask in blockingFailedTasks(selectedObjective)"
                :key="failedTask.task_id"
                class="failed-task-card mb-3"
              >
                <div class="text-body-2 font-weight-medium mb-1">{{ failedTask.title || failedTask.task_id }}</div>
                <div class="text-caption text-on-surface-variant mb-3">
                  Retry this failed task if the work should run again, or waive it with rationale if it is obsolete and should stop blocking promotion.
                </div>
                <div v-if="taskInsights[failedTask.task_id]" class="failure-insight mb-3">
                  <div class="panel-label mb-1">Why it failed</div>
                  <div v-if="taskInsights[failedTask.task_id].analysis_summary" class="text-body-2 mb-2">
                    {{ taskInsights[failedTask.task_id].analysis_summary }}
                  </div>
                  <div v-if="taskInsights[failedTask.task_id].failure_message" class="text-caption text-on-surface-variant mb-1">
                    Raw failure: {{ taskInsights[failedTask.task_id].failure_message }}
                  </div>
                  <div v-if="taskInsights[failedTask.task_id].root_cause_hint" class="text-caption text-on-surface-variant">
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
              <div v-if="actionMessage" class="action-feedback mt-3">{{ actionMessage }}</div>
              <div v-if="actionError" class="action-error mt-2">{{ actionError }}</div>
            </div>

            <div class="d-flex flex-wrap ga-2">
              <v-btn size="small" prepend-icon="$sourceBranch" :to="{ name: 'objective-atomic', params: { projectId: selectedObjective.project_id, objectiveId: selectedObjective.id } }">Open Atomicity</v-btn>
              <v-btn size="small" variant="tonal" prepend-icon="$bookOpenVariant" :to="{ name: 'objective', params: { projectId: selectedObjective.project_id, objectiveId: selectedObjective.id } }">Overview</v-btn>
            </div>
          </v-card>
        </div>
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
            <div class="assistant-turn-time">{{ formatRelativeTime(turn.created_at) }}</div>
          </div>
        </div>
      </div>

      <form class="assistant-composer" @submit.prevent="submitTaskQuestion">
        <label class="assistant-section-title" for="global-task-question">Ask Harness about this task</label>
        <textarea
          id="global-task-question"
          v-model="assistantInput"
          class="assistant-textarea"
          placeholder="Ask what failed, whether to retry, or what evidence matters."
          rows="4"
        />
        <div class="assistant-actions">
          <div v-if="assistantError" class="assistant-error">{{ assistantError }}</div>
          <button type="submit" class="assistant-send" :disabled="assistantSendState.disabled">
            {{ assistantSendState.label }}
          </button>
        </div>
      </form>
    </aside>
  </v-container>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { post, useApi } from '../composables/useApi'
import HarnessSectionNav from '../components/HarnessSectionNav.vue'
import { assistantSendButtonState, hasAssistantPending } from '../lib/assistantState'
import { buildContextChangeDetail } from '../lib/contextState'
import { assistantPendingAnchorMs, isAtomicityRelevantObjective, latestPendingTurn as selectLatestPendingTurn } from '../lib/taskConversation'

const { data, fetch } = useApi<any>('/api/atomicity')
const selectedId = ref('')
const rawObjectives = computed(() => data.value?.objectives || [])
const objectives = computed(() => rawObjectives.value.filter(isAtomicityRelevantObjective))
const selectedObjective = computed(() => objectives.value.find((objective: any) => objective.id === selectedId.value) || objectives.value[0] || null)
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
let assistantShouldStickToBottom = true

const assistantSendState = computed(() => assistantSendButtonState(assistantSubmitting.value))

const assistantTask = computed(() => {
  const taskId = assistantTaskId.value
  if (!taskId) return null
  for (const objective of objectives.value) {
    const failedTask = (objective.failed_tasks || []).find((task: any) => task.task_id === taskId)
    if (failedTask) return { id: taskId, title: failedTask.title || taskId, objective_id: objective.id }
  }
  return null
})

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

const latestPendingTurn = computed(() => selectLatestPendingTurn(assistantTurns.value))

function firstFailedCheck(checkGroup: any) {
  return (checkGroup?.checks || []).find((check: any) => !check.ok)
}

function firstRelevantFailedCheck(checkGroup: any) {
  return (checkGroup?.checks || []).find((check: any) => !check.ok && !String(check.key || '').endsWith('_placeholder'))
}

function activity(objective: any) {
  const counts = objective.task_counts || {}
  if ((counts.active || 0) > 0) {
    return { tone: 'info', label: `${counts.active} active tasks`, detail: 'The harness is currently executing atomic units for this objective.' }
  }
  const generation = objective.atomic_generation || {}
  if (generation.status === 'running' && generation.is_stale) {
    return {
      tone: 'warning',
      label: 'Generation stalled',
      detail: `Atomic generation started ${formatRelativeTime(generation.started_at)} and has not reported activity since ${formatRelativeTime(generation.last_activity_at)}.`,
    }
  }
  if (generation.status === 'running') {
    return {
      tone: 'info',
      label: 'Generating atomic units',
      detail: generation.phase
        ? `Atomic generation is ${generation.phase}. Last activity ${formatRelativeTime(generation.last_activity_at)}.`
        : `Atomic generation is in progress. Last activity ${formatRelativeTime(generation.last_activity_at)}.`,
    }
  }
  const blocker = firstRelevantFailedCheck(objective.execution_gate)
  if (blocker?.key === 'interrogation_complete') {
    return { tone: 'warning', label: 'Waiting on interrogation', detail: blocker.detail }
  }
  if (blocker?.key === 'mermaid_finished') {
    return { tone: 'warning', label: 'Waiting on Mermaid', detail: blocker.detail }
  }
  if ((counts.pending || 0) > 0) {
    return { tone: 'warning', label: `${counts.pending} pending tasks`, detail: 'Atomic tasks exist but are waiting to run.' }
  }
  if ((objective.unresolved_failed_count || 0) > 0) {
    return {
      tone: 'error',
      label: objective.unresolved_failed_count === 1 ? '1 blocking failed task' : `${objective.unresolved_failed_count} blocking failed tasks`,
      detail: 'Promotion/review cannot proceed until these failed tasks are retried or explicitly dispositioned.',
    }
  }
  if ((counts.failed || 0) > 0) {
    return {
      tone: 'surface-variant',
      label: `${counts.failed} historical failed`,
      detail: 'This objective has failed task history, but those failures are not currently blocking progress.',
    }
  }
  if ((counts.completed || 0) > 0) {
    return { tone: 'success', label: 'Atomic work completed', detail: 'Atomic execution has completed for this objective.' }
  }
  return { tone: 'surface-variant', label: 'Idle', detail: 'No atomic generation or task execution is active right now.' }
}

function blockingReason(objective: any) {
  if ((objective.unresolved_failed_count || 0) > 0) {
    return objective.unresolved_failed_count === 1
      ? 'One failed task still needs an explicit retry or disposition decision before workflow can advance.'
      : `${objective.unresolved_failed_count} failed tasks still need explicit retry or disposition decisions before workflow can advance.`
  }
  const generation = objective.atomic_generation || {}
  if (generation.status === 'running' && generation.is_stale) {
    return 'Atomic generation appears stale. The harness has not reported generation activity recently and may need a restart or operator review.'
  }
  return firstRelevantFailedCheck(objective.execution_gate)?.detail || ''
}

function generationLabel(objective: any) {
  const generation = objective.atomic_generation || {}
  if (generation.status === 'running' && generation.is_stale) return 'stalled'
  if (generation.status === 'running') return 'running'
  if (generation.status === 'completed') return `v${generation.diagram_version || '?'} done`
  if (generation.status === 'failed') return 'failed'
  return 'idle'
}

function generationLastActivity(objective: any) {
  const generation = objective.atomic_generation || {}
  if (!generation.last_activity_at) return 'none'
  return formatRelativeTime(generation.last_activity_at)
}

function generationStatusNote(objective: any) {
  const generation = objective.atomic_generation || {}
  if (generation.status === 'running' && generation.is_stale) {
    return `This generation started ${formatRelativeTime(generation.started_at)} and has not emitted progress since ${formatRelativeTime(generation.last_activity_at)}. It looks stale, not actively working.`
  }
  if (generation.status === 'running') {
    return `This generation started ${formatRelativeTime(generation.started_at)} and last reported activity ${formatRelativeTime(generation.last_activity_at)}.`
  }
  if (generation.status === 'completed') {
    return `Atomic generation completed ${formatRelativeTime(generation.completed_at || generation.last_activity_at)}.`
  }
  if (generation.status === 'failed') {
    return `Atomic generation failed ${formatRelativeTime(generation.failed_at || generation.last_activity_at)}.`
  }
  return ''
}

function blockingFailedTasks(objective: any) {
  const failedTasks = objective?.failed_tasks || []
  return failedTasks.filter((task: any) => task.effective_status === 'blocking')
}

function persistContext(projectId: string, objectiveId: string) {
  globalThis.localStorage?.setItem('accruvia:last-project-id', projectId)
  globalThis.localStorage?.setItem('accruvia:last-objective-id', objectiveId)
  globalThis.dispatchEvent(new CustomEvent('accruvia-context-change', { detail: buildContextChangeDetail(projectId, objectiveId) }))
}

async function fetchJson(url: string, options?: RequestInit) {
  const response = await globalThis.fetch(url, options)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload?.error || `${response.status} ${response.statusText}`)
  }
  return payload
}

function startAssistantPending() {
  const anchorMs = assistantPendingAnchorMs(latestPendingTurn.value)
  assistantPending.value = true
  assistantPendingStartedAt.value = anchorMs
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
  const hasPending = hasAssistantPending(assistantTurns.value)
  if (hasPending) {
    if (!assistantPending.value) startAssistantPending()
  } else {
    stopAssistantPending()
    stopAssistantConversationPolling()
  }
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

async function refreshAtomicity() {
  await fetch()
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
    await refreshAtomicity()
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
    await refreshAtomicity()
  } catch (error: any) {
    actionError.value = error?.message || 'Could not waive the failed task.'
  } finally {
    actionTaskId.value = ''
    actionKind.value = ''
  }
}

async function preloadBlockingTaskInsights() {
  for (const objective of objectives.value) {
    for (const task of blockingFailedTasks(objective)) {
      const taskId = String(task.task_id || '')
      if (!taskId || taskInsights.value[taskId]) continue
      try {
        const payload = await fetchJson(`/api/tasks/${encodeURIComponent(taskId)}/insight`)
        taskInsights.value = { ...taskInsights.value, [taskId]: payload }
      } catch {
        // Explicit task assistant loading can still fetch this on demand.
      }
    }
  }
}

async function openTaskAssistant(taskId: string) {
  if (selectedObjective.value) {
    persistContext(selectedObjective.value.project_id, selectedObjective.value.id)
  }
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
  if (!task || !body || !selectedObjective.value) return
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
    await post(`/api/projects/${encodeURIComponent(selectedObjective.value.project_id)}/comments`, {
      text: body,
      author: 'ui',
      objective_id: task.objective_id,
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

function formatRelativeTime(value: string) {
  if (!value) return 'recently'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'recently'
  const diffMinutes = Math.floor((Date.now() - date.getTime()) / 60000)
  if (diffMinutes < 1) return 'just now'
  if (diffMinutes < 60) return `${diffMinutes} min ago`
  const diffHours = Math.floor(diffMinutes / 60)
  if (diffHours < 24) return `${diffHours} hr ago`
  const diffDays = Math.floor(diffHours / 24)
  if (diffDays < 7) return `${diffDays} day${diffDays === 1 ? '' : 's'} ago`
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

onMounted(async () => {
  await fetch()
  if (!selectedId.value && objectives.value[0]?.id) selectedId.value = objectives.value[0].id
  void preloadBlockingTaskInsights()
})

watch(selectedObjective, (objective) => {
  if (!objective) return
  persistContext(objective.project_id, objective.id)
}, { immediate: true })
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

.activity-chip {
  font-weight: 600;
}

.activity-chip-surface-variant {
  background: rgba(190, 158, 123, 0.2) !important;
  color: rgb(var(--v-theme-on-surface)) !important;
}

.activity-chip-warning {
  color: rgb(var(--v-theme-on-warning)) !important;
}

.activity-chip-error {
  color: rgb(var(--v-theme-on-error)) !important;
}

.activity-chip-info {
  color: rgb(var(--v-theme-on-info)) !important;
}

.activity-chip-success {
  color: rgb(var(--v-theme-on-success)) !important;
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

.failed-task-card {
  border: 1px solid rgba(125, 94, 67, 0.12);
  border-radius: 14px;
  background: rgba(255, 251, 245, 0.76);
  padding: 0.9rem 1rem;
}

.failure-insight {
  border: 1px solid rgba(125, 94, 67, 0.12);
  border-radius: 14px;
  background: rgba(255, 255, 255, 0.45);
  padding: 0.85rem;
}

.action-feedback {
  font-size: 0.92rem;
  color: rgb(var(--v-theme-success));
}

.action-error {
  font-size: 0.92rem;
  color: rgb(var(--v-theme-error));
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
  font-size: 1.35rem;
  font-weight: 620;
  color: rgb(var(--v-theme-on-surface));
}

.assistant-close,
.assistant-send,
.assistant-prompt {
  border: 0;
  border-radius: 999px;
  background: rgba(179, 92, 46, 0.12);
  color: rgb(var(--v-theme-on-surface));
  cursor: pointer;
  font: inherit;
}

.assistant-close,
.assistant-send {
  padding: 0.55rem 0.9rem;
}

.assistant-send {
  background: rgb(var(--v-theme-primary));
  color: rgb(var(--v-theme-on-primary));
}

.assistant-section {
  border: 1px solid rgba(125, 94, 67, 0.1);
  border-radius: 16px;
  background: rgba(255, 251, 245, 0.72);
  padding: 0.9rem;
}

.assistant-copy,
.assistant-empty,
.assistant-turn-text {
  font-size: 0.93rem;
  line-height: 1.5;
  color: rgb(var(--v-theme-on-surface));
}

.assistant-copy + .assistant-copy {
  margin-top: 0.45rem;
}

.assistant-copy-muted {
  color: rgb(var(--v-theme-on-surface-variant));
}

.assistant-error {
  font-size: 0.85rem;
  color: rgb(var(--v-theme-error));
}

.assistant-prompt-list {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-top: 0.75rem;
}

.assistant-prompt {
  padding: 0.5rem 0.8rem;
}

.assistant-transcript {
  min-height: 0;
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
}

.assistant-turns {
  margin-top: 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  overflow: auto;
  min-height: 0;
  height: 100%;
  padding-right: 0.2rem;
}

.assistant-turn {
  border-radius: 14px;
  padding: 0.75rem 0.85rem;
  background: rgba(255, 255, 255, 0.6);
}

.assistant-turn-operator {
  background: rgba(179, 92, 46, 0.12);
}

.assistant-turn-pending {
  border: 1px dashed rgba(179, 92, 46, 0.24);
}

.assistant-turn-failed {
  border: 1px solid rgba(186, 55, 42, 0.24);
}

.assistant-turn-time {
  margin-top: 0.4rem;
  font-size: 0.76rem;
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
  min-height: 100px;
  border: 1px solid rgba(125, 94, 67, 0.18);
  border-radius: 16px;
  background: rgba(255, 251, 245, 0.88);
  padding: 0.85rem 0.95rem;
  resize: vertical;
  font: inherit;
  color: rgb(var(--v-theme-on-surface));
}

.assistant-status {
  border-color: rgba(179, 92, 46, 0.2);
  background: rgba(255, 245, 236, 0.9);
}

@media (max-width: 960px) {
  .assistant-drawer {
    width: 100vw;
  }
}
</style>

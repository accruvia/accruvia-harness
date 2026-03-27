<template>
  <v-container fluid class="pa-6">
    <div class="d-flex align-center mb-6">
      <v-btn
        icon="$arrowLeft"
        variant="text"
        size="small"
        :to="{ name: 'project', params: { projectId: props.projectId } }"
      />
      <div class="ml-3">
        <div class="page-kicker">{{ objective?.status || 'objective' }}</div>
        <h1 class="page-title">{{ objective?.title || '...' }}</h1>
      </div>
      <v-spacer />
      <v-btn
        v-if="canPromote"
        color="primary"
        prepend-icon="$rocketLaunch"
        @click="promote"
      >Promote</v-btn>
    </div>

    <ObjectiveSectionNav :project-id="props.projectId" :objective-id="props.objectiveId" />

    <v-card color="surface-light" class="pa-5 mt-4 mb-4 current-focus-card">
      <div class="d-flex align-start ga-4 flex-wrap">
        <div class="focus-copy">
          <div class="section-title mb-2">What you're working on now</div>
          <div class="focus-title">{{ currentFocus.title }}</div>
          <div class="text-body-2 text-on-surface mt-2">{{ currentFocus.detail }}</div>
          <div v-if="currentFocus.next" class="text-body-2 text-on-surface-variant mt-2">
            Next: {{ currentFocus.next }}
          </div>
          <v-btn
            v-if="currentFocus.ctaTo"
            class="mt-4"
            color="primary"
            variant="flat"
            :to="currentFocus.ctaTo"
          >
            {{ currentFocus.ctaLabel || 'Open next step' }}
          </v-btn>
        </div>
        <v-spacer />
        <div class="focus-pills">
          <v-chip size="small" color="primary" variant="tonal">
            {{ focusStageLabel }}
          </v-chip>
          <v-chip
            v-if="currentFocus.blocker"
            size="small"
            color="warning"
            variant="tonal"
          >
            {{ currentFocus.blocker }}
          </v-chip>
        </div>
      </div>
    </v-card>

    <v-card color="surface-light" class="pa-5 mb-4">
      <div class="section-title mb-2">Recorded objective brief</div>
      <div class="text-body-1 text-on-surface">
        {{ recordedBrief }}
      </div>
      <div v-if="!hasRecordedBrief" class="text-body-2 text-on-surface-variant mt-2">
        Only the objective title was recorded for this item. If you want richer context here, add the intent model or answer the red-team prompts below.
      </div>
    </v-card>

    <v-row class="mt-2">
      <!-- Left: Intent & Interrogation -->
      <v-col cols="12" md="5">
        <!-- Intent Model -->
        <v-card color="surface-light" class="pa-5 mb-4">
          <h3 class="section-title mb-3">Intent model</h3>
          <div v-if="intent" class="text-body-2 text-on-surface">
            <p class="mb-2"><strong>Success Criteria:</strong> {{ intent.intent_summary }}</p>
            <p v-if="intent.success_definition"><strong>Definition:</strong> {{ intent.success_definition }}</p>
          </div>
          <div v-else class="text-body-2 text-on-surface-variant">No intent model defined yet.</div>
        </v-card>

        <!-- Red-Team Interrogation -->
        <v-card color="surface-light" class="pa-5 mb-4">
          <div class="d-flex align-center mb-3">
            <h3 class="section-title">Red-team interrogation</h3>
            <v-spacer />
            <v-chip
              :color="interrogation?.completed ? 'success' : 'warning'"
              size="x-small"
              variant="tonal"
            >{{ interrogation?.completed ? 'Complete' : 'Pending' }}</v-chip>
          </div>
          <div v-if="interrogation?.completed" class="d-flex flex-column ga-3">
            <div class="text-body-2 text-on-surface">
              {{ interrogation?.summary || 'Interrogation is complete.' }}
            </div>
            <div v-if="latestOperatorAnswer" class="completed-review-card">
              <div class="text-caption text-on-surface-variant mb-1">Latest operator answer</div>
              <div class="text-body-2 text-on-surface">{{ latestOperatorAnswer }}</div>
            </div>
            <div v-if="interrogation?.plan_elements?.length" class="d-flex flex-column ga-2">
              <div class="text-caption text-on-surface-variant">Extracted planning elements</div>
              <div
                v-for="(item, i) in interrogation.plan_elements"
                :key="`plan-${i}`"
                class="text-body-2 text-on-surface pa-2 rounded"
                style="background: rgba(157,213,134,0.05)"
              >
                {{ item }}
              </div>
            </div>
          </div>
          <div v-else-if="interrogation?.questions?.length" class="d-flex flex-column ga-2">
            <div v-for="(q, i) in interrogation.questions" :key="i" class="text-body-2 text-on-surface pa-2 rounded" style="background: rgba(157,213,134,0.05)">
              <v-icon size="x-small" color="primary" class="mr-1">$helpCircle</v-icon>
              {{ q }}
            </div>
          </div>
        </v-card>

        <!-- Operator Comment -->
        <v-card color="surface-light" class="pa-5">
          <div class="section-title mb-3">Respond to red-team</div>
          <v-text-field
            v-model="comment"
            placeholder="Respond to red-team..."
            append-inner-icon="$send"
            :loading="commentSubmitting"
            :disabled="commentSubmitting"
            @click:append-inner="sendComment"
            @keyup.enter="sendComment"
            hide-details
          />
          <div v-if="commentSubmitting" class="text-caption text-on-surface-variant mt-3">
            Sending your answer to the harness…
          </div>
          <div v-else-if="commentError" class="text-caption text-error mt-3">
            {{ commentError }}
          </div>
          <div v-else-if="latestExchange" class="receipt-card mt-3">
            <div class="text-caption text-on-surface-variant mb-1">Latest exchange</div>
            <div class="text-body-2 font-weight-medium text-on-surface mb-1">You</div>
            <div class="text-body-2 text-on-surface mb-3">{{ latestExchange.comment.text }}</div>
            <div class="text-body-2 font-weight-medium text-on-surface mb-1">Harness</div>
            <div class="text-body-2 text-on-surface">{{ latestExchange.reply.text }}</div>
          </div>
        </v-card>

        <v-card v-if="conversationTurns.length" color="surface-light" class="pa-5 mt-4">
          <div class="section-title mb-3">Recent conversation</div>
          <div class="d-flex flex-column ga-3">
            <div
              v-for="turn in conversationTurns"
              :key="turn.id"
              class="conversation-turn"
              :class="turn.role"
            >
              <div class="d-flex align-center justify-space-between ga-3 mb-1">
                <div class="text-body-2 font-weight-medium text-on-surface">
                  {{ turn.role === 'operator' ? 'You' : turn.role === 'harness' ? 'Harness' : 'System' }}
                </div>
                <div class="text-caption text-on-surface-variant">{{ formatTimestamp(turn.created_at) }}</div>
              </div>
              <div class="text-body-2 text-on-surface">{{ turn.text }}</div>
            </div>
          </div>
        </v-card>
      </v-col>

      <!-- Right: Mermaid & Tasks & Review -->
      <v-col cols="12" md="7">
        <!-- Mermaid Diagram -->
        <v-card color="surface-light" class="pa-5 mb-4">
          <div class="d-flex align-center mb-3">
            <h3 class="section-title">Architecture workspace</h3>
            <v-spacer />
            <div v-if="diagram?.content" class="diagram-controls mr-3">
              <button type="button" @click="zoomOut">-</button>
              <button type="button" @click="resetView">Reset</button>
              <button type="button" @click="zoomIn">+</button>
            </div>
            <v-chip-group v-if="diagram">
              <v-chip
                v-for="s in ['draft', 'investigating', 'paused', 'finished']"
                :key="s"
                :color="diagram.status === s ? 'primary' : 'surface-variant'"
                size="x-small"
                variant="tonal"
              >{{ s }}</v-chip>
            </v-chip-group>
          </div>
          <div
            v-if="diagram?.content && mermaidSvg"
            ref="diagramViewport"
            class="diagram-shell"
            @wheel.prevent="onWheel"
            @mousedown="startPan"
          >
            <div
              ref="diagramContent"
              class="diagram-content"
              :style="diagramTransform"
              v-html="mermaidSvg"
            />
          </div>
          <div
            v-else-if="diagram?.content && renderError"
            class="diagram-fallback pa-3 rounded"
          >
            <div class="text-body-2 text-error mb-2">{{ renderError }}</div>
            <pre class="diagram-code">{{ diagram.content }}</pre>
          </div>
          <div v-else-if="diagram?.content" class="text-body-2 text-on-surface-variant">
            Rendering diagram…
          </div>
          <div v-else class="text-body-2 text-on-surface-variant">
            No architecture workspace artifact yet.
          </div>
        </v-card>

        <!-- Review Report Card -->
        <v-card v-if="reviewRound" color="surface-light" class="pa-5 mb-4">
          <div class="d-flex align-center mb-4">
            <h3 class="section-title">Promotion review - round {{ reviewRound.round_number }}</h3>
            <v-spacer />
            <v-chip :color="reviewRound.status === 'passed' ? 'success' : 'warning'" size="x-small" label>
              {{ reviewRound.status }}
            </v-chip>
          </div>
          <v-row dense>
            <v-col v-for="pkt in reviewRound.packets" :key="pkt.dimension" cols="12" sm="6">
              <v-card
                :color="pkt.verdict === 'pass' ? 'rgba(157,213,134,0.08)' : pkt.verdict === 'concern' ? 'rgba(232,199,106,0.08)' : 'rgba(242,114,106,0.08)'"
                class="pa-3"
                variant="flat"
              >
                <div class="d-flex align-center mb-1">
                  <v-icon
                    :color="pkt.verdict === 'pass' ? 'success' : pkt.verdict === 'concern' ? 'warning' : 'error'"
                    size="small"
                    class="mr-2"
                  >{{ pkt.verdict === 'pass' ? '$checkCircle' : '$alertCircle' }}</v-icon>
                  <span class="text-caption font-weight-bold text-uppercase">{{ dimensionLabel(pkt.dimension) }}</span>
                </div>
                <p class="text-caption text-on-surface-variant">{{ pkt.summary?.slice(0, 100) }}{{ pkt.summary?.length > 100 ? '...' : '' }}</p>
              </v-card>
            </v-col>
          </v-row>
        </v-card>

        <!-- Atomic Tasks -->
        <v-card color="surface-light" class="pa-5">
          <h3 class="section-title mb-3">
            Atomic Tasks ({{ tasks.length }})
          </h3>
          <v-list density="compact" bg-color="transparent" class="pa-0">
            <v-list-item
              v-for="task in tasks"
              :key="task.id"
              class="px-0"
            >
              <template #prepend>
                <v-icon
                  :color="task.status === 'completed' ? 'success' : task.status === 'active' ? 'info' : task.status === 'failed' ? 'error' : 'on-surface-variant'"
                  size="small"
                  :class="{ 'spin-icon': task.status === 'active' }"
                >
                  {{ task.status === 'completed' ? '$checkCircle' : task.status === 'active' ? '$loading' : task.status === 'failed' ? '$closeCircle' : '$circleOutline' }}
                </v-icon>
              </template>
              <v-list-item-title class="text-body-2">{{ task.title }}</v-list-item-title>
              <v-list-item-subtitle class="text-caption">{{ task.strategy }}</v-list-item-subtitle>
            </v-list-item>
          </v-list>
        </v-card>
      </v-col>
    </v-row>
  </v-container>
</template>

<script setup lang="ts">
import { ref, computed, onActivated, onDeactivated, onBeforeUnmount, watch } from 'vue'
import { useApi, post, useSSE } from '../composables/useApi'
import ObjectiveSectionNav from '../components/ObjectiveSectionNav.vue'
import { buildContextChangeDetail } from '../lib/contextState'

const props = defineProps<{ projectId: string; objectiveId: string }>()
const comment = ref('')
const commentSubmitting = ref(false)
const commentError = ref('')
const latestExchange = ref<any | null>(null)
const mermaidSvg = ref('')
const renderError = ref('')
const diagramViewport = ref<HTMLDivElement | null>(null)
const diagramContent = ref<HTMLDivElement | null>(null)
const diagramScale = ref(1)
const baseScale = ref(1)
const panX = ref(0)
const panY = ref(0)
const isPanning = ref(false)
const lastPointer = ref<{ x: number; y: number } | null>(null)

const { data: summary, fetch: fetchSummary } = useApi<any>(`/api/projects/${props.projectId}/summary`)
const { data: detail, fetch: fetchDetail } = useApi<any>(`/api/projects/${props.projectId}/objectives/${props.objectiveId}`)

const objective = computed(() => {
  return detail.value?.objective
    || (summary.value?.objectives || []).find((o: any) => o.id === props.objectiveId)
})

const intent = computed(() => objective.value?.intent_model)
const interrogation = computed(() => objective.value?.interrogation_review)
const diagram = computed(() => objective.value?.diagram)
const canPromote = computed(() => objective.value?.promotion_review?.review_clear)
const diagramTransform = computed(() => ({
  transform: `translate(${panX.value}px, ${panY.value}px) scale(${baseScale.value * diagramScale.value})`,
}))

const reviewRound = computed(() => {
  const rounds = objective.value?.promotion_review?.review_rounds || []
  return rounds[0] || null
})

const tasks = computed(() => {
  return detail.value?.tasks
    || (summary.value?.tasks || []).filter((t: any) => t.objective_id === props.objectiveId)
})
const conversationTurns = computed(() => {
  const comments = (detail.value?.comments || []).map((item: any) => ({ ...item, role: 'operator' }))
  const replies = (detail.value?.replies || []).map((item: any) => ({ ...item, role: 'harness' }))
  const receipts = (detail.value?.receipts || []).map((item: any) => ({ ...item, role: 'system' }))
  return [...comments, ...replies, ...receipts]
    .sort((left: any, right: any) => String(left.created_at || '').localeCompare(String(right.created_at || '')))
})
const latestOperatorAnswer = computed(() => {
  const operatorTurns = conversationTurns.value.filter((turn: any) => turn.role === 'operator')
  return operatorTurns[operatorTurns.length - 1]?.text || ''
})
const hasRecordedBrief = computed(() => Boolean(String(objective.value?.summary || '').trim()))
const recordedBrief = computed(() => {
  const summaryText = String(objective.value?.summary || '').trim()
  if (summaryText) return summaryText
  return `Title only: ${objective.value?.title || 'Untitled objective'}`
})

const focusStageLabel = computed(() => {
  const stage = String(objective.value?.workflow?.current_stage || 'planning')
  return stage.charAt(0).toUpperCase() + stage.slice(1)
})

const currentFocus = computed(() => {
  const planningChecks = objective.value?.workflow?.planning?.checks || []
  const failedPlanningChecks = planningChecks.filter((check: any) => !check.ok && !String(check.key || '').endsWith('_placeholder'))
  const interrogationBlocked = failedPlanningChecks.find((check: any) => check.key === 'interrogation_complete')
  const intentBlocked = failedPlanningChecks.find((check: any) => check.key === 'intent_model')
  const mermaidBlocked = failedPlanningChecks.find((check: any) => check.key === 'mermaid_finished')
  const reviewPhase = String(objective.value?.promotion_review?.phase || '')
  const completedTasks = Number(objective.value?.promotion_review?.task_counts?.completed || 0)

  if (interrogationBlocked) {
    return {
      title: 'Answer the red-team questions',
      detail: 'The harness is waiting on operator input before it can tighten the intent and move this objective through planning.',
      next: 'Use the response box below to answer the red-team interrogation prompts.',
      blocker: 'Interrogation pending',
      ctaTo: null,
      ctaLabel: '',
    }
  }

  if (intentBlocked) {
    return {
      title: 'Define the intent model',
      detail: 'This objective still needs a concrete success definition before the harness can plan or execute work.',
      next: 'Add the success criteria and definition this objective should satisfy.',
      blocker: 'Intent model missing',
      ctaTo: null,
      ctaLabel: '',
    }
  }

  if (mermaidBlocked) {
    return {
      title: 'Finish the workflow Mermaid',
      detail: 'Planning input exists, but execution is still blocked until the architecture workspace moves past draft review.',
      next: 'Review the Mermaid and get it to a finished state before execution.',
      blocker: 'Mermaid not finished',
      ctaTo: null,
      ctaLabel: '',
    }
  }

  if (reviewPhase === 'remediation_required') {
    return {
      title: 'Resolve promotion review findings in Atomicity',
      detail: 'This objective is not waiting on passive promotion. Promotion review found concerns, and the remaining work is a remediation task in Atomicity.',
      next: objective.value?.promotion_review?.next_action || 'Open Atomicity and resolve the failed remediation task before returning to Promotion.',
      blocker: 'Atomic remediation required',
      ctaTo: { name: 'objective-atomicity', params: { projectId: props.projectId, objectiveId: props.objectiveId } },
      ctaLabel: 'Open Atomicity',
    }
  }

  if (completedTasks > 0 && !objective.value?.promotion_review?.review_clear) {
    return {
      title: 'Review promotion status',
      detail: 'Execution work exists, and the harness is now waiting on promotion review rather than more planning.',
      next: objective.value?.promotion_review?.next_action || 'Open Promotion to review the current round.',
      blocker: 'Promotion review pending',
      ctaTo: { name: 'objective-promotion', params: { projectId: props.projectId, objectiveId: props.objectiveId } },
      ctaLabel: 'Open Promotion',
    }
  }

  return {
    title: 'Review the current objective state',
    detail: 'This objective has no active blocker summary yet. Use the sections below to inspect intent, interrogation, and task activity.',
    next: 'Open Atomicity if you want to inspect concrete task execution next.',
    blocker: '',
    ctaTo: null,
    ctaLabel: '',
  }
})

function persistContext() {
  globalThis.localStorage?.setItem('accruvia:last-project-id', props.projectId)
  globalThis.localStorage?.setItem('accruvia:last-objective-id', props.objectiveId)
  globalThis.dispatchEvent(new CustomEvent('accruvia-context-change', { detail: buildContextChangeDetail(props.projectId, props.objectiveId) }))
}

function dimensionLabel(dim: string) {
  const labels: Record<string, string> = {
    intent_fidelity: 'Intent', unit_test_coverage: 'QA', integration_e2e_coverage: 'E2E',
    security: 'Security', devops: 'DevOps', atomic_fidelity: 'Atomic', code_structure: 'Arch',
  }
  return labels[dim] || dim
}

function formatTimestamp(value: string) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

async function sendComment() {
  if (!comment.value.trim()) return
  const draft = comment.value
  commentSubmitting.value = true
  commentError.value = ''
  try {
    const response = await post(`/api/projects/${props.projectId}/comments`, {
      text: draft,
      objective_id: props.objectiveId,
      author: 'operator',
    })
    latestExchange.value = response
    comment.value = ''
    void fetchSummary()
    void fetchDetail()
  } catch (error: any) {
    commentError.value = error?.message || 'Failed to send your answer.'
  } finally {
    commentSubmitting.value = false
  }
}

async function promote() {
  await post(`/api/objectives/${props.objectiveId}/promote`)
  void fetchSummary()
  void fetchDetail()
}

let mermaidModule: any = null
let mermaidLoadPromise: Promise<any> | null = null

async function ensureMermaid() {
  if (mermaidModule) return mermaidModule
  if (!mermaidLoadPromise) {
    mermaidLoadPromise = import('https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs')
      .then((module) => {
        const instance = module.default
        instance.initialize({
          startOnLoad: false,
          theme: 'default',
          securityLevel: 'loose',
          flowchart: {
            useMaxWidth: false,
            htmlLabels: true,
          },
          themeVariables: {
            fontSize: '18px',
          },
        })
        mermaidModule = instance
        return instance
      })
  }
  return mermaidLoadPromise
}

async function renderDiagram() {
  const code = diagram.value?.content || ''
  if (!code) {
    mermaidSvg.value = ''
    renderError.value = ''
    return
  }
  try {
    renderError.value = ''
    const mermaid = await ensureMermaid()
    const id = `objective-diagram-${Math.random().toString(36).slice(2)}`
    const rendered = await mermaid.render(id, code)
    mermaidSvg.value = rendered.svg
    queueMicrotask(() => {
      fitDiagramToViewport()
    })
  } catch (error: any) {
    mermaidSvg.value = ''
    renderError.value = error?.message || 'Failed to render Mermaid diagram.'
  }
}

function clampScale(next: number) {
  return Math.min(4, Math.max(0.35, Number(next.toFixed(3))))
}

function zoomIn() {
  diagramScale.value = clampScale(diagramScale.value * 1.2)
}

function zoomOut() {
  diagramScale.value = clampScale(diagramScale.value / 1.2)
}

function resetView() {
  fitDiagramToViewport()
}

function fitDiagramToViewport() {
  if (!diagramViewport.value || !diagramContent.value) {
    baseScale.value = 1
    diagramScale.value = 1
    panX.value = 0
    panY.value = 0
    return
  }
  const svg = diagramContent.value.querySelector('svg')
  if (!(svg instanceof SVGSVGElement)) {
    baseScale.value = 1
    diagramScale.value = 1
    panX.value = 0
    panY.value = 0
    return
  }
  const viewBox = svg.viewBox.baseVal
  const naturalWidth = viewBox && viewBox.width ? viewBox.width : Number(svg.getAttribute('width') || 0) || 1200
  const naturalHeight = viewBox && viewBox.height ? viewBox.height : Number(svg.getAttribute('height') || 0) || 700
  const viewportWidth = Math.max(200, diagramViewport.value.clientWidth - 32)
  const viewportHeight = Math.max(200, diagramViewport.value.clientHeight - 32)
  const fit = Math.min(viewportWidth / naturalWidth, viewportHeight / naturalHeight)
  baseScale.value = Math.max(0.45, Math.min(1.6, fit || 1))
  diagramScale.value = 1
  panX.value = Math.max(0, (viewportWidth - naturalWidth * baseScale.value) / 2)
  panY.value = Math.max(0, (viewportHeight - naturalHeight * baseScale.value) / 2)
}

function onWheel(event: WheelEvent) {
  if (!diagramViewport.value) return
  const delta = event.deltaY < 0 ? 1.1 : 0.9
  const currentScale = baseScale.value * diagramScale.value
  const nextScale = clampScale(diagramScale.value * delta)
  const actualNextScale = baseScale.value * nextScale
  const rect = diagramViewport.value.getBoundingClientRect()
  const pointerX = event.clientX - rect.left
  const pointerY = event.clientY - rect.top
  const worldX = (pointerX - panX.value) / currentScale
  const worldY = (pointerY - panY.value) / currentScale
  diagramScale.value = nextScale
  panX.value = pointerX - worldX * actualNextScale
  panY.value = pointerY - worldY * actualNextScale
}

function startPan(event: MouseEvent) {
  if (event.button !== 0) return
  isPanning.value = true
  lastPointer.value = { x: event.clientX, y: event.clientY }
}

function onPointerMove(event: MouseEvent) {
  if (!isPanning.value || !lastPointer.value) return
  panX.value += event.clientX - lastPointer.value.x
  panY.value += event.clientY - lastPointer.value.y
  lastPointer.value = { x: event.clientX, y: event.clientY }
}

function stopPan() {
  isPanning.value = false
  lastPointer.value = null
}

const { connect, disconnect } = useSSE(() => {
  void fetchSummary()
  void fetchDetail()
})

watch(() => diagram.value?.content, () => {
  void renderDiagram()
}, { immediate: true })

onActivated(() => {
  persistContext()
  void fetchSummary()
  void fetchDetail()
  void renderDiagram()
  connect()
})
onDeactivated(() => disconnect())

watch(
  () => [props.projectId, props.objectiveId],
  () => {
    persistContext()
  },
  { immediate: true },
)

if (typeof window !== 'undefined') {
  window.addEventListener('mousemove', onPointerMove)
  window.addEventListener('mouseup', stopPan)
}

onBeforeUnmount(() => {
  if (typeof window !== 'undefined') {
    window.removeEventListener('mousemove', onPointerMove)
    window.removeEventListener('mouseup', stopPan)
  }
})
</script>

<style scoped>
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

.current-focus-card {
  border: 1px solid rgba(179, 92, 46, 0.16);
  background: linear-gradient(135deg, rgba(179, 92, 46, 0.07), rgba(95, 118, 80, 0.05));
}

.focus-copy {
  max-width: 42rem;
}

.focus-title {
  font-size: 1.2rem;
  font-weight: 650;
  color: rgb(var(--v-theme-on-surface));
}

.focus-pills {
  display: flex;
  gap: 0.5rem;
  flex-wrap: wrap;
  align-items: flex-start;
}

.receipt-card {
  padding: 0.9rem 1rem;
  border-radius: 14px;
  background: rgba(157, 213, 134, 0.08);
  border: 1px solid rgba(95, 118, 80, 0.14);
}

.conversation-turn {
  padding: 0.9rem 1rem;
  border-radius: 14px;
  border: 1px solid rgba(125, 94, 67, 0.12);
  background: rgba(255, 255, 255, 0.54);
}

.conversation-turn.operator {
  background: rgba(179, 92, 46, 0.08);
}

.conversation-turn.harness {
  background: rgba(157, 213, 134, 0.08);
}

.completed-review-card {
  padding: 0.9rem 1rem;
  border-radius: 14px;
  background: rgba(157, 213, 134, 0.08);
  border: 1px solid rgba(95, 118, 80, 0.14);
}

.section-title {
  font-size: 0.96rem;
  font-weight: 600;
  color: rgb(var(--v-theme-on-surface-variant));
}
</style>

<style scoped>
.diagram-controls {
  display: inline-flex;
  gap: 0.35rem;
}

.diagram-controls button {
  border: 1px solid rgba(125, 94, 67, 0.16);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.85);
  color: rgb(var(--v-theme-on-surface));
  padding: 0.35rem 0.75rem;
  font: inherit;
  font-size: 0.82rem;
  font-weight: 600;
  cursor: pointer;
}

.diagram-shell {
  position: relative;
  min-height: 420px;
  border-radius: 16px;
  background: rgba(255, 255, 255, 0.72);
  overflow: hidden;
  cursor: grab;
}

.diagram-shell:active {
  cursor: grabbing;
}

.diagram-content {
  position: absolute;
  top: 1rem;
  left: 1rem;
  transform-origin: top left;
  will-change: transform;
}

.diagram-content :deep(svg) {
  display: block;
  width: auto;
  height: auto;
  max-width: none;
  min-width: 640px;
}

.diagram-fallback {
  background: rgba(255, 245, 244, 0.95);
}

.diagram-code {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
  font-size: 0.83rem;
  color: rgb(var(--v-theme-on-surface));
}

.spin-icon {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from {
    transform: rotate(0deg);
  }

  to {
    transform: rotate(360deg);
  }
}
</style>

<template>
  <v-container fluid class="pa-6">
    <div class="d-flex align-center mb-6">
      <v-btn icon="mdi-arrow-left" variant="text" size="small" :to="{ name: 'project', params: { projectId } }" />
      <div class="ml-3">
        <div class="text-caption text-on-surface-variant text-uppercase">{{ objective?.status }}</div>
        <h1 class="text-h4 font-weight-bold text-on-surface">{{ objective?.title || '...' }}</h1>
      </div>
      <v-spacer />
      <v-btn
        v-if="canPromote"
        color="primary"
        prepend-icon="mdi-rocket-launch"
        @click="promote"
      >Promote</v-btn>
    </div>

    <v-row>
      <!-- Left: Intent & Interrogation -->
      <v-col cols="12" md="5">
        <!-- Intent Model -->
        <v-card color="surface-light" class="pa-5 mb-4">
          <h3 class="text-caption text-uppercase text-on-surface-variant mb-3">Intent Model</h3>
          <div v-if="intent" class="text-body-2 text-on-surface">
            <p class="mb-2"><strong>Success Criteria:</strong> {{ intent.intent_summary }}</p>
            <p v-if="intent.success_definition"><strong>Definition:</strong> {{ intent.success_definition }}</p>
          </div>
          <div v-else class="text-body-2 text-on-surface-variant">No intent model defined yet.</div>
        </v-card>

        <!-- Red-Team Interrogation -->
        <v-card color="surface-light" class="pa-5 mb-4">
          <div class="d-flex align-center mb-3">
            <h3 class="text-caption text-uppercase text-on-surface-variant">Red-Team Interrogation</h3>
            <v-spacer />
            <v-chip
              :color="interrogation?.completed ? 'success' : 'warning'"
              size="x-small"
              label
            >{{ interrogation?.completed ? 'COMPLETE' : 'PENDING' }}</v-chip>
          </div>
          <div v-if="interrogation?.questions?.length" class="d-flex flex-column ga-2">
            <div v-for="(q, i) in interrogation.questions" :key="i" class="text-body-2 text-on-surface pa-2 rounded" style="background: rgba(157,213,134,0.05)">
              <v-icon size="x-small" color="primary" class="mr-1">mdi-help-circle</v-icon>
              {{ q }}
            </div>
          </div>
        </v-card>

        <!-- Operator Comment -->
        <v-card color="surface-light" class="pa-5">
          <v-text-field
            v-model="comment"
            placeholder="Respond to red-team..."
            append-inner-icon="mdi-send"
            @click:append-inner="sendComment"
            @keyup.enter="sendComment"
            hide-details
          />
        </v-card>
      </v-col>

      <!-- Right: Mermaid & Tasks & Review -->
      <v-col cols="12" md="7">
        <!-- Mermaid Diagram -->
        <v-card color="surface-light" class="pa-5 mb-4">
          <div class="d-flex align-center mb-3">
            <h3 class="text-caption text-uppercase text-on-surface-variant">Architecture Workspace</h3>
            <v-spacer />
            <v-chip-group v-if="mermaid">
              <v-chip
                v-for="s in ['draft', 'investigating', 'paused', 'finished']"
                :key="s"
                :color="mermaid.status === s ? 'primary' : 'surface-variant'"
                size="x-small"
                label
              >{{ s }}</v-chip>
            </v-chip-group>
          </div>
          <div v-if="mermaid?.content" class="pa-3 rounded font-mono text-body-2" style="background: rgba(0,0,0,0.3); white-space: pre-wrap; max-height: 300px; overflow: auto;">{{ mermaid.content }}</div>
        </v-card>

        <!-- Review Report Card -->
        <v-card v-if="reviewRound" color="surface-light" class="pa-5 mb-4">
          <div class="d-flex align-center mb-4">
            <h3 class="text-caption text-uppercase text-on-surface-variant">Promotion Review — Round {{ reviewRound.round_number }}</h3>
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
                  >{{ pkt.verdict === 'pass' ? 'mdi-check-circle' : 'mdi-alert-circle' }}</v-icon>
                  <span class="text-caption font-weight-bold text-uppercase">{{ dimensionLabel(pkt.dimension) }}</span>
                </div>
                <p class="text-caption text-on-surface-variant">{{ pkt.summary?.slice(0, 100) }}{{ pkt.summary?.length > 100 ? '...' : '' }}</p>
              </v-card>
            </v-col>
          </v-row>
        </v-card>

        <!-- Atomic Tasks -->
        <v-card color="surface-light" class="pa-5">
          <h3 class="text-caption text-uppercase text-on-surface-variant mb-3">
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
                >
                  {{ task.status === 'completed' ? 'mdi-check-circle' : task.status === 'active' ? 'mdi-loading mdi-spin' : task.status === 'failed' ? 'mdi-close-circle' : 'mdi-circle-outline' }}
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
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useApi, post, useSSE } from '../composables/useApi'

const props = defineProps<{ projectId: string; objectiveId: string }>()
const comment = ref('')

const { data: workspace, fetch: fetchWorkspace } = useApi<any>(`/api/projects/${props.projectId}/workspace`)

const objective = computed(() => {
  return (workspace.value?.objectives || []).find((o: any) => o.id === props.objectiveId)
})

const intent = computed(() => objective.value?.intent_model)
const interrogation = computed(() => objective.value?.interrogation_review)
const mermaid = computed(() => objective.value?.mermaid)
const canPromote = computed(() => objective.value?.promotion_review?.review_clear)

const reviewRound = computed(() => {
  const rounds = objective.value?.promotion_review?.review_rounds || []
  return rounds[0] || null
})

const tasks = computed(() => {
  return (workspace.value?.tasks || []).filter((t: any) => t.objective_id === props.objectiveId)
})

function dimensionLabel(dim: string) {
  const labels: Record<string, string> = {
    intent_fidelity: 'Intent', unit_test_coverage: 'QA', integration_e2e_coverage: 'E2E',
    security: 'Security', devops: 'DevOps', atomic_fidelity: 'Atomic', code_structure: 'Arch',
  }
  return labels[dim] || dim
}

async function sendComment() {
  if (!comment.value.trim()) return
  await post(`/api/projects/${props.projectId}/comments`, {
    text: comment.value,
    objective_id: props.objectiveId,
    author: 'operator',
  })
  comment.value = ''
  fetchWorkspace()
}

async function promote() {
  await post(`/api/objectives/${props.objectiveId}/promote`)
  fetchWorkspace()
}

const { connect, disconnect } = useSSE(() => fetchWorkspace())

onMounted(() => {
  fetchWorkspace()
  connect()
})
onUnmounted(() => disconnect())
</script>

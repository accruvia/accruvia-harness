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
        <div class="text-caption text-uppercase text-on-surface-variant">Promotion Review</div>
        <h1 class="text-h4 font-weight-bold text-on-surface">{{ objective?.title || '...' }}</h1>
      </div>
    </div>

    <ObjectiveSectionNav :project-id="props.projectId" :objective-id="props.objectiveId" />

    <v-row class="mt-2">
      <v-col cols="12" lg="4">
        <v-card color="surface-light" class="pa-5 mb-4">
          <div class="d-flex align-center mb-3">
            <div class="text-caption text-uppercase text-on-surface-variant">Promotion State</div>
            <v-spacer />
            <v-chip :color="review?.review_clear ? 'success' : 'warning'" label size="x-small">
              {{ review?.review_clear ? 'clear' : 'blocked' }}
            </v-chip>
          </div>
          <div class="text-body-2 text-on-surface">
            {{ review?.next_action || 'No review action recorded yet.' }}
          </div>
        </v-card>

        <v-card color="surface-light" class="pa-5 mb-4">
          <div class="text-caption text-uppercase text-on-surface-variant mb-3">Review Metrics</div>
          <div class="d-flex flex-column ga-2">
            <div class="metric-row"><span>Rounds</span><strong>{{ rounds.length }}</strong></div>
            <div class="metric-row"><span>Packets</span><strong>{{ review?.review_packet_count || review?.objective_review_packet_count || 0 }}</strong></div>
            <div class="metric-row"><span>Unresolved failed tasks</span><strong>{{ review?.unresolved_failed_count || 0 }}</strong></div>
            <div class="metric-row"><span>Waived failed tasks</span><strong>{{ review?.waived_failed_count || 0 }}</strong></div>
          </div>
        </v-card>

        <v-card color="surface-light" class="pa-5">
          <div class="text-caption text-uppercase text-on-surface-variant mb-3">Verdict Counts</div>
          <div v-if="verdictEntries.length" class="d-flex flex-wrap ga-2">
            <v-chip
              v-for="[key, value] in verdictEntries"
              :key="key"
              :color="verdictColor(key)"
              variant="tonal"
            >
              {{ key }}: {{ value }}
            </v-chip>
          </div>
          <div v-else class="text-body-2 text-on-surface-variant">
            No verdicts recorded yet.
          </div>
        </v-card>
      </v-col>

      <v-col cols="12" lg="8">
        <v-card color="surface-light" class="pa-5 mb-4">
          <div class="d-flex align-center mb-4">
            <div>
              <div class="text-caption text-uppercase text-on-surface-variant">Latest Review Round</div>
              <h2 class="text-h6 mt-1">What promotion is saying now</h2>
            </div>
            <v-spacer />
            <v-chip v-if="latestRound" :color="roundStatusColor(latestRound.status)" label size="small">
              {{ latestRound.status }}
            </v-chip>
          </div>

          <div v-if="!latestRound" class="text-body-2 text-on-surface-variant">
            No promotion-review rounds have been recorded yet for this objective.
          </div>

          <div v-else class="d-flex flex-column ga-3">
            <div class="round-summary">
              <div class="metric-row"><span>Round</span><strong>#{{ latestRound.round_number }}</strong></div>
              <div class="metric-row"><span>Packets</span><strong>{{ latestRound.packet_count || (latestRound.packets || []).length }}</strong></div>
              <div class="metric-row"><span>Needs remediation</span><strong>{{ latestRound.needs_remediation ? 'yes' : 'no' }}</strong></div>
            </div>

            <div class="packet-grid">
              <div
                v-for="packet in latestRound.packets || []"
                :key="`${latestRound.review_id}-${packet.dimension}`"
                class="packet-card"
              >
                <div class="d-flex align-center mb-2">
                  <v-chip :color="verdictColor(packet.verdict)" size="x-small" label class="mr-2">
                    {{ packet.verdict }}
                  </v-chip>
                  <div class="text-caption font-weight-bold text-uppercase">{{ dimensionLabel(packet.dimension) }}</div>
                </div>
                <div class="text-body-2 text-on-surface">{{ packet.summary || 'No summary recorded.' }}</div>
                <div class="text-caption text-on-surface-variant mt-2">
                  Reviewer: {{ packet.reviewer || packet.dimension }}
                </div>
              </div>
            </div>
          </div>
        </v-card>

        <v-card color="surface-light" class="pa-5">
          <div class="text-caption text-uppercase text-on-surface-variant mb-3">Recent Rounds</div>
          <div v-if="!rounds.length" class="text-body-2 text-on-surface-variant">
            No review rounds yet.
          </div>
          <div v-else class="d-flex flex-column ga-3">
            <div
              v-for="round in rounds"
              :key="round.review_id"
              class="history-card"
            >
              <div class="d-flex align-center mb-2">
                <div class="text-subtitle-2 font-weight-medium">Round {{ round.round_number }}</div>
                <v-spacer />
                <v-chip :color="roundStatusColor(round.status)" size="x-small" label>
                  {{ round.status }}
                </v-chip>
              </div>
              <div class="text-caption text-on-surface-variant">
                {{ round.packet_count || (round.packets || []).length }} packets
                <span v-if="round.last_activity_at"> · last activity {{ formatTimestamp(round.last_activity_at) }}</span>
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

const review = computed(() => objective.value?.promotion_review || {})
const rounds = computed(() => review.value?.review_rounds || [])
const latestRound = computed(() => rounds.value[0] || null)

const verdictEntries = computed(() => {
  return Object.entries(review.value?.verdict_counts || {}) as Array<[string, number]>
})

function dimensionLabel(dim: string) {
  const labels: Record<string, string> = {
    intent_fidelity: 'Intent',
    unit_test_coverage: 'QA',
    integration_e2e_coverage: 'E2E',
    security: 'Security',
    devops: 'DevOps',
    atomic_fidelity: 'Atomic',
    code_structure: 'Architecture',
  }
  return labels[dim] || dim
}

function verdictColor(verdict: string) {
  if (verdict === 'pass') return 'success'
  if (verdict === 'concern') return 'warning'
  return 'error'
}

function roundStatusColor(status: string) {
  if (status === 'passed') return 'success'
  if (status === 'in_progress') return 'info'
  return 'warning'
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
.metric-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  font-size: 0.92rem;
}

.metric-row span {
  color: rgb(var(--v-theme-on-surface-variant));
}

.round-summary,
.history-card,
.packet-card {
  border: 1px solid rgba(125, 94, 67, 0.14);
  border-radius: 16px;
  background: rgba(255, 251, 245, 0.76);
  padding: 1rem;
}

.packet-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 1rem;
}
</style>

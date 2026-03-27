<template>
  <v-container fluid class="pa-6">
    <div class="d-flex align-center mb-3">
      <v-btn
        icon="$arrowLeft"
        variant="text"
        size="small"
        :to="{ name: 'dashboard' }"
      />
      <div class="ml-3">
        <div class="text-caption text-uppercase text-on-surface-variant">Token Performance</div>
        <h1 class="text-h4 font-weight-bold text-on-surface">{{ project?.name || '...' }}</h1>
      </div>
    </div>

    <ProjectSectionNav :project-id="props.projectId" />

    <div class="summary-grid mt-6">
      <v-card v-for="item in summaryCards" :key="item.label" color="surface-light" class="pa-4">
        <div class="text-caption text-uppercase text-on-surface-variant">{{ item.label }}</div>
        <div class="text-h5 font-weight-bold mt-2">{{ item.value }}</div>
      </v-card>
    </div>

    <v-row class="mt-2">
      <v-col cols="12" xl="4">
        <v-card color="surface-light" class="pa-5">
          <div class="text-caption text-uppercase text-on-surface-variant mb-3">By Objective</div>
          <div v-if="!objectiveRows.length" class="text-body-2 text-on-surface-variant">No review token data yet.</div>
          <div v-else class="d-flex flex-column ga-3">
            <div v-for="row in objectiveRows" :key="row.objective_id" class="table-card">
              <div class="text-subtitle-2 font-weight-medium">{{ row.title }}</div>
              <div class="text-caption text-on-surface-variant mt-1">
                {{ row.round_count }} rounds · {{ row.packet_count }} packets
              </div>
              <div class="metric-line mt-2"><span>Tokens</span><strong>{{ fmtTokens(row.usage.total_tokens) }}</strong></div>
              <div class="metric-line"><span>Cost</span><strong>{{ fmtCost(row.usage.cost_usd) }}</strong></div>
            </div>
          </div>
        </v-card>
      </v-col>

      <v-col cols="12" xl="4">
        <v-card color="surface-light" class="pa-5">
          <div class="text-caption text-uppercase text-on-surface-variant mb-3">By Reviewer</div>
          <div v-if="!reviewerRows.length" class="text-body-2 text-on-surface-variant">No reviewer packet data yet.</div>
          <div v-else class="d-flex flex-column ga-3">
            <div v-for="row in reviewerRows" :key="row.reviewer" class="table-card">
              <div class="text-subtitle-2 font-weight-medium">{{ row.reviewer }}</div>
              <div class="text-caption text-on-surface-variant mt-1">
                {{ row.packet_count }} packets
              </div>
              <div class="metric-line mt-2"><span>Tokens</span><strong>{{ fmtTokens(row.total_tokens) }}</strong></div>
              <div class="metric-line"><span>Cost</span><strong>{{ fmtCost(row.cost_usd) }}</strong></div>
            </div>
          </div>
        </v-card>
      </v-col>

      <v-col cols="12" xl="4">
        <v-card color="surface-light" class="pa-5">
          <div class="text-caption text-uppercase text-on-surface-variant mb-3">Top Review Rounds</div>
          <div v-if="!roundRows.length" class="text-body-2 text-on-surface-variant">No round data yet.</div>
          <div v-else class="d-flex flex-column ga-3">
            <div v-for="row in roundRows" :key="`${row.objective_id}-${row.round_number}`" class="table-card">
              <div class="text-subtitle-2 font-weight-medium">{{ row.objective_title }}</div>
              <div class="text-caption text-on-surface-variant mt-1">
                Round {{ row.round_number }} · {{ row.status }}
              </div>
              <div class="metric-line mt-2"><span>Tokens</span><strong>{{ fmtTokens(row.usage.total_tokens) }}</strong></div>
              <div class="metric-line"><span>Cost</span><strong>{{ fmtCost(row.usage.cost_usd) }}</strong></div>
            </div>
          </div>
        </v-card>
      </v-col>
    </v-row>
  </v-container>
</template>

<script setup lang="ts">
import { computed, onActivated } from 'vue'
import { useApi } from '../composables/useApi'
import ProjectSectionNav from '../components/ProjectSectionNav.vue'

const props = defineProps<{ projectId: string }>()

const { data, fetch } = useApi<any>(`/api/projects/${props.projectId}/token-performance`)

const project = computed(() => data.value?.project || null)
const totals = computed(() => data.value?.totals || {})
const summary = computed(() => data.value?.summary || {})
const objectiveRows = computed(() => data.value?.objectives || [])
const reviewerRows = computed(() => data.value?.reviewers || [])
const roundRows = computed(() => data.value?.rounds || [])

function fmtTokens(value: number) {
  return Number(value || 0).toLocaleString()
}

function fmtCost(value: number) {
  return `$${Number(value || 0).toFixed(4)}`
}

function fmtLatency(value: number) {
  return `${Math.round(Number(value || 0))}ms`
}

const summaryCards = computed(() => [
  { label: 'Total Tokens', value: fmtTokens(totals.value.total_tokens) },
  { label: 'Total Cost', value: fmtCost(totals.value.cost_usd) },
  { label: 'Review Packets', value: fmtTokens(totals.value.packet_count) },
  { label: 'Review Rounds', value: fmtTokens(totals.value.round_count) },
  { label: 'Avg Tokens / Round', value: fmtTokens(summary.value.avg_tokens_per_round) },
  { label: 'Total Latency', value: fmtLatency(totals.value.latency_ms) },
])

onActivated(() => {
  void fetch()
})
</script>

<style scoped>
.summary-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 1rem;
}

.table-card {
  border: 1px solid rgba(125, 94, 67, 0.14);
  border-radius: 16px;
  background: rgba(255, 251, 245, 0.76);
  padding: 1rem;
}

.metric-line {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  font-size: 0.92rem;
}

.metric-line span {
  color: rgb(var(--v-theme-on-surface-variant));
}
</style>

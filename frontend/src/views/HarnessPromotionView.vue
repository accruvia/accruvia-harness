<template>
  <v-container fluid class="pa-6">
    <div class="mb-6">
      <div class="page-kicker">Harness workspace</div>
      <h1 class="page-title">Promotion</h1>
    </div>

    <HarnessSectionNav />

    <v-row class="mt-6">
      <v-col cols="12" lg="7">
        <div class="d-flex align-center justify-space-between flex-wrap ga-3 mb-4">
          <h2 class="section-title">Promotion queue</h2>
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
              <v-chip :color="objective.review_clear ? 'success' : 'warning'" size="x-small" variant="tonal">
                {{ objective.review_clear ? 'Clear to promote' : promotionLabel(objective) }}
              </v-chip>
            </div>
            <div class="tile-title">{{ objective.title }}</div>
            <div class="tile-copy mt-2 line-clamp">{{ objective.next_action || 'No promotion activity has been recorded yet.' }}</div>
            <div class="tile-stats mt-3">
              <span>{{ objective.review_round_count || 0 }} rounds</span>
              <span>{{ objective.review_packet_count || 0 }} packets</span>
              <span>{{ objective.unresolved_failed_count || 0 }} unresolved failed</span>
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
              <v-chip :color="selectedObjective.review_clear ? 'success' : 'warning'" variant="tonal">
                {{ selectedObjective.review_clear ? 'Clear to promote' : promotionLabel(selectedObjective) }}
              </v-chip>
            </div>

            <div class="detail-stat-grid mb-4">
              <div class="detail-stat">
                <div class="label">Phase</div>
                <div class="value">{{ formatPhase(selectedObjective.phase) }}</div>
              </div>
              <div class="detail-stat">
                <div class="label">Review Rounds</div>
                <div class="value">{{ selectedObjective.review_round_count || 0 }}</div>
              </div>
              <div class="detail-stat">
                <div class="label">Packets</div>
                <div class="value">{{ selectedObjective.review_packet_count || 0 }}</div>
              </div>
              <div class="detail-stat">
                <div class="label">Failed Tasks</div>
                <div class="value">{{ selectedObjective.unresolved_failed_count || 0 }}</div>
              </div>
            </div>

            <div class="panel-label mb-2">What needs to happen</div>
            <div class="detail-callout mb-4">
              <div class="text-body-2 text-on-surface">{{ selectedObjective.next_action || 'No promotion activity has been recorded yet.' }}</div>
            </div>

            <div v-if="selectedObjective.latest_round" class="detail-callout mb-4">
              <div class="panel-label mb-1">Latest round</div>
              <div class="text-body-2 text-on-surface">
                Round {{ selectedObjective.latest_round.round_number || '?' }}
                · {{ selectedObjective.latest_round.packet_count || 0 }} packets
                <span v-if="selectedObjective.latest_round.needs_remediation"> · remediation required</span>
              </div>
              <div v-if="selectedObjective.latest_round.last_activity_at" class="text-caption text-on-surface-variant mt-1">
                Last activity {{ formatTimestamp(selectedObjective.latest_round.last_activity_at) }}
              </div>
            </div>

            <div class="d-flex flex-wrap ga-2">
              <v-btn size="small" prepend-icon="$rocketLaunch" :to="{ name: 'objective-promotion', params: { projectId: selectedObjective.project_id, objectiveId: selectedObjective.id } }">Open Promotion</v-btn>
              <v-btn size="small" variant="tonal" prepend-icon="$bookOpenVariant" :to="{ name: 'objective', params: { projectId: selectedObjective.project_id, objectiveId: selectedObjective.id } }">Overview</v-btn>
            </div>
          </v-card>
        </div>
      </v-col>
    </v-row>
  </v-container>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useApi } from '../composables/useApi'
import HarnessSectionNav from '../components/HarnessSectionNav.vue'

const { data, fetch } = useApi<any>('/api/promotion')
const selectedId = ref('')
const objectives = computed(() => data.value?.objectives || [])
const selectedObjective = computed(() => objectives.value.find((objective: any) => objective.id === selectedId.value) || objectives.value[0] || null)

function promotionLabel(objective: any) {
  if ((objective.unresolved_failed_count || 0) > 0) return 'Needs remediation'
  if ((objective.review_round_count || 0) > 0) return 'Review in progress'
  return 'Not ready'
}

function formatPhase(phase: string) {
  return String(phase || 'idle').replaceAll('_', ' ')
}

function formatTimestamp(value: string) {
  if (!value) return 'recently'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'recently'
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
})
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

.tile-title {
  font-size: 1.02rem;
  font-weight: 600;
  line-height: 1.4;
}

.page-kicker,
.section-meta,
.tile-project,
.panel-project,
.panel-label {
  font-size: 0.78rem;
}

.page-kicker,
.section-meta,
.tile-project,
.panel-project,
.panel-label,
.tile-copy,
.tile-stats {
  color: rgb(var(--v-theme-on-surface-variant));
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
  color: rgb(var(--v-theme-on-surface-variant));
}

.line-clamp {
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
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

.tile-copy {
  font-size: 0.92rem;
  line-height: 1.5;
}

.tile-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 0.85rem;
  font-size: 0.82rem;
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
</style>

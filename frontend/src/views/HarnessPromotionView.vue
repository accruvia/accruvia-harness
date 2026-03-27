<template>
  <v-container fluid class="pa-6">
    <div class="mb-6">
      <div class="text-caption text-uppercase text-on-surface-variant">Harness Workspace</div>
      <h1 class="text-h4 font-weight-bold text-on-surface">Promotion</h1>
    </div>

    <HarnessSectionNav />

    <div class="d-flex flex-column ga-3 mt-6">
      <v-card v-for="objective in objectives" :key="objective.id" color="surface-light" class="pa-4">
        <div class="d-flex align-center justify-space-between flex-wrap ga-3 mb-2">
          <div>
            <div class="text-caption text-uppercase text-on-surface-variant">{{ objective.project_name }}</div>
            <h3 class="text-subtitle-1 font-weight-medium text-on-surface">{{ objective.title }}</h3>
          </div>
          <v-chip :color="objective.review_clear ? 'success' : 'warning'" variant="tonal" size="small">
            {{ objective.review_clear ? 'Clear to promote' : promotionLabel(objective) }}
          </v-chip>
        </div>
        <div class="text-body-2 text-on-surface-variant mb-3">
          {{ objective.next_action || 'No promotion activity has been recorded yet.' }}
        </div>
        <div class="d-flex ga-4 flex-wrap text-caption text-on-surface-variant mb-3">
          <span>{{ objective.review_round_count || 0 }} rounds</span>
          <span>{{ objective.review_packet_count || 0 }} packets</span>
          <span>{{ objective.unresolved_failed_count || 0 }} unresolved failed</span>
          <span>{{ objective.waived_failed_count || 0 }} waived failed</span>
        </div>
        <div class="d-flex flex-wrap ga-2">
          <v-btn size="small" variant="tonal" prepend-icon="$rocketLaunch" :to="{ name: 'objective-promotion', params: { projectId: objective.project_id, objectiveId: objective.id } }">Open Promotion</v-btn>
          <v-btn size="small" variant="tonal" prepend-icon="$bookOpenVariant" :to="{ name: 'objective', params: { projectId: objective.project_id, objectiveId: objective.id } }">Overview</v-btn>
        </div>
      </v-card>
    </div>
  </v-container>
</template>

<script setup lang="ts">
import { computed, onMounted } from 'vue'
import { useApi } from '../composables/useApi'
import HarnessSectionNav from '../components/HarnessSectionNav.vue'

const { data, fetch } = useApi<any>('/api/promotion')
const objectives = computed(() => data.value?.objectives || [])

function promotionLabel(objective: any) {
  if ((objective.unresolved_failed_count || 0) > 0) return 'Needs remediation'
  if ((objective.review_round_count || 0) > 0) return 'Review in progress'
  return 'Not ready'
}

onMounted(() => {
  void fetch()
})
</script>

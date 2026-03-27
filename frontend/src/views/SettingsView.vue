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
        <div class="text-caption text-uppercase text-on-surface-variant">Project Settings</div>
        <h1 class="text-h4 font-weight-bold text-on-surface">{{ project?.name || '...' }}</h1>
      </div>
    </div>

    <ProjectSectionNav :project-id="props.projectId" />

    <v-row class="mt-2">
      <v-col cols="12" xl="8">
        <v-card color="surface-light" class="pa-5">
          <div class="text-caption text-uppercase text-on-surface-variant mb-2">Repository Promotion</div>
          <h2 class="text-h6 text-on-surface mb-4">How approved work lands back in the repo</h2>

          <div class="settings-grid">
            <label class="field">
              <span>Promotion Mode</span>
              <select v-model="form.promotion_mode">
                <option value="direct_main">Direct to main</option>
                <option value="branch_only">Branch only</option>
                <option value="branch_and_pr">Branch and PR</option>
              </select>
            </label>

            <label class="field">
              <span>Repo Provider</span>
              <select v-model="form.repo_provider">
                <option value="github">GitHub</option>
                <option value="gitlab">GitLab</option>
              </select>
            </label>

            <label class="field field-full">
              <span>Repository</span>
              <input v-model="form.repo_name" type="text" placeholder="owner/repo">
            </label>

            <label class="field">
              <span>Base Branch</span>
              <input v-model="form.base_branch" type="text" placeholder="main">
            </label>
          </div>

          <div class="d-flex align-center ga-3 mt-5">
            <v-btn color="primary" :disabled="saving || !dirty" @click="save">
              {{ saving ? 'Saving…' : 'Save Repo Settings' }}
            </v-btn>
            <div class="text-body-2" :class="statusClass">{{ statusText }}</div>
          </div>
        </v-card>
      </v-col>

      <v-col cols="12" xl="4">
        <v-card color="surface-light" class="pa-5">
          <div class="text-caption text-uppercase text-on-surface-variant mb-3">Current Project</div>
          <div class="meta-row"><span>Name</span><strong>{{ project?.name || 'Unknown' }}</strong></div>
          <div class="meta-row"><span>Provider</span><strong>{{ form.repo_provider || 'unset' }}</strong></div>
          <div class="meta-row"><span>Repository</span><strong>{{ form.repo_name || 'unset' }}</strong></div>
          <div class="meta-row"><span>Policy</span><strong>{{ form.promotion_mode || 'unset' }}</strong></div>
        </v-card>
      </v-col>
    </v-row>
  </v-container>
</template>

<script setup lang="ts">
import { computed, onActivated, reactive, ref, watch } from 'vue'
import { post, useApi } from '../composables/useApi'
import ProjectSectionNav from '../components/ProjectSectionNav.vue'

const props = defineProps<{ projectId: string }>()

const { data: summary, fetch: fetchSummary } = useApi<any>(`/api/projects/${props.projectId}/summary`)
const saving = ref(false)
const statusText = ref('No changes yet')
const statusClass = ref('text-on-surface-variant')
const baseline = ref('')
const form = reactive({
  promotion_mode: '',
  repo_provider: '',
  repo_name: '',
  base_branch: '',
})

const project = computed(() => summary.value?.project || null)
const signature = computed(() => JSON.stringify(form))
const dirty = computed(() => signature.value !== baseline.value)

watch(project, (value) => {
  if (!value) return
  form.promotion_mode = value.promotion_mode || 'direct_main'
  form.repo_provider = value.repo_provider || 'github'
  form.repo_name = value.repo_name || ''
  form.base_branch = value.base_branch || 'main'
  baseline.value = JSON.stringify(form)
  statusText.value = 'No changes yet'
  statusClass.value = 'text-on-surface-variant'
}, { immediate: true })

async function save() {
  saving.value = true
  statusText.value = 'Saving repo settings…'
  statusClass.value = 'text-warning'
  try {
    await post(`/api/projects/${props.projectId}/repo-settings`, { ...form })
    baseline.value = JSON.stringify(form)
    statusText.value = 'Repo settings saved.'
    statusClass.value = 'text-success'
    await fetchSummary()
  } catch {
    statusText.value = 'Failed to save settings.'
    statusClass.value = 'text-error'
  } finally {
    saving.value = false
  }
}

onActivated(() => {
  void fetchSummary()
})
</script>

<style scoped>
.settings-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 1rem;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
  font-size: 0.9rem;
  color: rgb(var(--v-theme-on-surface));
}

.field span {
  font-weight: 600;
}

.field-full {
  grid-column: 1 / -1;
}

.field input,
.field select {
  border: 1px solid rgba(125, 94, 67, 0.2);
  border-radius: 12px;
  background: rgba(255, 255, 255, 0.85);
  color: rgb(var(--v-theme-on-surface));
  padding: 0.8rem 0.9rem;
  font: inherit;
}

.meta-row {
  display: flex;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 0.8rem;
  font-size: 0.92rem;
}

.meta-row span {
  color: rgb(var(--v-theme-on-surface-variant));
}
</style>

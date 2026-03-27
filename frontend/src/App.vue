<template>
  <v-app>
    <v-navigation-drawer permanent width="280" color="surface" class="app-drawer border-0">
      <div class="brand-block">
        <div class="brand-kicker">Accruvia Harness</div>
        <div class="brand-title">Workspace Console</div>
      </div>

      <div v-if="projectId" class="context-block">
        <div class="context-label">Project</div>
        <router-link class="context-value context-link" :to="{ name: 'project', params: { projectId } }">
          {{ projectLabel }}
        </router-link>
        <template v-if="objectiveId">
          <div class="context-label mt-3">Objective</div>
          <router-link
            class="context-value context-link"
            :to="{ name: 'objective', params: { projectId, objectiveId } }"
          >
            {{ objectiveLabel }}
          </router-link>
        </template>
        <div class="context-actions">
          <router-link class="context-action" :to="{ name: 'harness-objectives' }">
            Change project
          </router-link>
          <router-link
            v-if="projectId"
            class="context-action"
            :to="{ name: 'project', params: { projectId } }"
          >
            Change objective
          </router-link>
        </div>
      </div>

      <v-list density="compact" nav class="px-3">
        <v-list-item
          prepend-icon="$viewDashboard"
          title="Dashboard"
          :to="{ name: 'dashboard' }"
        />
        <v-list-item
          prepend-icon="$formatListBulleted"
          title="Objectives"
          :to="{ name: 'harness-objectives' }"
        />
        <v-list-item
          prepend-icon="$sourceBranch"
          title="Atomicity"
          :to="{ name: 'harness-atomicity' }"
        />
        <v-list-item
          prepend-icon="$rocketLaunch"
          title="Promotion"
          :to="{ name: 'harness-promotion' }"
        />
        <v-list-item
          v-if="projectId"
          prepend-icon="$cogOutline"
          title="Settings"
          :to="{ name: 'project-settings', params: { projectId } }"
        />
        <v-list-item
          v-if="projectId"
          prepend-icon="$chartBox"
          title="Token Performance"
          :to="{ name: 'project-token-performance', params: { projectId } }"
        />
        <v-list-item
          prepend-icon="$bookOpenVariant"
          title="Docs"
          href="/docs"
          target="_blank"
        />
      </v-list>
    </v-navigation-drawer>

    <v-main class="app-main">
      <router-view v-slot="{ Component, route }">
        <keep-alive>
          <component :is="Component" :key="route.fullPath" />
        </keep-alive>
      </router-view>
    </v-main>
  </v-app>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { resolveVisibleContext } from './lib/contextState'

const route = useRoute()
const LAST_PROJECT_KEY = 'accruvia:last-project-id'
const LAST_OBJECTIVE_KEY = 'accruvia:last-objective-id'
const projectName = ref('')
const objectiveName = ref('')
const lastProjectId = ref(globalThis.localStorage?.getItem(LAST_PROJECT_KEY) || '')
const lastObjectiveId = ref(globalThis.localStorage?.getItem(LAST_OBJECTIVE_KEY) || '')

const currentProjectId = computed(() => {
  const raw = route.params.projectId
  return typeof raw === 'string' ? raw : ''
})

const currentObjectiveId = computed(() => {
  const raw = route.params.objectiveId
  return typeof raw === 'string' ? raw : ''
})

const resolvedContext = computed(() =>
  resolveVisibleContext({
    currentProjectId: currentProjectId.value,
    currentObjectiveId: currentObjectiveId.value,
    lastProjectId: lastProjectId.value,
    lastObjectiveId: lastObjectiveId.value,
  }),
)
const projectId = computed(() => resolvedContext.value.projectId)
const objectiveId = computed(() => resolvedContext.value.objectiveId)

const projectLabel = computed(() => projectName.value || projectId.value)
const objectiveLabel = computed(() => objectiveName.value || objectiveId.value)

async function hydrateProjectContext(projectIdValue: string, objectiveIdValue: string) {
  if (!projectIdValue) {
    projectName.value = ''
    objectiveName.value = ''
    return
  }
  try {
    const response = await globalThis.fetch(`/api/projects/${encodeURIComponent(projectIdValue)}/summary`)
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`)
    const payload = await response.json()
    projectName.value = payload?.project?.name || projectIdValue
    if (!objectiveIdValue) {
      objectiveName.value = ''
      return
    }
    const matchingObjective = (payload?.objectives || []).find((item: any) => item.id === objectiveIdValue)
    if (matchingObjective?.title) {
      objectiveName.value = matchingObjective.title
      return
    }
    const objectiveResponse = await globalThis.fetch(
      `/api/projects/${encodeURIComponent(projectIdValue)}/objectives/${encodeURIComponent(objectiveIdValue)}`,
    )
    if (!objectiveResponse.ok) throw new Error(`${objectiveResponse.status} ${objectiveResponse.statusText}`)
    const objectivePayload = await objectiveResponse.json()
    objectiveName.value = objectivePayload?.objective?.title || objectiveIdValue
  } catch {
    projectName.value = projectIdValue
    objectiveName.value = objectiveIdValue || ''
  }
}

watch(
  [currentProjectId, currentObjectiveId],
  ([nextProjectId, nextObjectiveId]) => {
    if (nextProjectId) {
      lastProjectId.value = nextProjectId
      globalThis.localStorage?.setItem(LAST_PROJECT_KEY, nextProjectId)
    }
    if (nextObjectiveId) {
      lastObjectiveId.value = nextObjectiveId
      globalThis.localStorage?.setItem(LAST_OBJECTIVE_KEY, nextObjectiveId)
    }
    void hydrateProjectContext(nextProjectId || lastProjectId.value, nextObjectiveId || lastObjectiveId.value)
  },
  { immediate: true },
)

if (typeof window !== 'undefined') {
  window.addEventListener('accruvia-context-change', (event: Event) => {
    const detail = (event as CustomEvent).detail || {}
    const nextProjectId = typeof detail.projectId === 'string' ? detail.projectId : ''
    const nextObjectiveId = typeof detail.objectiveId === 'string' ? detail.objectiveId : ''
    if (nextProjectId) {
      lastProjectId.value = nextProjectId
      globalThis.localStorage?.setItem(LAST_PROJECT_KEY, nextProjectId)
    }
    if (nextObjectiveId) {
      lastObjectiveId.value = nextObjectiveId
      globalThis.localStorage?.setItem(LAST_OBJECTIVE_KEY, nextObjectiveId)
    }
    void hydrateProjectContext(nextProjectId || lastProjectId.value, nextObjectiveId || lastObjectiveId.value)
  })
}
</script>

<style scoped>
.app-drawer {
  border-right: 1px solid rgba(125, 94, 67, 0.12);
}

.brand-block {
  padding: 1.25rem 1.25rem 0.5rem;
}

.brand-kicker,
.context-label {
  font-size: 0.74rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: rgb(var(--v-theme-on-surface-variant));
}

.brand-title {
  margin-top: 0.3rem;
  font-size: 1.25rem;
  font-weight: 700;
  color: rgb(var(--v-theme-on-surface));
}

.context-block {
  margin: 0.5rem 1.25rem 1rem;
  padding: 0.9rem 1rem;
  border-radius: 18px;
  background: linear-gradient(135deg, rgba(179, 92, 46, 0.12), rgba(95, 118, 80, 0.12));
}

.context-value {
  margin-top: 0.35rem;
  font-size: 0.88rem;
  font-weight: 600;
  color: rgb(var(--v-theme-on-surface));
  word-break: break-word;
}

.context-link {
  display: block;
  text-decoration: none;
}

.context-link:hover {
  text-decoration: underline;
}

.context-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-top: 0.85rem;
}

.context-action {
  display: inline-flex;
  align-items: center;
  border: 1px solid rgba(125, 94, 67, 0.14);
  border-radius: 999px;
  background: rgba(255, 251, 245, 0.72);
  color: rgb(var(--v-theme-on-surface));
  font-size: 0.78rem;
  font-weight: 600;
  padding: 0.38rem 0.7rem;
  text-decoration: none;
}

.context-action:hover {
  border-color: rgba(179, 92, 46, 0.28);
}

.app-main {
  min-height: 100vh;
  background:
    radial-gradient(circle at top right, rgba(179, 92, 46, 0.12), transparent 28%),
    radial-gradient(circle at left center, rgba(95, 118, 80, 0.12), transparent 22%),
    rgb(var(--v-theme-background));
}
</style>

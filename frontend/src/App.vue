<template>
  <v-app>
    <v-navigation-drawer permanent width="280" color="surface" class="app-drawer border-0">
      <div class="brand-block">
        <div class="brand-kicker">Accruvia Harness</div>
        <div class="brand-title">Workspace Console</div>
      </div>

      <div v-if="projectId" class="context-block">
        <div class="context-label">Project</div>
        <div class="context-value">{{ projectId }}</div>
      </div>

      <v-list density="compact" nav class="px-3">
        <v-list-item
          prepend-icon="$viewDashboard"
          title="Dashboard"
          :to="{ name: 'dashboard' }"
        />
        <v-list-item
          v-if="projectId"
          prepend-icon="$formatListBulleted"
          title="Objectives"
          :to="{ name: 'project', params: { projectId } }"
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
          v-if="projectId && objectiveId"
          prepend-icon="$bookOpenVariant"
          title="Overview"
          :to="{ name: 'objective', params: { projectId, objectiveId } }"
        />
        <v-list-item
          v-if="projectId && objectiveId"
          prepend-icon="$sourceBranch"
          title="Atomicity"
          :to="{ name: 'objective-atomic', params: { projectId, objectiveId } }"
        />
        <v-list-item
          v-if="projectId && objectiveId"
          prepend-icon="$rocketLaunch"
          title="Promotion"
          :to="{ name: 'objective-promotion', params: { projectId, objectiveId } }"
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
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()

const projectId = computed(() => {
  const raw = route.params.projectId
  return typeof raw === 'string' ? raw : ''
})

const objectiveId = computed(() => {
  const raw = route.params.objectiveId
  return typeof raw === 'string' ? raw : ''
})
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

.app-main {
  min-height: 100vh;
  background:
    radial-gradient(circle at top right, rgba(179, 92, 46, 0.12), transparent 28%),
    radial-gradient(circle at left center, rgba(95, 118, 80, 0.12), transparent 22%),
    rgb(var(--v-theme-background));
}
</style>

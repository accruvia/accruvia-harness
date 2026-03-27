<template>
  <div class="section-nav">
    <router-link
      v-for="item in items"
      :key="item.name"
      :to="{ name: item.name, params: { projectId } }"
      class="section-link"
      :class="{ active: route.name === item.name }"
    >
      <v-icon size="x-small">{{ item.icon }}</v-icon>
      <span>{{ item.label }}</span>
    </router-link>
  </div>
</template>

<script setup lang="ts">
import { useRoute } from 'vue-router'

defineProps<{ projectId: string }>()

const route = useRoute()

const items = [
  { name: 'project', label: 'Objectives', icon: '$formatListBulleted' },
  { name: 'project-settings', label: 'Settings', icon: '$cogOutline' },
  { name: 'project-token-performance', label: 'Token Performance', icon: '$chartBox' },
]
</script>

<style scoped>
.section-nav {
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem;
  margin-top: 1rem;
}

.section-link {
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  padding: 0.55rem 0.9rem;
  border: 1px solid rgba(179, 92, 46, 0.14);
  border-radius: 999px;
  background: rgba(255, 251, 245, 0.88);
  color: rgb(var(--v-theme-on-surface));
  text-decoration: none;
  font-size: 0.88rem;
  font-weight: 600;
}

.section-link.active {
  background: rgb(var(--v-theme-primary));
  border-color: rgb(var(--v-theme-primary));
  color: rgb(var(--v-theme-on-primary));
}
</style>

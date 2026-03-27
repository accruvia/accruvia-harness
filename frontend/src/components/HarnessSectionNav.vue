<template>
  <div class="harness-nav">
    <v-btn
      size="small"
      :variant="route.name === 'harness-objectives' ? 'flat' : 'tonal'"
      :color="route.name === 'harness-objectives' ? 'primary' : 'surface'"
      prepend-icon="$formatListBulleted"
      :to="{ name: 'harness-objectives' }"
    >
      Objectives
    </v-btn>
    <v-btn
      size="small"
      :variant="route.name === 'harness-atomicity' ? 'flat' : 'tonal'"
      :color="route.name === 'harness-atomicity' ? 'primary' : 'surface'"
      prepend-icon="$sourceBranch"
      :to="{ name: 'harness-atomicity' }"
    >
      Atomicity
    </v-btn>
    <v-btn
      size="small"
      :variant="route.name === 'harness-promotion' ? 'flat' : 'tonal'"
      :color="route.name === 'harness-promotion' ? 'primary' : 'surface'"
      prepend-icon="$rocketLaunch"
      :to="{ name: 'harness-promotion' }"
    >
      Promotion
    </v-btn>
  </div>
</template>

<script setup lang="ts">
import { useRoute } from 'vue-router'

const route = useRoute()
</script>

<style scoped>
.harness-nav {
  display: flex;
  flex-wrap: wrap;
  gap: 0.75rem;
}
</style>

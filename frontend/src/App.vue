<template>
  <v-app>
    <v-navigation-drawer permanent rail color="surface" class="border-0">
      <v-list density="compact" nav>
        <v-list-item
          prepend-icon="mdi-view-dashboard"
          title="Dashboard"
          :to="{ name: 'dashboard' }"
        />
        <v-list-item
          prepend-icon="mdi-book-open-variant"
          title="Docs"
          href="/docs"
          target="_blank"
        />
      </v-list>
    </v-navigation-drawer>

    <v-main class="bg-background">
      <router-view />
    </v-main>
  </v-app>
</template>

import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/',
    name: 'harness-objectives',
    component: () => import('../views/HarnessObjectivesView.vue'),
  },
  {
    path: '/dashboard',
    name: 'dashboard',
    component: () => import('../views/DashboardView.vue'),
  },
  {
    path: '/atomicity',
    name: 'harness-atomicity',
    component: () => import('../views/HarnessAtomicityView.vue'),
  },
  {
    path: '/promotion',
    name: 'harness-promotion',
    component: () => import('../views/HarnessPromotionView.vue'),
  },
  {
    path: '/projects/:projectId',
    alias: '/projects/:projectId/objectives',
    name: 'project',
    component: () => import('../views/ProjectView.vue'),
    props: true,
  },
  {
    path: '/projects/:projectId/settings',
    name: 'project-settings',
    component: () => import('../views/SettingsView.vue'),
    props: true,
  },
  {
    path: '/projects/:projectId/token-performance',
    name: 'project-token-performance',
    component: () => import('../views/TokenPerformanceView.vue'),
    props: true,
  },
  {
    path: '/projects/:projectId/objectives/:objectiveId',
    name: 'objective',
    component: () => import('../views/ObjectiveView.vue'),
    props: true,
  },
  {
    path: '/projects/:projectId/objectives/:objectiveId/atomic',
    name: 'objective-atomic',
    component: () => import('../views/AtomicityView.vue'),
    props: true,
  },
  {
    path: '/projects/:projectId/objectives/:objectiveId/promotion',
    name: 'objective-promotion',
    component: () => import('../views/PromotionView.vue'),
    props: true,
  },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})

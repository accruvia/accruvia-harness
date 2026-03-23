import { createRouter, createWebHistory } from 'vue-router'

const routes = [
  {
    path: '/',
    name: 'dashboard',
    component: () => import('../views/DashboardView.vue'),
  },
  {
    path: '/projects/:projectId',
    name: 'project',
    component: () => import('../views/ProjectView.vue'),
    props: true,
  },
  {
    path: '/projects/:projectId/objectives/:objectiveId',
    name: 'objective',
    component: () => import('../views/ObjectiveView.vue'),
    props: true,
  },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})

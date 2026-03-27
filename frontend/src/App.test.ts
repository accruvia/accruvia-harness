import { reactive, nextTick } from 'vue'
import { describe, expect, it, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'

const mockRoute = reactive<{ params: Record<string, unknown> }>({ params: {} })

vi.mock('vue-router', () => ({
  useRoute: () => mockRoute,
}))

import App from './App.vue'

function flushPromises() {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

const globalStubs = {
  'v-app': { template: '<div><slot /></div>' },
  'v-navigation-drawer': { template: '<aside><slot /></aside>' },
  'v-list': { template: '<div><slot /></div>' },
  'v-list-item': {
    props: ['title'],
    template: '<div class="nav-item">{{ title }}</div>',
  },
  'v-main': { template: '<main><slot /></main>' },
  'router-link': {
    props: ['to'],
    template: '<a><slot /></a>',
  },
  'router-view': {
    template: '<div />',
  },
}

describe('App shell context', () => {
  beforeEach(() => {
    mockRoute.params = {}
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('uses persisted project and objective context when route params are absent', async () => {
    localStorage.setItem('accruvia:last-project-id', 'project_1')
    localStorage.setItem('accruvia:last-objective-id', 'objective_9')
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          project: { name: 'accruvia-harness' },
          objectives: [{ id: 'objective_9', title: 'Refactor task execution pipeline' }],
        }),
      }),
    )

    const wrapper = mount(App, { global: { stubs: globalStubs } })
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('accruvia-harness')
    expect(wrapper.text()).toContain('Refactor task execution pipeline')
    expect(wrapper.text()).toContain('Settings')
    expect(wrapper.text()).toContain('Token Performance')
    expect(wrapper.text()).toContain('Project Objectives')
  })

  it('prefers current route params over persisted context', async () => {
    localStorage.setItem('accruvia:last-project-id', 'project_old')
    localStorage.setItem('accruvia:last-objective-id', 'objective_old')
    mockRoute.params = { projectId: 'project_live', objectiveId: 'objective_live' }
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          project: { name: 'live-project' },
          objectives: [{ id: 'objective_live', title: 'Live objective' }],
        }),
      }),
    )

    const wrapper = mount(App, { global: { stubs: globalStubs } })
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('live-project')
    expect(wrapper.text()).toContain('Live objective')
    expect(wrapper.text()).not.toContain('objective_old')
  })

  it('updates visible context when a cross-view context-change event is dispatched', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          project: { name: 'accruvia-harness' },
          objectives: [{ id: 'objective_new', title: 'Context Management' }],
        }),
      }),
    )

    const wrapper = mount(App, { global: { stubs: globalStubs } })
    window.dispatchEvent(
      new CustomEvent('accruvia-context-change', {
        detail: { projectId: 'project_a2c2a3c8a1c0', objectiveId: 'objective_new' },
      }),
    )
    await flushPromises()
    await nextTick()

    expect(localStorage.getItem('accruvia:last-project-id')).toBe('project_a2c2a3c8a1c0')
    expect(localStorage.getItem('accruvia:last-objective-id')).toBe('objective_new')
    expect(wrapper.text()).toContain('Context Management')
  })

  it('falls back to raw ids when the summary fetch fails', async () => {
    localStorage.setItem('accruvia:last-project-id', 'project_fallback')
    localStorage.setItem('accruvia:last-objective-id', 'objective_fallback')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 500, statusText: 'Boom' }))

    const wrapper = mount(App, { global: { stubs: globalStubs } })
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('project_fallback')
    expect(wrapper.text()).toContain('objective_fallback')
  })

  it('does not render project context when no current or persisted project exists', async () => {
    vi.stubGlobal('fetch', vi.fn())

    const wrapper = mount(App, { global: { stubs: globalStubs } })
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).not.toContain('Change project')
    expect(wrapper.text()).not.toContain('Settings')
  })

  it('clears the objective label when only project context is available', async () => {
    localStorage.setItem('accruvia:last-project-id', 'project_only')
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          project: { name: 'project-only-name' },
          objectives: [],
        }),
      }),
    )

    const wrapper = mount(App, { global: { stubs: globalStubs } })
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('project-only-name')
    const contextLabels = wrapper.findAll('.context-label').map((node) => node.text())
    expect(contextLabels).toEqual(['Project'])
  })

  it('falls back to ids when summary payload lacks matching labels and ignores malformed context events', async () => {
    localStorage.setItem('accruvia:last-project-id', 'project_seed')
    localStorage.setItem('accruvia:last-objective-id', 'objective_seed')
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({
          project: {},
          objectives: [],
        }),
      }),
    )

    const wrapper = mount(App, { global: { stubs: globalStubs } })
    window.dispatchEvent(
      new CustomEvent('accruvia-context-change', {
        detail: { projectId: 123, objectiveId: null },
      }),
    )
    await flushPromises()
    await nextTick()

    expect(wrapper.text()).toContain('project_seed')
    expect(wrapper.text()).toContain('objective_seed')
  })
})

import { ref, type Ref } from 'vue'

const BASE = ''

export function useApi<T>(url: string) {
  const data: Ref<T | null> = ref(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function fetch() {
    loading.value = true
    error.value = null
    try {
      const res = await globalThis.fetch(`${BASE}${url}`)
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
      data.value = await res.json()
      return data.value
    } catch (e: any) {
      error.value = e.message
      return null
    } finally {
      loading.value = false
    }
  }

  return { data, loading, error, fetch }
}

export async function post(url: string, body?: object) {
  const res = await globalThis.fetch(`${BASE}${url}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
  return res.json()
}

export async function put(url: string, body: object) {
  const res = await globalThis.fetch(`${BASE}${url}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return res.json()
}

export function useSSE(onEvent: (data: string) => void) {
  let source: EventSource | null = null

  function connect() {
    source = new EventSource(`${BASE}/api/events`)
    source.onmessage = (e) => onEvent(e.data)
    source.onerror = () => {
      source?.close()
      setTimeout(connect, 3000)
    }
  }

  function disconnect() {
    source?.close()
    source = null
  }

  return { connect, disconnect }
}

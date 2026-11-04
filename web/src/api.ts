/** Unified fetch wrapper that injects the API auth token if configured. */

const API_TOKEN: string =
  (typeof import.meta !== 'undefined' && (import.meta as any).env?.VITE_API_TOKEN) || ''

function authHeaders(): Record<string, string> {
  if (!API_TOKEN) return {}
  return { Authorization: `Bearer ${API_TOKEN}` }
}

export async function apiFetch(url: string, options: RequestInit = {}): Promise<Response> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...authHeaders(),
    ...((options.headers as Record<string, string>) || {}),
  }
  return fetch(url, { ...options, headers })
}

export async function apiPost(url: string, body?: unknown): Promise<Response> {
  return apiFetch(url, {
    method: 'POST',
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
}

export async function apiGet(url: string): Promise<Response> {
  return apiFetch(url, { method: 'GET' })
}

/**
 * Parse a JSON response, turning a non-2xx status into a thrown Error.
 *
 * The backend now returns real HTTP status codes with FastAPI's
 * `{"detail": "..."}` body instead of a 200 carrying `{"error": ...}`,
 * so callers must not treat a failed request as valid data.
 */
export async function apiJson<T = any>(res: Response): Promise<T> {
  let body: any = null
  try {
    body = await res.json()
  } catch {
    body = null
  }
  if (!res.ok) {
    const detail = body?.detail ?? body?.error ?? `请求失败 (${res.status})`
    throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
  }
  return body as T
}

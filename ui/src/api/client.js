// Empty base = same origin, which is how the nginx container serves it.
const BASE = import.meta.env.VITE_API_BASE_URL ?? ''

function qs(params) {
  const q = new URLSearchParams()
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') q.set(k, v)
  })
  const s = q.toString()
  return s ? `?${s}` : ''
}

async function request(path, options = {}) {
  const res = await fetch(`${BASE}${path}`, {
    headers: options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' },
    ...options,
  })
  const text = await res.text()
  const data = text ? JSON.parse(text) : null
  if (!res.ok) throw new Error(data?.error || `Request failed (${res.status})`)
  return data
}

export const api = {
  health: () => request('/api/health'),
  clearQuota: () => request('/api/llm/quota/clear', { method: 'POST' }),

  jobs: (params = {}) => request(`/api/jobs${qs(params)}`),
  jobsGrouped: (params = {}) => request(`/api/jobs/grouped${qs(params)}`),
  updateJob: (id, status) =>
    request(`/api/jobs/${id}`, { method: 'PATCH', body: JSON.stringify({ status }) }),
  aiSearch: (query, filters, page, page_size) =>
    request('/api/jobs/ai-search', {
      method: 'POST',
      body: JSON.stringify({ query, filters, page, page_size }),
    }),
  scoreJob: (id) => request(`/api/jobs/${id}/score`, { method: 'POST' }),
  scoreJobs: (filters) =>
    request('/api/jobs/score', { method: 'POST', body: JSON.stringify({ filters, only_unscored: true }) }),

  ats: () => request('/api/ats'),

  companies: () => request('/api/companies'),
  addCompany: (payload) =>
    request('/api/companies', { method: 'POST', body: JSON.stringify(payload) }),
  setPreferred: (id, preferred) =>
    request(`/api/companies/${id}`, { method: 'PATCH', body: JSON.stringify({ preferred }) }),
  deleteCompany: (id) => request(`/api/companies/${id}`, { method: 'DELETE' }),
  companyProfile: (id) => request(`/api/companies/${id}/profile`),

  discover: (query) =>
    request('/api/discover', { method: 'POST', body: JSON.stringify({ query }) }),
  discoveryRuns: () => request('/api/discover'),

  settings: () => request('/api/settings'),
  saveSettings: (payload) =>
    request('/api/settings', { method: 'POST', body: JSON.stringify(payload) }),
  uploadResume: (file) => {
    const fd = new FormData()
    fd.append('resume', file)
    return request('/api/settings/resume', { method: 'POST', body: fd })
  },

  rescore: () => request('/api/rescore', { method: 'POST' }),
  purgeResearch: () => request('/api/data/research', { method: 'DELETE' }),
  wipeAll: () => request('/api/data/all', { method: 'DELETE' }),
}

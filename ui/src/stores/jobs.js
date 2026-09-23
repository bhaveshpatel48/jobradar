import { defineStore } from 'pinia'
import { api } from '@/api/client'

export const useJobsStore = defineStore('jobs', {
  state: () => ({
    jobs: [],
    companies: [],
    loading: false,
    error: null,
    // null = let the server apply the saved setting.
    filters: {
      remote_only: null,
      engineering_only: null,
      company_id: '',
      status: '',
      q: '',
      sort: 'match',
    },
    ai: { query: '', active: false, loading: false, meta: null, error: null },
  }),
  actions: {
    params() {
      const p = { ...this.filters }
      Object.keys(p).forEach((k) => (p[k] === null || p[k] === '') && delete p[k])
      return p
    },
    async load() {
      if (this.ai.active) return this.runAi()
      this.loading = true
      this.error = null
      try {
        const data = await api.jobs(this.params())
        this.jobs = data.jobs
        this.adoptServerDefaults(data)
      } catch (e) {
        this.error = e.message
      } finally {
        this.loading = false
      }
    },
    adoptServerDefaults(data) {
      if (this.filters.remote_only === null) this.filters.remote_only = data.remote_only
      if (this.filters.engineering_only === null) this.filters.engineering_only = data.engineering_only
    },
    async loadCompanies() {
      try {
        this.companies = (await api.companies()).companies
      } catch {
        this.companies = []
      }
    },
    async runAi() {
      const query = this.ai.query.trim()
      if (!query) return this.clearAi()
      this.ai.active = true
      this.ai.loading = true
      this.ai.error = null
      try {
        const data = await api.aiSearch(query, this.params())
        this.jobs = data.jobs
        this.ai.meta = data
        this.adoptServerDefaults(data)
      } catch (e) {
        this.ai.error = e.message
      } finally {
        this.ai.loading = false
      }
    },
    clearAi() {
      this.ai = { query: '', active: false, loading: false, meta: null, error: null }
      return this.load()
    },
    async setStatus(job, status) {
      await api.updateJob(job.id, status)
      job.status = status
    },
  },
})

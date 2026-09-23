import { defineStore } from 'pinia'
import { api } from '@/api/client'

export const useJobsStore = defineStore('jobs', {
  state: () => ({
    jobs: [],
    loading: false,
    error: null,
    filters: { remote_only: null, status: '', q: '', sort: 'match' },
  }),
  actions: {
    async load() {
      this.loading = true
      this.error = null
      try {
        // remote_only null = let the server apply the saved setting.
        const params = { ...this.filters }
        if (params.remote_only === null) delete params.remote_only
        const data = await api.jobs(params)
        this.jobs = data.jobs
        if (this.filters.remote_only === null) this.filters.remote_only = data.remote_only
      } catch (e) {
        this.error = e.message
      } finally {
        this.loading = false
      }
    },
    async setStatus(job, status) {
      await api.updateJob(job.id, status)
      job.status = status
    },
  },
})

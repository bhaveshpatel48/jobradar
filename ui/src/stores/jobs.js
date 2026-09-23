import { defineStore } from 'pinia'
import { api } from '@/api/client'

const COMPANIES_PER_PAGE = 10
const JOBS_PER_COMPANY = 5
const POLL_MS = 8000

export const useJobsStore = defineStore('jobs', {
  state: () => ({
    groups: [],
    companies: [],
    page: 1,
    totalPages: 1,
    totalCompanies: 0,
    totalJobs: 0,
    unscoredJobs: 0,
    pendingScores: 0,
    loading: false,
    error: null,
    notice: null,
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
    scoring: {}, // job id -> 'running' | error message
    bulkQueuing: false,
    pollTimer: null,
  }),
  actions: {
    params() {
      const p = { ...this.filters }
      Object.keys(p).forEach((k) => (p[k] === null || p[k] === '') && delete p[k])
      return p
    },
    adopt(data) {
      this.groups = data.groups.map((g) => ({ ...g, nextPage: 2, loadingMore: false }))
      this.page = data.page
      this.totalPages = data.total_pages
      this.totalCompanies = data.total
      this.totalJobs = data.total_jobs
      this.unscoredJobs = data.unscored_jobs ?? 0
      this.pendingScores = data.pending_scores ?? 0
      if (this.filters.remote_only === null) this.filters.remote_only = data.remote_only
      if (this.filters.engineering_only === null) this.filters.engineering_only = data.engineering_only
    },
    async load(page = 1, { quiet = false } = {}) {
      if (this.ai.active) return this.runAi(page)
      if (!quiet) this.loading = true
      this.error = null
      try {
        this.adopt(
          await api.jobsGrouped({
            ...this.params(),
            page,
            page_size: COMPANIES_PER_PAGE,
            jobs_per_company: JOBS_PER_COMPANY,
          }),
        )
      } catch (e) {
        this.error = e.message
      } finally {
        this.loading = false
      }
      this.syncPolling()
    },
    async loadMore(group) {
      group.loadingMore = true
      try {
        const data = await api.jobs({
          ...this.params(),
          company_id: group.company_id,
          page: group.nextPage,
          page_size: JOBS_PER_COMPANY,
        })
        const seen = new Set(group.jobs.map((j) => j.id))
        group.jobs.push(...data.jobs.filter((j) => !seen.has(j.id)))
        group.nextPage += 1
        group.has_more = data.page < data.total_pages
      } catch (e) {
        this.notice = e.message
      } finally {
        group.loadingMore = false
      }
    },
    async loadCompanies() {
      try {
        this.companies = (await api.companies()).companies
      } catch {
        this.companies = []
      }
    },
    async runAi(page = 1) {
      const query = this.ai.query.trim()
      if (!query) return this.clearAi()
      this.ai.active = true
      this.ai.loading = true
      this.ai.error = null
      try {
        const data = await api.aiSearch(query, this.params(), page, COMPANIES_PER_PAGE)
        this.adopt(data)
        this.ai.meta = data
      } catch (e) {
        this.ai.error = e.message
      } finally {
        this.ai.loading = false
      }
    },
    clearAi() {
      this.ai = { query: '', active: false, loading: false, meta: null, error: null }
      return this.load(1)
    },
    replaceJob(updated) {
      for (const group of this.groups) {
        const i = group.jobs.findIndex((j) => j.id === updated.id)
        if (i !== -1) {
          // Keep AI-search annotations, which aren't part of the stored job.
          const { ai_relevance, ai_reason } = group.jobs[i]
          group.jobs[i] = { ...updated, ai_relevance, ai_reason }
        }
      }
    },
    async scoreOne(job) {
      this.scoring[job.id] = 'running'
      try {
        const { job: updated } = await api.scoreJob(job.id)
        this.replaceJob(updated)
        delete this.scoring[job.id]
        if (this.unscoredJobs > 0 && job.match_score === null) this.unscoredJobs -= 1
      } catch (e) {
        this.scoring[job.id] = e.message
      }
    },
    async scoreAll() {
      this.bulkQueuing = true
      this.notice = null
      try {
        const r = await api.scoreJobs(this.params())
        this.notice = r.queued
          ? `Queued ${r.queued} job${r.queued === 1 ? '' : 's'} for scoring. Scores appear here as they finish.`
          : 'Every matching job is already scored or waiting in the queue.'
        await this.load(this.page, { quiet: true })
      } catch (e) {
        this.notice = e.message
      } finally {
        this.bulkQueuing = false
      }
    },
    syncPolling() {
      // Refresh quietly while queued scores are outstanding, then stop.
      if (this.pendingScores > 0 && !this.ai.active) {
        if (!this.pollTimer) {
          this.pollTimer = setInterval(() => this.load(this.page, { quiet: true }), POLL_MS)
        }
      } else if (this.pollTimer) {
        clearInterval(this.pollTimer)
        this.pollTimer = null
      }
    },
    stopPolling() {
      clearInterval(this.pollTimer)
      this.pollTimer = null
    },
    async setStatus(job, status) {
      await api.updateJob(job.id, status)
      job.status = status
    },
  },
})

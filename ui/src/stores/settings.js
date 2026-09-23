import { defineStore } from 'pinia'
import { api } from '@/api/client'

export const useSettingsStore = defineStore('settings', {
  state: () => ({ data: null, loading: false, error: null }),
  actions: {
    async load() {
      this.loading = true
      try {
        this.data = await api.settings()
      } catch (e) {
        this.error = e.message
      } finally {
        this.loading = false
      }
    },
    async save(payload) {
      this.data = await api.saveSettings(payload)
      return this.data
    },
  },
})

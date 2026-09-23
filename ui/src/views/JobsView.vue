<script setup>
import { onMounted, ref, computed } from 'vue'
import { storeToRefs } from 'pinia'
import { useJobsStore } from '@/stores/jobs'
import { useSettingsStore } from '@/stores/settings'
import JobCard from '@/components/JobCard.vue'
import FilterBar from '@/components/FilterBar.vue'
import CompanyDrawer from '@/components/CompanyDrawer.vue'

const store = useJobsStore()
const settings = useSettingsStore()
const { jobs, loading, error, ai } = storeToRefs(store)
const openCompanyId = ref(null)

onMounted(async () => {
  await Promise.all([store.load(), store.loadCompanies(), settings.load()])
})

const needsProfile = computed(() => {
  const s = settings.data
  if (!s) return false
  return !s.resume_structured && !(s.interests || []).length
})
</script>

<template>
  <div>
    <p v-if="needsProfile" class="card notice">
      Jobs are unscored until you add a resume or interests.
      <RouterLink to="/settings">Set up your profile</RouterLink> to see match scores.
    </p>

    <FilterBar />

    <p v-if="loading || ai.loading" class="muted">
      {{ ai.loading ? 'AI is reading the job list…' : 'Loading jobs…' }}
    </p>
    <p v-else-if="error" class="muted">{{ error }}</p>
    <p v-else-if="!jobs.length" class="muted">
      <template v-if="ai.active">No jobs fit that request. Try wording it more broadly.</template>
      <template v-else>
        No jobs match. Track companies on the <RouterLink to="/sources">Sources</RouterLink> page.
      </template>
    </p>

    <div v-else class="stack">
      <JobCard
        v-for="job in jobs"
        :key="job.id"
        :job="job"
        @open-company="openCompanyId = $event"
        @set-status="store.setStatus"
      />
    </div>

    <CompanyDrawer
      v-if="openCompanyId"
      :company-id="openCompanyId"
      @close="openCompanyId = null"
    />
  </div>
</template>

<style scoped>
.notice { margin-bottom: 16px; border-color: var(--accent); }
</style>

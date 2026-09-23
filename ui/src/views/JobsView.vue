<script setup>
import { onMounted, onUnmounted, ref, computed } from 'vue'
import { storeToRefs } from 'pinia'
import { useJobsStore } from '@/stores/jobs'
import { useSettingsStore } from '@/stores/settings'
import FilterBar from '@/components/FilterBar.vue'
import CompanyGroup from '@/components/CompanyGroup.vue'
import PageNav from '@/components/PageNav.vue'
import CompanyDrawer from '@/components/CompanyDrawer.vue'

const store = useJobsStore()
const settings = useSettingsStore()
const {
  groups, loading, error, ai, notice, page, totalPages,
  unscoredJobs, pendingScores, bulkQueuing,
} = storeToRefs(store)
const openCompanyId = ref(null)

onMounted(async () => {
  await Promise.all([store.load(1), store.loadCompanies(), settings.load()])
})
onUnmounted(() => store.stopPolling())

const needsProfile = computed(() => {
  const s = settings.data
  if (!s) return false
  return !s.resume_structured && !(s.interests || []).length
})

function scoreAll() {
  const n = unscoredJobs.value
  const ok = confirm(
    `Check fit for ${n} unscored job${n === 1 ? '' : 's'} matching your current filters?\n\n` +
      'Each job uses one Gemini request. On the free tier that is 20 per day, ' +
      'so large batches finish over several days. You can keep browsing meanwhile.',
  )
  if (ok) store.scoreAll()
}

async function goTo(p) {
  await store.load(p)
  window.scrollTo({ top: 0, behavior: 'smooth' })
}
</script>

<template>
  <div>
    <p v-if="needsProfile" class="card notice">
      Jobs are unscored until you add a resume or interests.
      <RouterLink to="/settings">Set up your profile</RouterLink> to see match scores.
    </p>

    <FilterBar />

    <div v-if="!ai.active && (unscoredJobs || pendingScores)" class="bulk row">
      <button
        v-if="unscoredJobs"
        class="primary"
        :disabled="bulkQueuing || needsProfile"
        @click="scoreAll"
      >
        {{ bulkQueuing ? 'Queuing…' : `Check fit for all ${unscoredJobs} unscored` }}
      </button>
      <span v-if="pendingScores" class="muted small">
        {{ pendingScores }} waiting in the scoring queue — this page refreshes as they finish.
      </span>
    </div>
    <p v-if="notice" class="card notice">{{ notice }}</p>

    <p v-if="loading || ai.loading" class="muted">
      {{ ai.loading ? 'AI is reading the job list…' : 'Loading jobs…' }}
    </p>
    <p v-else-if="error" class="muted">{{ error }}</p>
    <p v-else-if="!groups.length" class="muted">
      <template v-if="ai.active">No jobs fit that request. Try wording it more broadly.</template>
      <template v-else>
        No jobs match. Track companies on the <RouterLink to="/sources">Sources</RouterLink> page.
      </template>
    </p>

    <template v-else>
      <div class="groups">
        <CompanyGroup
          v-for="group in groups"
          :key="group.company_id"
          :group="group"
          @open-company="openCompanyId = $event"
        />
      </div>
      <PageNav :page="page" :total-pages="totalPages" :disabled="loading" class="pages" @go="goTo" />
    </template>

    <CompanyDrawer
      v-if="openCompanyId"
      :company-id="openCompanyId"
      @close="openCompanyId = null"
    />
  </div>
</template>

<style scoped>
.notice { margin-bottom: 16px; border-color: var(--accent); }
.bulk { margin-bottom: 16px; }
.small { font-size: 13px; }
.groups { display: flex; flex-direction: column; gap: 28px; }
.pages { margin-top: 24px; }
</style>

<script setup>
import { computed } from 'vue'
import { storeToRefs } from 'pinia'
import { useJobsStore } from '@/stores/jobs'

const store = useJobsStore()
const { filters, companies, ai, totalJobs, totalCompanies } = storeToRefs(store)

let debounce
function update(key, value, { immediate = true } = {}) {
  filters.value[key] = value
  clearTimeout(debounce)
  // Any filter change starts over at page 1.
  debounce = setTimeout(() => store.load(1), immediate ? 0 : 300)
}

const sortedCompanies = computed(() =>
  [...companies.value].sort((a, b) => a.name.localeCompare(b.name)),
)

const aiSummary = computed(() => {
  const m = ai.value.meta
  if (!m) return ''
  let s = `AI kept ${m.total_jobs} of ${m.judged} jobs`
  if (m.truncated) s += ' (first 400 judged — narrow the filters for more)'
  if (m.partial) s += ' · some batches hit the rate limit, retry for the rest'
  return s
})
</script>

<template>
  <div class="card bar-wrap">
    <form class="ai row" @submit.prevent="store.runAi(1)">
      <input
        v-model="ai.query"
        class="grow"
        maxlength="500"
        placeholder="Ask AI: e.g. “senior backend roles in Go or Rust working on distributed systems”"
      />
      <button class="primary" type="submit" :disabled="ai.loading || !ai.query.trim()">
        {{ ai.loading ? 'Thinking…' : 'Ask AI' }}
      </button>
      <button v-if="ai.active" type="button" @click="store.clearAi()">Clear</button>
    </form>
    <p v-if="ai.error" class="ai-note err">{{ ai.error }}</p>
    <p v-else-if="ai.active && aiSummary" class="ai-note muted">{{ aiSummary }}</p>

    <div class="bar">
      <input
        :value="filters.q"
        placeholder="Keyword filter"
        class="search"
        @input="update('q', $event.target.value, { immediate: false })"
      />
      <select :value="filters.company_id" @change="update('company_id', $event.target.value)">
        <option value="">All companies</option>
        <option v-for="c in sortedCompanies" :key="c.id" :value="String(c.id)">
          {{ c.name }}{{ c.preferred ? '' : ' (discovered)' }}
        </option>
      </select>
      <select :value="filters.status" @change="update('status', $event.target.value)">
        <option value="">All statuses</option>
        <option value="new">New</option>
        <option value="saved">Saved</option>
        <option value="applied">Applied</option>
        <option value="dismissed">Dismissed</option>
      </select>
      <select :value="filters.sort" @change="update('sort', $event.target.value)">
        <option value="match">Best match</option>
        <option value="newest">Newest</option>
      </select>
      <label class="toggle">
        <input
          type="checkbox"
          :checked="filters.engineering_only"
          @change="update('engineering_only', $event.target.checked)"
        />
        Engineering only
      </label>
      <label class="toggle">
        <input
          type="checkbox"
          :checked="filters.remote_only"
          @change="update('remote_only', $event.target.checked)"
        />
        Remote only
      </label>
      <span class="muted count">{{ totalJobs }} jobs · {{ totalCompanies }} companies</span>
    </div>
  </div>
</template>

<style scoped>
.bar-wrap { margin-bottom: 16px; display: flex; flex-direction: column; gap: 10px; }
.ai { flex-wrap: nowrap; }
.grow { flex: 1; }
.ai-note { margin: -2px 0 0; font-size: 13px; }
.err { color: var(--bad); }
.bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.search { flex: 1 1 180px; }
.bar select { width: auto; max-width: 220px; }
.toggle { display: flex; align-items: center; gap: 6px; font-size: 14px; white-space: nowrap; }
.toggle input { width: auto; }
.count { margin-left: auto; font-size: 13px; }
@media (max-width: 640px) {
  .ai { flex-wrap: wrap; }
  .bar select { max-width: none; flex: 1 1 140px; }
}
</style>

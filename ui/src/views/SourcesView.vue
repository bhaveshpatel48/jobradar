<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { api } from '@/api/client'
import { useSettingsStore } from '@/stores/settings'

const settings = useSettingsStore()
const platforms = ref([])
const companies = ref([])
const runs = ref([])
const message = ref('')
const busy = ref(false)
const addUrl = ref('')
const showManual = ref(false)
const manual = reactive({ name: '', platform: 'greenhouse', board_token: '', website: '' })
const discoverQuery = ref('')
const companyFilter = ref('all')
let poll

const scope = computed(() => settings.data?.scrape_scope || 'preferred')
const activeRun = computed(() => runs.value.find((r) => ['pending', 'running'].includes(r.status)))
const latestRun = computed(() => runs.value[0])
const visibleCompanies = computed(() =>
  companies.value.filter((c) =>
    companyFilter.value === 'all'
      ? true
      : companyFilter.value === 'preferred'
        ? c.preferred
        : !c.preferred,
  ),
)
const preferredCount = computed(() => companies.value.filter((c) => c.preferred).length)

async function refresh() {
  const [ats, comps, disc] = await Promise.all([api.ats(), api.companies(), api.discoveryRuns()])
  platforms.value = ats.platforms
  companies.value = comps.companies
  runs.value = disc.runs
}

function schedulePoll() {
  clearTimeout(poll)
  poll = setTimeout(async () => {
    await refresh().catch(() => {})
    schedulePoll()
  }, activeRun.value ? 3000 : 20000)
}

onMounted(async () => {
  await Promise.all([settings.load(), refresh()])
  schedulePoll()
})
onUnmounted(() => clearTimeout(poll))

async function act(fn, ok) {
  busy.value = true
  message.value = ''
  try {
    const result = await fn()
    message.value = typeof ok === 'function' ? ok(result) : ok
    await refresh()
  } catch (e) {
    message.value = e.message
  } finally {
    busy.value = false
  }
}

const setScope = (value) =>
  act(
    () => settings.save({ scrape_scope: value }),
    value === 'all'
      ? 'Scraping all companies, including discovered ones.'
      : 'Scraping only your preferred companies.',
  )

const addByUrl = () =>
  act(async () => {
    const r = await api.addCompany({ url: addUrl.value.trim() })
    addUrl.value = ''
    return r
  }, (r) => `Added ${r.name} (${r.platform}). It will be scraped within a few seconds.`)

const addManual = () =>
  act(async () => {
    const r = await api.addCompany({ ...manual })
    Object.assign(manual, { name: '', platform: 'greenhouse', board_token: '', website: '' })
    return r
  }, (r) => `Added ${r.name}.`)

const togglePreferred = (c) =>
  act(() => api.setPreferred(c.id, !c.preferred), c.preferred ? `${c.name} unstarred.` : `${c.name} starred.`)

function remove(c) {
  if (!confirm(`Stop tracking ${c.name} and delete its ${c.jobs} jobs?`)) return
  act(() => api.deleteCompany(c.id), `${c.name} removed.`)
}

const discover = () =>
  act(async () => {
    await api.discover(discoverQuery.value.trim())
    schedulePoll()
  }, 'Discovery started — candidates are verified against their real job boards.')

function ago(ts) {
  if (!ts) return 'never'
  const mins = Math.round((Date.now() / 1000 - ts) / 60)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.round(mins / 60)
  return hrs < 48 ? `${hrs}h ago` : `${Math.round(hrs / 24)}d ago`
}
</script>

<template>
  <div class="stack">
    <p v-if="message" class="card notice">{{ message }}</p>

    <section class="card stack">
      <h2>What to scrape</h2>
      <div class="scope">
        <label :class="{ on: scope === 'preferred' }">
          <input type="radio" :checked="scope === 'preferred'" :disabled="busy" @change="setScope('preferred')" />
          <span>
            <strong>Preferred companies only</strong>
            <span class="muted">{{ preferredCount }} starred</span>
          </span>
        </label>
        <label :class="{ on: scope === 'all' }">
          <input type="radio" :checked="scope === 'all'" :disabled="busy" @change="setScope('all')" />
          <span>
            <strong>All companies</strong>
            <span class="muted">{{ companies.length }} tracked, including AI-discovered</span>
          </span>
        </label>
      </div>
    </section>

    <section class="card stack">
      <h2>Job boards JobRadar scrapes</h2>
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>System</th><th>How it's read</th><th class="num">Companies</th><th class="num">Eng. jobs</th></tr>
          </thead>
          <tbody>
            <tr v-for="p in platforms" :key="p.key">
              <td>
                <strong>{{ p.name }}</strong>
                <div class="muted small">{{ p.example }}</div>
              </td>
              <td class="small">{{ p.method }}</td>
              <td class="num">{{ p.companies }}</td>
              <td class="num">{{ p.engineering_jobs }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="card stack">
      <h2>Discover companies with AI</h2>
      <p class="muted small">
        Gemini suggests companies from your preferred domains and interests. Each one is kept only
        if JobRadar can reach a real job board with open roles. Discovered companies are unstarred,
        so star the ones you care about.
      </p>
      <form class="row" @submit.prevent="discover">
        <input v-model="discoverQuery" class="grow" maxlength="300" placeholder="Optional focus, e.g. “developer tools startups in Europe”" />
        <button class="primary" :disabled="busy || !!activeRun">
          {{ activeRun ? 'Discovering…' : 'Discover' }}
        </button>
      </form>
      <div v-if="latestRun" class="run">
        <p class="small">
          <strong>Last run:</strong> {{ latestRun.status }}
          <template v-if="latestRun.status === 'done'"> · {{ latestRun.added }} of {{ latestRun.candidates }} candidates verified and added</template>
          <span v-if="latestRun.error" class="err"> · {{ latestRun.error }}</span>
        </p>
        <ul v-if="latestRun.log?.length" class="log">
          <li v-for="(line, i) in latestRun.log" :key="i">{{ line }}</li>
        </ul>
      </div>
    </section>

    <section class="card stack">
      <h2>Add a company</h2>
      <form class="row" @submit.prevent="addByUrl">
        <input
          v-model="addUrl"
          class="grow"
          placeholder="Paste a careers or job-board URL, e.g. https://jobs.ashbyhq.com/linear"
        />
        <button class="primary" :disabled="busy || !addUrl.trim()">Detect & add</button>
      </form>
      <button class="link" @click="showManual = !showManual">
        {{ showManual ? 'Hide manual entry' : 'Or enter the platform and board token manually' }}
      </button>
      <form v-if="showManual" class="grid-2" @submit.prevent="addManual">
        <div class="field"><label>Name</label><input v-model="manual.name" /></div>
        <div class="field">
          <label>Platform</label>
          <select v-model="manual.platform">
            <option v-for="p in platforms" :key="p.key" :value="p.key">{{ p.name }}</option>
          </select>
        </div>
        <div class="field"><label>Board token</label><input v-model="manual.board_token" /></div>
        <div class="field"><label>Website (optional)</label><input v-model="manual.website" /></div>
        <div><button :disabled="busy || !manual.name || !manual.board_token">Add</button></div>
      </form>
    </section>

    <section class="card stack">
      <div class="row between">
        <h2>Tracked companies</h2>
        <select v-model="companyFilter" class="narrow">
          <option value="all">All ({{ companies.length }})</option>
          <option value="preferred">Preferred</option>
          <option value="discovered">Not starred</option>
        </select>
      </div>
      <p v-if="!visibleCompanies.length" class="muted">No companies here yet.</p>
      <ul v-else class="companies">
        <li v-for="c in visibleCompanies" :key="c.id">
          <button
            class="star"
            :class="{ on: c.preferred }"
            :title="c.preferred ? 'Preferred — click to unstar' : 'Star as preferred'"
            :disabled="busy"
            @click="togglePreferred(c)"
          >{{ c.preferred ? '★' : '☆' }}</button>
          <div class="grow">
            <div>
              <strong>{{ c.name }}</strong>
              <span class="badge">{{ c.platform }}</span>
              <span v-if="c.source === 'discovered'" class="badge">AI-discovered</span>
            </div>
            <div class="small muted">
              {{ c.engineering_jobs }} engineering / {{ c.jobs }} jobs · scraped {{ ago(c.last_scraped_at) }}
              <span v-if="c.last_scrape_error" class="err"> · {{ c.last_scrape_error }}</span>
            </div>
          </div>
          <button class="danger" :disabled="busy" @click="remove(c)">Remove</button>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
h2 { margin: 0; font-size: 17px; }
.notice { border-color: var(--accent); }
.small { font-size: 13px; }
.grow { flex: 1; min-width: 0; }
.between { justify-content: space-between; }
.narrow { width: auto; }
.err { color: var(--bad); }
.link {
  background: none; border: none; padding: 0; align-self: flex-start;
  color: var(--accent); text-decoration: underline; font-size: 13px;
}
.scope { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.scope label {
  display: flex; gap: 10px; align-items: flex-start; cursor: pointer;
  border: 1px solid var(--border); border-radius: var(--radius); padding: 12px;
}
.scope label.on { border-color: var(--accent); }
.scope input { width: auto; margin-top: 4px; }
.scope span { display: flex; flex-direction: column; gap: 2px; }
.table-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); vertical-align: top; }
th { color: var(--muted); font-weight: 500; font-size: 13px; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.run { background: var(--surface-2); border-radius: 8px; padding: 10px 12px; }
.run p { margin: 0; }
.log { margin: 8px 0 0; padding: 0; list-style: none; font-size: 13px; max-height: 220px; overflow-y: auto; }
.log li { padding: 2px 0; }
.companies { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
.companies li {
  display: flex; gap: 12px; align-items: center;
  padding: 10px 0; border-top: 1px solid var(--border);
}
.companies .badge { margin-left: 6px; }
.star { border: none; background: none; font-size: 20px; padding: 0 4px; color: var(--muted); }
.star.on { color: var(--warn); }
@media (max-width: 640px) {
  .scope { grid-template-columns: 1fr; }
}
</style>

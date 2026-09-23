<script setup>
import { onMounted, reactive, ref } from 'vue'
import { api } from '@/api/client'
import { useSettingsStore } from '@/stores/settings'

const store = useSettingsStore()
const form = reactive({
  interests: '',
  domains: '',
  salary_min: null,
  salary_max: null,
  salary_currency: 'USD',
  min_years_experience: null,
  max_years_experience: null,
  remote_only: true,
  engineering_only: true,
})

const resume = ref(null)
const message = ref('')
const busy = ref(false)

function toList(s) {
  return s.split(',').map((x) => x.trim()).filter(Boolean)
}

onMounted(async () => {
  await store.load()
  const d = store.data || {}
  form.interests = (d.interests || []).join(', ')
  form.domains = (d.domains || []).join(', ')
  form.salary_min = d.salary_min
  form.salary_max = d.salary_max
  form.salary_currency = d.salary_currency || 'USD'
  form.min_years_experience = d.min_years_experience
  form.max_years_experience = d.max_years_experience
  form.remote_only = Boolean(d.remote_only)
  form.engineering_only = Boolean(d.engineering_only)
  resume.value = d.resume_structured
})

async function act(fn, ok) {
  busy.value = true
  message.value = ''
  try {
    await fn()
    message.value = ok
  } catch (e) {
    message.value = e.message
  } finally {
    busy.value = false
  }
}

const save = () =>
  act(
    () =>
      store.save({
        ...form,
        interests: toList(form.interests),
        domains: toList(form.domains),
      }),
    'Settings saved.',
  )

async function onUpload(event) {
  const file = event.target.files?.[0]
  if (!file) return
  await act(async () => {
    const res = await api.uploadResume(file)
    resume.value = res.structured
    if (!res.parsed) throw new Error('Resume stored, but parsing needs a Gemini API key.')
  }, 'Resume uploaded and parsed.')
}

const rescore = () =>
  act(async () => {
    const r = await api.rescore()
    message.value = `Queued ${r.queued} jobs for re-scoring.`
  }, '')

function purge() {
  if (!confirm('Delete all company research and people data?')) return
  act(() => api.purgeResearch(), 'Research data deleted.')
}

function wipe() {
  if (!confirm('Wipe the entire database? Jobs, companies and research will be lost.')) return
  act(async () => {
    await api.wipeAll()
    resume.value = null
  }, 'Database wiped.')
}
</script>

<template>
  <div class="stack">
    <p v-if="message" class="card notice">{{ message }}</p>

    <section class="card stack">
      <h2>Resume</h2>
      <input type="file" accept=".pdf,.docx" :disabled="busy" @change="onUpload" />
      <div v-if="resume" class="preview">
        <p class="muted">Extracted (read-only):</p>
        <p v-if="resume.seniority"><strong>Seniority:</strong> {{ resume.seniority }}</p>
        <p v-if="resume.years_experience">
          <strong>Experience:</strong> {{ resume.years_experience }} yrs
        </p>
        <div v-if="resume.skills?.length" class="row">
          <span v-for="s in resume.skills" :key="s" class="badge">{{ s }}</span>
        </div>
      </div>
      <p v-else class="muted">No resume parsed yet.</p>
    </section>

    <section class="card stack">
      <h2>Preferences</h2>
      <div class="field">
        <label>Interests (comma separated)</label>
        <input v-model="form.interests" placeholder="backend, developer relations" />
      </div>
      <div class="field">
        <label>Preferred domains</label>
        <input v-model="form.domains" placeholder="B2B SaaS, fintech, devtools" />
      </div>
      <div class="grid-2">
        <div class="field">
          <label>Salary min</label>
          <input v-model.number="form.salary_min" type="number" />
        </div>
        <div class="field">
          <label>Salary max</label>
          <input v-model.number="form.salary_max" type="number" />
        </div>
        <div class="field">
          <label>Min years experience</label>
          <input v-model.number="form.min_years_experience" type="number" />
        </div>
        <div class="field">
          <label>Max years experience</label>
          <input v-model.number="form.max_years_experience" type="number" />
        </div>
      </div>
      <label class="row">
        <input v-model="form.remote_only" type="checkbox" style="width:auto" />
        Remote only
      </label>
      <label class="row">
        <input v-model="form.engineering_only" type="checkbox" style="width:auto" />
        Engineering roles only (other roles are stored but not shown or scored)
      </label>
      <div class="row">
        <button class="primary" :disabled="busy" @click="save">Save settings</button>
        <button :disabled="busy" @click="rescore">Re-score all jobs</button>
      </div>
    </section>

    <p class="muted small">
      Companies and job boards are managed on the <RouterLink to="/sources">Sources</RouterLink> page.
    </p>

    <section class="card stack">
      <h2>Data</h2>
      <p class="muted">This is personal tooling — reset it whenever you like.</p>
      <div class="row">
        <button class="danger" :disabled="busy" @click="purge">Delete research data</button>
        <button class="danger" :disabled="busy" @click="wipe">Wipe database</button>
      </div>
    </section>
  </div>
</template>

<style scoped>
h2 { margin: 0; font-size: 17px; }
.notice { border-color: var(--accent); }
.preview { background: var(--surface-2); border-radius: 8px; padding: 10px 12px; font-size: 14px; }
.preview p { margin: 0 0 6px; }
.small { font-size: 13px; margin: 0; }
</style>

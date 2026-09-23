<script setup>
import { ref, watch } from 'vue'
import { api } from '@/api/client'

const props = defineProps({ companyId: Number })
const emit = defineEmits(['close'])

const data = ref(null)
const loading = ref(false)
const error = ref(null)

watch(
  () => props.companyId,
  async (id) => {
    if (!id) return
    loading.value = true
    error.value = null
    data.value = null
    try {
      data.value = await api.companyProfile(id)
    } catch (e) {
      error.value = e.message
    } finally {
      loading.value = false
    }
  },
  { immediate: true },
)

function hostOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, '')
  } catch {
    return url
  }
}
</script>

<template>
  <div class="scrim" @click.self="emit('close')">
    <aside class="drawer">
      <div class="head">
        <h2>{{ data?.company?.name || 'Company' }}</h2>
        <button @click="emit('close')">Close</button>
      </div>

      <p v-if="loading" class="muted">Loading…</p>
      <p v-else-if="error" class="muted">{{ error }}</p>

      <template v-else-if="data">
        <p v-if="!data.profile" class="muted">
          No research yet. The agent worker fills this in after a job from this company is
          picked up.
        </p>

        <section v-else class="stack">
          <p v-if="data.profile.summary">{{ data.profile.summary }}</p>
          <p v-if="data.profile.mission" class="muted">{{ data.profile.mission }}</p>

          <dl class="facts">
            <template v-if="data.profile.funding_stage">
              <dt>Stage</dt><dd>{{ data.profile.funding_stage }}</dd>
            </template>
            <template v-if="data.profile.funding_amount">
              <dt>Raised</dt><dd>{{ data.profile.funding_amount }}</dd>
            </template>
            <template v-if="data.profile.employee_count_estimate">
              <dt>Headcount</dt><dd>{{ data.profile.employee_count_estimate }}</dd>
            </template>
          </dl>

          <div v-if="data.profile.tech_stack?.length">
            <h3>Tech</h3>
            <div class="row">
              <span v-for="t in data.profile.tech_stack" :key="t" class="badge">{{ t }}</span>
            </div>
          </div>

          <div v-if="data.profile.recent_news?.length">
            <h3>Recent news</h3>
            <ul class="news">
              <li v-for="n in data.profile.recent_news" :key="n.headline">
                <a v-if="n.url" :href="n.url" target="_blank" rel="noopener">{{ n.headline }}</a>
                <span v-else>{{ n.headline }}</span>
                <span v-if="n.date" class="muted"> · {{ n.date }}</span>
              </li>
            </ul>
          </div>

          <div v-if="data.profile.sources?.length">
            <h3>Sources</h3>
            <div class="row">
              <a
                v-for="s in data.profile.sources"
                :key="s"
                :href="s"
                target="_blank"
                rel="noopener"
                class="badge"
              >{{ hostOf(s) }}</a>
            </div>
          </div>
        </section>

        <section>
          <h3>People</h3>
          <p v-if="!data.people.length" class="muted">No people found yet.</p>
          <ul v-else class="people">
            <li v-for="p in data.people" :key="p.id">
              <div>
                <strong>{{ p.name }}</strong>
                <span v-if="p.title" class="muted"> — {{ p.title }}</span>
              </div>
              <div class="row">
                <a v-if="p.linkedin_url" :href="p.linkedin_url" target="_blank" rel="noopener">
                  LinkedIn
                </a>
                <template v-if="p.email">
                  <a :href="`mailto:${p.email}`">{{ p.email }}</a>
                  <span class="badge" :class="p.email_confidence">{{ p.email_confidence }}</span>
                </template>
              </div>
            </li>
          </ul>
          <p class="muted note">
            Emails marked <em>guessed</em> are inferred from a public pattern and are not verified.
          </p>
        </section>
      </template>
    </aside>
  </div>
</template>

<style scoped>
.scrim {
  position: fixed; inset: 0; background: rgba(0, 0, 0, 0.5);
  display: flex; justify-content: flex-end; z-index: 50;
}
.drawer {
  width: min(520px, 100%);
  background: var(--surface);
  border-left: 1px solid var(--border);
  padding: 20px;
  overflow-y: auto;
  display: flex; flex-direction: column; gap: 18px;
}
.head { display: flex; justify-content: space-between; align-items: center; }
h2 { margin: 0; font-size: 19px; }
h3 { margin: 0 0 8px; font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }
.facts { display: grid; grid-template-columns: auto 1fr; gap: 4px 14px; margin: 0; font-size: 14px; }
.facts dt { color: var(--muted); }
.facts dd { margin: 0; }
.news, .people { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 10px; }
.news { gap: 6px; font-size: 14px; }
.people li { border-top: 1px solid var(--border); padding-top: 10px; font-size: 14px; }
.note { font-size: 12px; margin-top: 12px; }
</style>

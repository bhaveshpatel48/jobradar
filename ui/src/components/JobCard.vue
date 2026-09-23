<script setup>
import { ref, computed } from 'vue'

const props = defineProps({ job: { type: Object, required: true } })
const emit = defineEmits(['open-company', 'set-status'])

const expanded = ref(false)

const REMOTE_LABELS = {
  fully_remote: ['Remote', 'remote'],
  hybrid: ['Hybrid', 'hybrid'],
  onsite: ['Onsite', 'onsite'],
  unknown: ['Location unclear', ''],
}
const remote = computed(() => REMOTE_LABELS[props.job.remote_type] || REMOTE_LABELS.unknown)
const hasReasoning = computed(() => Boolean(props.job.match_reasoning))
const concerns = computed(() => props.job.match_concerns || [])
const skills = computed(() => props.job.match_matched_skills || [])
</script>

<template>
  <article class="card job">
    <div class="head">
      <div>
        <a v-if="job.url" :href="job.url" target="_blank" rel="noopener" class="title">
          {{ job.title }}
        </a>
        <span v-else class="title">{{ job.title }}</span>
        <div class="row sub">
          <button class="link" @click="emit('open-company', job.company_id)">
            {{ job.company_name }}
          </button>
          <span v-if="job.location" class="muted">· {{ job.location }}</span>
        </div>
      </div>
      <div class="row">
        <span v-if="job.match_score !== null" class="badge score">{{ job.match_score }}% match</span>
        <span class="badge" :class="remote[1]">{{ remote[0] }}</span>
      </div>
    </div>

    <p v-if="hasReasoning" class="why">
      <button class="link" @click="expanded = !expanded">
        {{ expanded ? 'Hide' : 'Why this matches' }}
      </button>
    </p>
    <div v-if="expanded && hasReasoning" class="detail">
      <p>{{ job.match_reasoning }}</p>
      <p v-if="skills.length" class="muted"><strong>Matched:</strong> {{ skills.join(', ') }}</p>
      <ul v-if="concerns.length" class="concerns">
        <li v-for="c in concerns" :key="c">{{ c }}</li>
      </ul>
    </div>

    <div class="row actions">
      <select :value="job.status" @change="emit('set-status', job, $event.target.value)">
        <option value="new">New</option>
        <option value="saved">Saved</option>
        <option value="applied">Applied</option>
        <option value="dismissed">Dismissed</option>
      </select>
    </div>
  </article>
</template>

<style scoped>
.job { display: flex; flex-direction: column; gap: 10px; }
.head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
.title { font-weight: 600; font-size: 16px; color: var(--text); text-decoration: none; }
.title:hover { color: var(--accent); }
.sub { font-size: 13px; gap: 6px; }
.link {
  background: none; border: none; padding: 0;
  color: var(--accent); text-decoration: underline; font-size: inherit;
}
.why { margin: 0; }
.detail {
  background: var(--surface-2);
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 14px;
}
.detail p { margin: 0 0 6px; }
.concerns { margin: 6px 0 0; padding-left: 18px; color: var(--warn); }
.actions select { width: auto; }
</style>

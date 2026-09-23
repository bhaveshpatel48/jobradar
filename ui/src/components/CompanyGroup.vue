<script setup>
import { computed } from 'vue'
import { storeToRefs } from 'pinia'
import { useJobsStore } from '@/stores/jobs'
import JobCard from '@/components/JobCard.vue'

const props = defineProps({ group: { type: Object, required: true } })
const emit = defineEmits(['open-company'])

const store = useJobsStore()
const { scoring } = storeToRefs(store)

const remaining = computed(() => props.group.job_count - props.group.jobs.length)
</script>

<template>
  <section class="group">
    <header class="group-head">
      <button class="name" @click="emit('open-company', group.company_id)">
        {{ group.company_name }}
      </button>
      <span class="badge">{{ group.company_platform }}</span>
      <span class="muted small">
        {{ group.job_count }} job{{ group.job_count === 1 ? '' : 's' }}
        <template v-if="group.best_score !== null && group.best_score !== undefined">
          · best match {{ group.best_score }}%
        </template>
      </span>
    </header>

    <div class="stack">
      <JobCard
        v-for="job in group.jobs"
        :key="job.id"
        :job="job"
        :scoring="scoring[job.id] ?? null"
        :show-company="false"
        @open-company="emit('open-company', $event)"
        @set-status="store.setStatus"
        @score="store.scoreOne"
      />
    </div>

    <button
      v-if="group.has_more"
      class="more"
      :disabled="group.loadingMore"
      @click="store.loadMore(group)"
    >
      {{ group.loadingMore ? 'Loading…' : `Show more from ${group.company_name} (${remaining} left)` }}
    </button>
  </section>
</template>

<style scoped>
.group { display: flex; flex-direction: column; gap: 10px; }
.group-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; padding: 0 2px; }
.name {
  background: none; border: none; padding: 0;
  font-size: 18px; font-weight: 700; color: var(--text);
}
.name:hover { color: var(--accent); }
.small { font-size: 13px; }
.more { align-self: flex-start; }
</style>

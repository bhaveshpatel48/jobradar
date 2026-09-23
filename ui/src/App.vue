<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink, RouterView } from 'vue-router'
import { api } from '@/api/client'

const gemini = ref(null)
let timer

async function refresh() {
  try {
    gemini.value = (await api.health()).gemini
  } catch {
    gemini.value = null
  }
}

async function clearQuota() {
  await api.clearQuota()
  await refresh()
}

const resetTime = computed(() => {
  const ts = gemini.value?.quota_resets_at
  return ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''
})

onMounted(() => {
  refresh()
  timer = setInterval(refresh, 60000)
})
onUnmounted(() => clearInterval(timer))
</script>

<template>
  <header>
    <div class="container inner">
      <strong>JobRadar<span class="pro">Pro</span></strong>
      <nav>
        <RouterLink to="/">Jobs</RouterLink>
        <RouterLink to="/sources">Sources</RouterLink>
        <RouterLink to="/settings">Settings</RouterLink>
      </nav>
    </div>
  </header>
  <div v-if="gemini?.quota_exhausted" class="container banner-wrap">
    <p class="quota">
      Gemini's daily request quota is used up, so AI scoring, search and discovery are paused
      until {{ resetTime }}. Scraping continues. Raised your quota?
      <button class="link" @click="clearQuota">Retry now</button>
    </p>
  </div>
  <main class="container">
    <RouterView />
  </main>
</template>

<style scoped>
header {
  border-bottom: 1px solid var(--border);
  background: var(--surface);
  margin-bottom: 24px;
}
.inner {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-block: 14px;
}
.pro { color: var(--accent); }
nav { display: flex; gap: 16px; }
nav a { text-decoration: none; color: var(--muted); }
nav a.router-link-active { color: var(--text); font-weight: 600; }
.banner-wrap { padding-bottom: 0; }
.quota {
  margin: -8px 0 16px;
  padding: 10px 14px;
  border: 1px solid var(--warn);
  border-radius: var(--radius);
  font-size: 14px;
}
.link {
  background: none; border: none; padding: 0;
  color: var(--accent); text-decoration: underline; font-size: inherit;
}
</style>

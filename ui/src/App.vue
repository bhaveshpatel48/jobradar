<script setup>
import { RouterLink, RouterView } from 'vue-router'
</script>

<template>
  <header>
    <div class="container inner">
      <strong>JobRadar<span class="pro">Pro</span></strong>
      <nav>
        <RouterLink to="/">Jobs</RouterLink>
        <RouterLink to="/settings">Settings</RouterLink>
      </nav>
    </div>
  </header>
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
</style>

<script setup>
defineProps({
  page: { type: Number, required: true },
  totalPages: { type: Number, required: true },
  disabled: { type: Boolean, default: false },
})
const emit = defineEmits(['go'])
</script>

<template>
  <nav v-if="totalPages > 1" class="pager" aria-label="Pages">
    <button :disabled="disabled || page <= 1" @click="emit('go', 1)">First</button>
    <button :disabled="disabled || page <= 1" @click="emit('go', page - 1)">Previous</button>
    <span class="muted">Page {{ page }} of {{ totalPages }}</span>
    <button :disabled="disabled || page >= totalPages" @click="emit('go', page + 1)">Next</button>
    <button :disabled="disabled || page >= totalPages" @click="emit('go', totalPages)">Last</button>
  </nav>
</template>

<style scoped>
.pager { display: flex; align-items: center; justify-content: center; gap: 8px; flex-wrap: wrap; font-size: 14px; }
</style>

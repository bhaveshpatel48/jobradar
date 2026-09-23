<script setup>
const props = defineProps({ filters: { type: Object, required: true }, count: Number })
const emit = defineEmits(['change'])

function update(key, value) {
  props.filters[key] = value
  emit('change')
}
</script>

<template>
  <div class="card bar">
    <input
      :value="filters.q"
      placeholder="Search title or company"
      class="search"
      @input="update('q', $event.target.value)"
    />
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
        :checked="filters.remote_only"
        @change="update('remote_only', $event.target.checked)"
      />
      Remote only
    </label>
    <span class="muted count">{{ count }} jobs</span>
  </div>
</template>

<style scoped>
.bar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-bottom: 16px; }
.search { flex: 1 1 220px; }
.bar select { width: auto; }
.toggle { display: flex; align-items: center; gap: 6px; font-size: 14px; white-space: nowrap; }
.toggle input { width: auto; }
.count { margin-left: auto; font-size: 13px; }
</style>

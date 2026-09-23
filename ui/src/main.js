import { createApp } from 'vue'
import { createPinia } from 'pinia'
import { createRouter, createWebHistory } from 'vue-router'

import App from './App.vue'
import JobsView from './views/JobsView.vue'
import SettingsView from './views/SettingsView.vue'
import './style.css'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'jobs', component: JobsView },
    { path: '/settings', name: 'settings', component: SettingsView },
  ],
})

createApp(App).use(createPinia()).use(router).mount('#app')

import { createApp } from 'vue'
import { createVuetify } from 'vuetify'
import { aliases, mdi } from 'vuetify/iconsets/mdi'
import 'vuetify/styles'
import '@mdi/font/css/materialdesignicons.css'
import App from './App.vue'
import router from './plugins/router'

const accruvia = {
  dark: true,
  colors: {
    background: '#131313',
    surface: '#1c1b1b',
    'surface-bright': '#353535',
    'surface-light': '#2a2a2a',
    'surface-variant': '#42493d',
    primary: '#9dd586',
    'primary-darken-1': '#699e55',
    secondary: '#335028',
    'secondary-darken-1': '#1e3518',
    error: '#f2726a',
    info: '#7ac4e8',
    success: '#9dd586',
    warning: '#e8c76a',
    'on-background': '#e2e3dd',
    'on-surface': '#e2e3dd',
    'on-surface-variant': '#c2c9b9',
    'on-primary': '#131313',
  },
}

const vuetify = createVuetify({
  icons: { defaultSet: 'mdi', aliases, sets: { mdi } },
  theme: {
    defaultTheme: 'accruvia',
    themes: { accruvia },
  },
  defaults: {
    VCard: { rounded: 'sm', elevation: 0 },
    VBtn: { rounded: 'sm', variant: 'flat' },
    VChip: { rounded: 'sm', size: 'small' },
    VTextField: { variant: 'outlined', density: 'compact' },
  },
})

createApp(App).use(vuetify).use(router).mount('#app')

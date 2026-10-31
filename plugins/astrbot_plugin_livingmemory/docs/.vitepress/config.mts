import { defineConfig } from 'vitepress'

const repo = 'https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory'

export default defineConfig({
  title: 'LivingMemory',
  description: 'Intelligent long-term memory plugin for AstrBot',
  lang: 'en-US',
  base: process.env.DOCS_BASE || '/astrbot_plugin_livingmemory/',
  cleanUrls: true,
  lastUpdated: true,
  ignoreDeadLinks: false,
  head: [
    ['meta', { name: 'theme-color', content: '#3d7f8f' }]
  ],
  themeConfig: {
    logo: `${repo}/raw/master/logo.png`,
    siteTitle: 'LivingMemory',
    nav: navEn(),
    sidebar: sidebarEn(),
    outline: { label: 'On this page', level: [2, 3] },
    lastUpdated: {
      text: 'Last updated',
      formatOptions: { dateStyle: 'medium', timeStyle: 'short' }
    },
    docFooter: { prev: 'Previous', next: 'Next' },
    socialLinks: [{ icon: 'github', link: repo }],
    search: { provider: 'local' }
  }
})

function navEn() {
  return [
    { text: 'Guide', link: '/en/guide/getting-started' },
    { text: 'Features', link: '/en/features' },
    { text: 'Releases', link: '/en/releases' },
    { text: 'Architecture', link: '/en/architecture' },
    { text: 'GitHub', link: repo }
  ]
}

function sidebarEn() {
  return [
    {
      text: 'Get Started',
      items: [
        { text: 'Quick Start', link: '/en/guide/getting-started' },
        { text: 'Releases and Upgrades', link: '/en/releases' },
        { text: 'Configuration', link: '/en/configuration' },
        { text: 'Commands', link: '/en/commands' },
        { text: 'WebUI', link: '/en/webui' }
      ]
    },
    {
      text: 'Deep Dive',
      items: [
        { text: 'Features', link: '/en/features' },
        { text: 'Architecture', link: '/en/architecture' },
        { text: 'Docs Deployment', link: '/en/deployment' }
      ]
    }
  ]
}

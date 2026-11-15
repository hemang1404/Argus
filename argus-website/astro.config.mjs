// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import mdx from '@astrojs/mdx';

import { starlightSidebar, repoUrl } from './src/data/nav.mjs';

export default defineConfig({
  site: 'https://argus.hemangsharrma.workers.dev',
  trailingSlash: 'ignore',
  integrations: [
    starlight({
      title: 'Argus',
      description:
        'Argus is a local, test-gated code recovery runtime for LLM-generated patches. Recovery runs in a temporary copy of your project, with tiered patch reconciliation and human-approved candidates. Uses only the model provider you configure.',
      favicon: '/favicon.svg',
      pagefind: false,
      logo: {
        src: './src/assets/argus-mark.svg',
        alt: 'Argus',
      },
      social: [{ icon: 'github', label: 'GitHub', href: repoUrl }],
      editLink: {
        baseUrl: 'https://github.com/hemang1404/Argus/edit/main/',
      },
      lastUpdated: false,
      credits: false,
      pagination: true,
      tableOfContents: { minHeadingLevel: 2, maxHeadingLevel: 3 },
      customCss: ['./src/styles/theme.css', './src/styles/components.css'],
      sidebar: starlightSidebar,
      head: [
        { tag: 'meta', attrs: { name: 'theme-color', content: '#090A0D' } },
        { tag: 'meta', attrs: { name: 'color-scheme', content: 'dark' } },
        { tag: 'meta', attrs: { property: 'og:image', content: 'https://argus.hemangsharrma.workers.dev/og.png' } },
        { tag: 'meta', attrs: { property: 'og:image:width', content: '1200' } },
        { tag: 'meta', attrs: { property: 'og:image:height', content: '630' } },
        { tag: 'meta', attrs: { name: 'twitter:card', content: 'summary_large_image' } },
        { tag: 'meta', attrs: { name: 'twitter:image', content: 'https://argus.hemangsharrma.workers.dev/og.png' } },
      ],
      components: {
        Header: './src/components/Header.astro',
        Sidebar: './src/components/Sidebar.astro',
        TableOfContents: './src/components/TableOfContents.astro',
        MobileTableOfContents: './src/components/MobileTableOfContents.astro',
        MobileMenuToggle: './src/components/MobileMenuToggle.astro',
        PageTitle: './src/components/PageTitle.astro',
        Footer: './src/components/Footer.astro',
        Hero: './src/components/Hero.astro',
        ThemeProvider: './src/components/ThemeProvider.astro',
      },
    }),
    mdx(),
  ],
});

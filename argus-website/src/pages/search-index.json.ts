import type { APIRoute } from 'astro';
import { getCollection } from 'astro:content';
import { groupForHref } from '../data/nav.mjs';

/** Strip MDX/Markdown syntax down to searchable prose. */
function plainText(source: string): string {
  return source
    .replace(/^---[\s\S]*?---/m, ' ')
    .replace(/^\s*(import|export)\s.+$/gm, ' ')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/<[^>]+>/g, ' ')
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/[*_~>|]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

export const GET: APIRoute = async () => {
  const docs = await getCollection('docs');
  const entries = docs
    .filter((entry) => !entry.data.draft && !entry.id.startsWith('404'))
    .map((entry) => {
      const href = `/${entry.id.replace(/\/index$/, '')}/`;
      return {
        title: entry.data.title,
        description: entry.data.description ?? '',
        group: groupForHref(href)?.label ?? 'Documentation',
        href,
        body: plainText(entry.body ?? '').slice(0, 6000),
      };
    });

  entries.push({
    title: 'Argus documentation',
    description:
      'Local, test-gated code recovery runtime for LLM-generated patches. Isolated workspaces, tiered patch reconciliation, and human-approved candidates.',
    group: 'Overview',
    href: '/',
    body:
      'Argus documentation home. Installation quick start CLI TUI MCP server architecture security model patch reconciliation workspace isolation candidate artifacts research benchmarks changelog contributing.',
  });

  entries.sort((a, b) => a.title.localeCompare(b.title));

  return new Response(JSON.stringify(entries), {
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'public, max-age=300',
    },
  });
};

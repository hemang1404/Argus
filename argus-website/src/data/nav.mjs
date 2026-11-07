/**
 * Single source of truth for the documentation information architecture.
 * Used by the Astro/Starlight sidebar config, the sidebar component, the
 * mobile drawer, breadcrumbs, and the search index.
 */

export const version = '0.2.0b1';
export const repoUrl = 'https://github.com/hemang1404/Argus';
export const issuesUrl = 'https://github.com/hemang1404/Argus/issues';
export const releasesUrl = 'https://github.com/hemang1404/Argus/releases';

export const navGroups = [
  {
    label: 'Getting Started',
    items: [
      { label: 'Introduction', href: '/getting-started/introduction/' },
      { label: 'Installation', href: '/getting-started/installation/' },
      { label: 'Quick Start', href: '/getting-started/quick-start/' },
      { label: 'First Recovery', href: '/getting-started/first-recovery/' },
    ],
  },
  {
    label: 'Core Concepts',
    items: [
      { label: 'How Argus Works', href: '/core-concepts/how-argus-works/' },
      { label: 'Recovery Lifecycle', href: '/core-concepts/recovery-lifecycle/' },
      { label: 'Patch Reconciliation', href: '/core-concepts/patch-reconciliation/' },
      { label: 'Workspace Isolation', href: '/core-concepts/workspace-isolation/' },
      { label: 'Candidate Artifacts', href: '/core-concepts/candidate-artifacts/' },
      { label: 'Validation and Approval', href: '/core-concepts/validation-and-approval/' },
    ],
  },
  {
    label: 'Interfaces',
    items: [
      { label: 'CLI', href: '/interfaces/cli/' },
      { label: 'TUI', href: '/interfaces/tui/' },
      { label: 'MCP Server', href: '/interfaces/mcp-server/' },
      { label: 'Python API', href: '/interfaces/python-api/' },
    ],
  },
  {
    label: 'Guides',
    items: [
      { label: 'Provider Configuration', href: '/guides/provider-configuration/' },
      { label: 'Running Recoveries', href: '/guides/running-recoveries/' },
      { label: 'Reviewing Candidates', href: '/guides/reviewing-candidates/' },
      { label: 'Applying Candidates', href: '/guides/applying-candidates/' },
      { label: 'Troubleshooting', href: '/guides/troubleshooting/' },
    ],
  },
  {
    label: 'Reference',
    items: [
      { label: 'CLI Commands', href: '/reference/cli-commands/' },
      { label: 'Configuration', href: '/reference/configuration/' },
      { label: 'Environment Variables', href: '/reference/environment-variables/' },
      { label: 'Error Codes', href: '/reference/error-codes/' },
      { label: 'Supported Platforms', href: '/reference/supported-platforms/' },
    ],
  },
  {
    label: 'Project',
    items: [
      { label: 'Architecture', href: '/project/architecture/' },
      { label: 'Security Model', href: '/project/security-model/' },
      { label: 'Research and Benchmarks', href: '/project/research/' },
      { label: 'Changelog', href: '/project/changelog/' },
      { label: 'Contributing', href: '/project/contributing/' },
    ],
  },
];

export const topNav = [
  { label: 'Docs', href: '/getting-started/introduction/', key: 'docs' },
  { label: 'CLI', href: '/interfaces/cli/', key: 'cli' },
  { label: 'MCP', href: '/interfaces/mcp-server/', key: 'mcp' },
  { label: 'Architecture', href: '/project/architecture/', key: 'architecture' },
  { label: 'Research', href: '/project/research/', key: 'research' },
  { label: 'Changelog', href: '/project/changelog/', key: 'changelog' },
];

/** Starlight sidebar configuration derived from the groups above. */
export const starlightSidebar = navGroups.map((group) => ({
  label: group.label,
  items: group.items.map((item) => ({ label: item.label, link: item.href })),
}));

/** Normalize a pathname for comparisons ("/a/b" and "/a/b/" are equal). */
export function normalizePath(pathname) {
  const value = (pathname || '/').split('#')[0].split('?')[0];
  return value.length > 1 ? value.replace(/\/+$/, '') : value;
}

export function groupForHref(href) {
  return navGroups.find((group) => group.items.some((item) => item.href === href));
}

/** Top-navigation key for the current page, used for active states. */
export function topNavKey(pathname) {
  const path = normalizePath(pathname);
  if (path.startsWith('/interfaces/cli')) return 'cli';
  if (path.startsWith('/interfaces/mcp-server')) return 'mcp';
  if (path.startsWith('/project/architecture')) return 'architecture';
  if (path.startsWith('/project/research')) return 'research';
  if (path.startsWith('/project/changelog')) return 'changelog';
  return 'docs';
}

/**
 * Single source for install strings and release status shown on the site.
 *
 * Until the current version (`version` in nav.mjs) exists on PyPI, the site
 * leads with a Git install. After publishing it, set `currentOnPypi` to true;
 * the install tabs, home page and installation page then switch to the plain
 * `pip install argus-recovery` forms and the "PyPI vs repository" notes vanish.
 */
import { version, repoUrl } from './nav.mjs';

/** Flip to true once `version` has been published to PyPI. */
export const currentOnPypi = true;

/** Latest version actually on PyPI while currentOnPypi is false. */
export const pypiVersion = '0.2.0b2';

export const pypiUrl = 'https://pypi.org/project/argus-recovery/';
export const gitUrl = `git+${repoUrl}.git`;

/** Requirement spec for a package manager, with optional extras. */
export function installSpec(extras = '') {
  const name = extras ? `argus-recovery[${extras}]` : 'argus-recovery';
  return currentOnPypi ? name : `${name} @ ${gitUrl}`;
}

export { version };

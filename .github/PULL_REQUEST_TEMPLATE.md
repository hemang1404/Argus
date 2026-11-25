## Summary

<!-- Describe what this PR changes and why. Link any related issue below. -->
Fixes #

## Checklist

- [ ] Offline test suite passes (`ARGUS_OFFLINE=1 pytest -q -m "not live_provider and not live_antigravity and not real_host and not tokenizer_download"`)
- [ ] Linter passes (`ruff check .`)
- [ ] Compile check passes (`python -m compileall -q argus tests`)
- [ ] Commits follow Conventional Commits (`feat(...)`, `fix(...)`, `docs(...)`, `test(...)`) and include `Signed-off-by:` (`git commit -s`)
- [ ] New/changed behaviour has tests and matching docs (`README.md` / `CHANGELOG.md` / `argus-website/` if applicable)

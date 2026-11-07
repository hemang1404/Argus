# Test fixtures

## `flask_blueprints_7ee9ceb7.py`

Byte-identical copy of `src/flask/blueprints.py` from
[`pallets/flask`](https://github.com/pallets/flask) at commit
`7ee9ceb71e868944a46e1ff00b506772a53a4f1d`.

- SHA-256: `9d296c3a0dffffbafd59ef15290a6d4c2061a470f3ad2b00709388d35d1dddc0`
- Source: `git show 7ee9ceb71e868944a46e1ff00b506772a53a4f1d:src/flask/blueprints.py`
- License: BSD-3-Clause, Copyright 2010 Pallets. The file is retained unmodified
  because the anchor-matching tests assert exact line numbers (172, 170–209) and
  anchor uniqueness against this revision. Do not reformat or add a header.

These tests previously required a local checkout at `data/repos/pallets__flask`,
which is gitignored and absent from clean clones. Vendoring the pinned file makes
the suite self-contained and reproducible offline.

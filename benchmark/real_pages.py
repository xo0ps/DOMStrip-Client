"""Token-cost benchmark on real, previously fetched web pages.

Why this exists
---------------
`benchmark_cleaner.py` runs over `tests/fixtures/`, which are small synthetic
pages written for the test suite (100 files, ~27 KB total, largest 1.2 KB). They
prove the cleaner is correct, but they cannot support a claim about real-world
token savings: real pages are 100-500 KB and carry far more boilerplate.

This script measures on pages actually served by the web. It keeps a local
cache directory so repeated runs are fast and offline, and ships
``results-o200k.json`` with the exact per-page token counts behind the
published figures. The raw HTML itself is not committed: 1038 pages is 211 MB
uncompressed, which does not belong in a git repository.

To reproduce, run the script; it fetches any page not already cached and, with
``--verify``, asserts that expected content survived. Expect tiny differences
from the published numbers if a source page has changed since the manifest was
generated.

What it measures
----------------
For each page, two token counts under one tokenizer:

* ``html`` - the raw response body, markup and all
* ``md``   - what the cleaner returns

The headline metric is ``reduction = 1 - md/html``: what an agent saves by not
being handed raw markup. That is the question callers actually have, and it is
the only comparison this benchmark draws.

Why there is no "vs visible text" baseline
------------------------------------------
Earlier versions also compared against `text_content()`. That baseline is not
meaningful and was removed:

* It has no stable denominator. A page whose content is one 14,000-row table
  has a nearly flat `text_content()` but a large Markdown table, producing
  readings like -458% that describe the metric, not the tool.
* It mixes markup cost with content selection. When extraction keeps nav and
  footer the gap is boilerplate; when it keeps a whole RFC the gap is the
  document. The same number meant opposite things.

Content selection is checked directly instead: ``--verify`` asserts that
required substrings from each page survive in the Markdown. A cheaper output
that dropped the document fails the run rather than scoring well.

Usage
-----
    python real_pages.py                 # fetch what is missing, then report
    python real_pages.py --refresh       # re-fetch every URL
    python real_pages.py --verify        # also assert content survives
    python real_pages.py --json out.json # machine-readable results

Requires the packages in requirements.txt.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

# The extraction code is vendored in this directory so the benchmark runs from a
# clean clone with nothing installed. domstrip_extract is a verbatim copy of the
# cleaner and error models used in production; see domstrip_extract/README.md.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from domstrip_extract.cleaner import clean  # noqa: E402
from domstrip_extract.errors import AppError  # noqa: E402

DEFAULT_CACHE = _HERE / ".corpus"
DEFAULT_TOKENIZER = "tiktoken:o200k_base"

# A page set chosen to span the shapes people actually feed to agents, not to
# flatter the tool. `expect` lists substrings that must survive extraction;
# they are checked with --verify. Keep them short and distinctive so a rename on
# the source page fails loudly instead of silently passing.
SOURCES: list[dict[str, object]] = [
    {
        "url": "https://angular.dev/",
        "label": "angular-dev",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.hashicorp.com/",
        "label": "developer-hashicorp-com",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/",
        "label": "developer-mozilla-org-en-us",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://discord.com/invite/svelte",
        "label": "discord-com-invite-svelte",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://doc.rust-lang.org/book/",
        "label": "doc-rust-lang-org-book",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.docker.com/accounts/",
        "label": "docs-docker-com-accounts",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.aiohttp.org/en/stable/",
        "label": "docs-aiohttp-org-en-stable",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astral.sh/ruff/",
        "label": "docs-astral-sh-ruff",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astro.build/ar/getting-started/",
        "label": "docs-astro-build-ar-getting-started",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.celeryq.dev/en/main/index.html",
        "label": "docs-celeryq-dev-en-main-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.djangoproject.com/el/6.1/",
        "label": "docs-djangoproject-com-el-6-1",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.docker.com/",
        "label": "docs-docker-com",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.gitlab.com/",
        "label": "docs-gitlab-com",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.kernel.org/",
        "label": "docs-kernel-org",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.npmjs.com/",
        "label": "docs-npmjs-com",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/stable/getting-started.html",
        "label": "docs-pytest-org-en-stable-getting-started",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.python.org/2.6/",
        "label": "docs-python-org-2-6",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.ros.org",
        "label": "docs-ros-org",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.sqlalchemy.org/en/21/",
        "label": "docs-sqlalchemy-org-en-21",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://expressjs.com/",
        "label": "expressjs-com",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://fastapi.tiangolo.com/",
        "label": "fastapi-tiangolo-com",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://flask.palletsprojects.com/en/stable/",
        "label": "flask-palletsprojects-com-en-stable",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://flask.palletsprojects.com/en/stable/api/",
        "label": "flask-palletsprojects-com-en-stable-api",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://git-scm.com/",
        "label": "git-scm-com",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://go.dev/",
        "label": "go-dev",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://jestjs.io/",
        "label": "jestjs-io",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://jinja.palletsprojects.com/en/stable/",
        "label": "jinja-palletsprojects-com-en-stable",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/docs/concepts/architecture/nodes/",
        "label": "kubernetes-io-docs-concepts-architecture-nodes",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/docs/concepts/",
        "label": "kubernetes-io-docs-concepts",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://man7.org/index.html",
        "label": "man7-org-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/API",
        "label": "developer-mozilla-org-en-us-docs-web-api",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mochajs.org/",
        "label": "mochajs-org",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mypy.readthedocs.io/en/stable/",
        "label": "mypy-readthedocs-io-en-stable",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nextjs.org/",
        "label": "nextjs-org",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nodejs.org/en/blog",
        "label": "nodejs-org-en-blog",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nodejs.org/docs/latest/api/",
        "label": "nodejs-org-docs-latest-api",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://numpy.org",
        "label": "numpy-org",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pandas.pydata.org/",
        "label": "pandas-pydata-org",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pip.pypa.io/en/stable/",
        "label": "pip-pypa-io-en-stable",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pkg.go.dev/cmd/go",
        "label": "pkg-go-dev-cmd-go",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/",
        "label": "postgresql-org",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/4.6.x/",
        "label": "docs-pytest-org-en-4-6-x",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.python.org/3.11/",
        "label": "docs-python-org-3-11",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://react.dev/",
        "label": "react-dev",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/commands/redis-8-10-commands/",
        "label": "redis-io-docs-latest-commands-redis-8-10-commands",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/",
        "label": "redis-io",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://requests.readthedocs.io/en/latest/",
        "label": "requests-readthedocs-io-en-latest",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://doc.rust-lang.org/beta/",
        "label": "doc-rust-lang-org-beta",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://setuptools.pypa.io/en/latest/",
        "label": "setuptools-pypa-io-en-latest",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://svelte.dev/",
        "label": "svelte-dev",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://tailwindcss.com/",
        "label": "tailwindcss-com",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://vite.dev/guide/",
        "label": "vite-dev-guide",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://vuejs.org/",
        "label": "vuejs-org",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://webpack.js.org/",
        "label": "webpack-js-org",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.freedesktop.org/software/realmd/",
        "label": "freedesktop-org-software-realmd",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.gnu.org/licenses/",
        "label": "gnu-org-licenses",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/about/",
        "label": "postgresql-org-about",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.sqlite.org/",
        "label": "sqlite-org",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.typescriptlang.org/",
        "label": "typescriptlang-org",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://angular.dev/ai",
        "label": "angular-dev-ai",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.hashicorp.com/terraform/cli",
        "label": "developer-hashicorp-com-terraform-cli",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web",
        "label": "developer-mozilla-org-en-us-docs-web",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://doc.rust-lang.org/book/appendix-05-editions.html",
        "label": "doc-rust-lang-org-book-appendix-05-editions",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.docker.com/accounts/individual/",
        "label": "docs-docker-com-accounts-individual",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.aiohttp.org/en/stable/client.html",
        "label": "docs-aiohttp-org-en-stable-client",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astral.sh/ruff/configuration/",
        "label": "docs-astral-sh-ruff-configuration",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astro.build/de/getting-started/",
        "label": "docs-astro-build-de-getting-started",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.celeryq.dev/en/stable/",
        "label": "docs-celeryq-dev-en-stable",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.djangoproject.com/en/1.10/",
        "label": "docs-djangoproject-com-en-1-10",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.kernel.org/admin-guide/index.html",
        "label": "docs-kernel-org-admin-guide-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.npmjs.com/about-npm-versions/",
        "label": "docs-npmjs-com-about-npm-versions",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.python.org/2.7/",
        "label": "docs-python-org-2-7",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://expressjs.com/de/",
        "label": "expressjs-com-de",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://fastapi.tiangolo.com/about/",
        "label": "fastapi-tiangolo-com-about",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://flask.palletsprojects.com/en/stable/appcontext/",
        "label": "flask-palletsprojects-com-en-stable-appcontext",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://git-scm.com/about",
        "label": "git-scm-com-about",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://go.dev/blog/",
        "label": "go-dev-blog",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://jestjs.io/blog",
        "label": "jestjs-io-blog",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/docs/concepts/cluster-administration/node-autoscaling/",
        "label": "kubernetes-io-docs-concepts-cluster-administration-node-autoscaling",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/docs/contribute/",
        "label": "kubernetes-io-docs-contribute",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://man7.org/linux/man-pages/",
        "label": "man7-org-linux-man-pages",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/API/ANGLE_instanced_arrays",
        "label": "developer-mozilla-org-en-us-docs-web-api-angle-instanced-arrays",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mochajs.org/blog/",
        "label": "mochajs-org-blog",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mypy.readthedocs.io/en/stable/builtin_types.html",
        "label": "mypy-readthedocs-io-en-stable-builtin-types",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nextjs.org/blog",
        "label": "nextjs-org-blog",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nodejs.org/en/blog/weekly/weekly-update2015-02-06",
        "label": "nodejs-org-en-blog-weekly-weekly-update2015-02-06",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://numpy.org/doc/",
        "label": "numpy-org-doc",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pandas.pydata.org/docs/",
        "label": "pandas-pydata-org-docs",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pip.pypa.io/en/stable/copyright/",
        "label": "pip-pypa-io-en-stable-copyright",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/about/casestudies/mohawk/",
        "label": "postgresql-org-about-casestudies-mohawk",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/6.2.x/",
        "label": "docs-pytest-org-en-6-2-x",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.python.org/3.12/",
        "label": "docs-python-org-3-12",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://react.dev/blog",
        "label": "react-dev-blog",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/commands/redis-8-4-commands/",
        "label": "redis-io-docs-latest-commands-redis-8-4-commands",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/",
        "label": "redis-io-docs-latest",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://requests.readthedocs.io/en/latest/api/",
        "label": "requests-readthedocs-io-en-latest-api",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://doc.rust-lang.org/nightly/",
        "label": "doc-rust-lang-org-nightly",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://setuptools.pypa.io/en/latest/build_meta.html",
        "label": "setuptools-pypa-io-en-latest-build-meta",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://svelte.dev/apps",
        "label": "svelte-dev-apps",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://tailwindcss.com/blog",
        "label": "tailwindcss-com-blog",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.hashicorp.com/boundary",
        "label": "developer-hashicorp-com-boundary",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://vuejs.org/about/coc",
        "label": "vuejs-org-about-coc",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://webpack.js.org/api/",
        "label": "webpack-js-org-api",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.freedesktop.org/wiki/",
        "label": "freedesktop-org-wiki",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.gnu.org/licenses/licenses.ar.html",
        "label": "gnu-org-licenses-licenses-ar",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/about/news/postgresql-19-rc-1-released-3409/",
        "label": "postgresql-org-about-news-postgresql-19-rc-1-released-3409",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.sqlite.org/about.html",
        "label": "sqlite-org-about",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.typescriptlang.org/community/",
        "label": "typescriptlang-org-community",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://angular.dev/ai/agent-skills",
        "label": "angular-dev-ai-agent-skills",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.hashicorp.com/terraform/cloud-docs",
        "label": "developer-hashicorp-com-terraform-cloud-docs",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/CSS",
        "label": "developer-mozilla-org-en-us-docs-web-css",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://doc.rust-lang.org/book/appendix-06-translation.html",
        "label": "doc-rust-lang-org-book-appendix-06-translation",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.docker.com/accounts/organization/manage/general-settings/",
        "label": "docs-docker-com-accounts-organization-manage-general-settings",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.aiohttp.org/en/stable/client_advanced.html",
        "label": "docs-aiohttp-org-en-stable-client-advanced",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astral.sh/ruff/contributing/",
        "label": "docs-astral-sh-ruff-contributing",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astro.build/en/astro-courses/",
        "label": "docs-astro-build-en-astro-courses",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.celeryq.dev/en/stable/copyright.html",
        "label": "docs-celeryq-dev-en-stable-copyright",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.djangoproject.com/en/1.11/",
        "label": "docs-djangoproject-com-en-1-11",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.kernel.org/admin-guide/reporting-issues.html",
        "label": "docs-kernel-org-admin-guide-reporting-issues",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.npmjs.com/about-npm/",
        "label": "docs-npmjs-com-about-npm",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/stable/how-to/bash-completion.html",
        "label": "docs-pytest-org-en-stable-how-to-bash-completion",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.python.org/3.0/",
        "label": "docs-python-org-3-0",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.ros.org/en/humble/",
        "label": "docs-ros-org-en-humble",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://expressjs.com/en/",
        "label": "expressjs-com-en",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://fastapi.tiangolo.com/async/",
        "label": "fastapi-tiangolo-com-async",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://flask.palletsprojects.com/en/stable/config/",
        "label": "flask-palletsprojects-com-en-stable-config",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://git-scm.com/about/trademark",
        "label": "git-scm-com-about-trademark",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://go.dev/doc/",
        "label": "go-dev-doc",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://jestjs.io/docs/29.7/getting-started",
        "label": "jestjs-io-docs-29-7-getting-started",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/docs/concepts/cluster-administration/node-shutdown/",
        "label": "kubernetes-io-docs-concepts-cluster-administration-node-shutdown",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/docs/reference/",
        "label": "kubernetes-io-docs-reference",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://man7.org/linux/man-pages/changelog.html",
        "label": "man7-org-linux-man-pages-changelog",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/API/ANGLE_instanced_arrays/drawArraysInstancedANGLE",
        "label": "developer-mozilla-org-en-us-docs-web-api-angle-instanced-arrays-drawar",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mochajs.org/declaring/dynamic-tests/",
        "label": "mochajs-org-declaring-dynamic-tests",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mypy.readthedocs.io/en/stable/cheat_sheet_py3.html",
        "label": "mypy-readthedocs-io-en-stable-cheat-sheet-py3",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nextjs.org/docs",
        "label": "nextjs-org-docs",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nodejs.org/en/blog/weekly/weekly-update2015-02-13",
        "label": "nodejs-org-en-blog-weekly-weekly-update2015-02-13",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://numpy.org/doc/stable/",
        "label": "numpy-org-doc-stable",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pandas.pydata.org/docs/development/index.html",
        "label": "pandas-pydata-org-docs-development-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pip.pypa.io/en/stable/getting-started/",
        "label": "pip-pypa-io-en-stable-getting-started",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/about/casestudies/shannonmedical/",
        "label": "postgresql-org-about-casestudies-shannonmedical",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/7.0.x/",
        "label": "docs-pytest-org-en-7-0-x",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.python.org/3.13/",
        "label": "docs-python-org-3-13",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://react.dev/community",
        "label": "react-dev-community",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/commands/redis-8-6-commands/",
        "label": "redis-io-docs-latest-commands-redis-8-6-commands",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/apis/",
        "label": "redis-io-docs-latest-apis",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://requests.readthedocs.io/en/latest/community/faq/",
        "label": "requests-readthedocs-io-en-latest-community-faq",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://doc.rust-lang.org/stable/",
        "label": "doc-rust-lang-org-stable",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://setuptools.pypa.io/en/latest/genindex.html",
        "label": "setuptools-pypa-io-en-latest-genindex",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://svelte.dev/blog",
        "label": "svelte-dev-blog",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://tailwindcss.com/docs/adding-custom-styles",
        "label": "tailwindcss-com-docs-adding-custom-styles",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.hashicorp.com/boundary/api-docs",
        "label": "developer-hashicorp-com-boundary-api-docs",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://vuejs.org/about/community-guide",
        "label": "vuejs-org-about-community-guide",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://webpack.js.org/blog/",
        "label": "webpack-js-org-blog",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.freedesktop.org/wiki/CodeOfConduct/",
        "label": "freedesktop-org-wiki-codeofconduct",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.gnu.org/licenses/licenses.en.html",
        "label": "gnu-org-licenses-licenses-en",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/account/login/?next=/account/",
        "label": "postgresql-org-account-login",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.sqlite.org/c3ref/funclist.html",
        "label": "sqlite-org-c3ref-funclist",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.typescriptlang.org/docs/",
        "label": "typescriptlang-org-docs",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://angular.dev/ai/ai-tutor",
        "label": "angular-dev-ai-ai-tutor",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.hashicorp.com/terraform/docs",
        "label": "developer-hashicorp-com-terraform-docs",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/CSS/Guides",
        "label": "developer-mozilla-org-en-us-docs-web-css-guides",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://doc.rust-lang.org/book/ch01-01-installation.html",
        "label": "doc-rust-lang-org-book-ch01-01-installation",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.docker.com/accounts/organization/setup/orgs/",
        "label": "docs-docker-com-accounts-organization-setup-orgs",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.aiohttp.org/en/stable/client_middleware_cookbook.html",
        "label": "docs-aiohttp-org-en-stable-client-middleware-cookbook",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astral.sh/ruff/default-rules/",
        "label": "docs-astral-sh-ruff-default-rules",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astro.build/en/concepts/islands/",
        "label": "docs-astro-build-en-concepts-islands",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.celeryq.dev/en/stable/genindex.html",
        "label": "docs-celeryq-dev-en-stable-genindex",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.djangoproject.com/en/1.8/",
        "label": "docs-djangoproject-com-en-1-8",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.kernel.org/core-api/index.html",
        "label": "docs-kernel-org-core-api-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.npmjs.com/about-packages-and-modules/",
        "label": "docs-npmjs-com-about-packages-and-modules",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/stable/how-to/cache.html",
        "label": "docs-pytest-org-en-stable-how-to-cache",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.python.org/3.1/",
        "label": "docs-python-org-3-1",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.ros.org/en/jazzy/",
        "label": "docs-ros-org-en-jazzy",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://expressjs.com/en/5x/guide/error-handling/",
        "label": "expressjs-com-en-5x-guide-error-handling",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://fastapi.tiangolo.com/de/",
        "label": "fastapi-tiangolo-com-de",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://flask.palletsprojects.com/en/stable/debugging/",
        "label": "flask-palletsprojects-com-en-stable-debugging",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://git-scm.com/cheat-sheet",
        "label": "git-scm-com-cheat-sheet",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://go.dev/doc/articles/wiki/",
        "label": "go-dev-doc-articles-wiki",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://jestjs.io/docs/30.0/getting-started",
        "label": "jestjs-io-docs-30-0-getting-started",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/docs/concepts/cluster-administration/swap-memory-management/",
        "label": "kubernetes-io-docs-concepts-cluster-administration-swap-memory-managem",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/docs/setup/",
        "label": "kubernetes-io-docs-setup",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://man7.org/linux/man-pages/dir_all_alphabetic.html",
        "label": "man7-org-linux-man-pages-dir-all-alphabetic",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/API/ANGLE_instanced_arrays/drawElementsInstancedANGLE",
        "label": "developer-mozilla-org-en-us-docs-web-api-angle-instanced-arrays-drawel",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mochajs.org/declaring/exclusive-tests/",
        "label": "mochajs-org-declaring-exclusive-tests",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mypy.readthedocs.io/en/stable/class_basics.html",
        "label": "mypy-readthedocs-io-en-stable-class-basics",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nextjs.org/docs/app/getting-started",
        "label": "nextjs-org-docs-app-getting-started",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nodejs.org/en/blog/weekly/weekly-update2015-02-20",
        "label": "nodejs-org-en-blog-weekly-weekly-update2015-02-20",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://numpy.org/doc/stable/building/index.html",
        "label": "numpy-org-doc-stable-building-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pandas.pydata.org/docs/genindex.html",
        "label": "pandas-pydata-org-docs-genindex",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pip.pypa.io/en/stable/installation/",
        "label": "pip-pypa-io-en-stable-installation",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/about/casestudies/vanten/",
        "label": "postgresql-org-about-casestudies-vanten",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/7.1.x/",
        "label": "docs-pytest-org-en-7-1-x",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.python.org/3.14/",
        "label": "docs-python-org-3-14",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://react.dev/community/translations",
        "label": "react-dev-community-translations",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/commands/redis-8-8-commands/",
        "label": "redis-io-docs-latest-commands-redis-8-8-commands",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/commands/",
        "label": "redis-io-docs-latest-commands",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://requests.readthedocs.io/en/latest/community/out-there/",
        "label": "requests-readthedocs-io-en-latest-community-out-there",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://setuptools.pypa.io/en/latest/references/keywords.html",
        "label": "setuptools-pypa-io-en-latest-references-keywords",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://svelte.dev/docs",
        "label": "svelte-dev-docs",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://tailwindcss.com/docs/aspect-ratio",
        "label": "tailwindcss-com-docs-aspect-ratio",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.hashicorp.com/boundary/api-docs/account-service",
        "label": "developer-hashicorp-com-boundary-api-docs-account-service",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://vuejs.org/about/faq",
        "label": "vuejs-org-about-faq",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://webpack.js.org/concepts/",
        "label": "webpack-js-org-concepts",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.freedesktop.org/wiki/Infrastructure/",
        "label": "freedesktop-org-wiki-infrastructure",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.gnu.org/licenses/licenses.html",
        "label": "gnu-org-licenses-licenses",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/docs/",
        "label": "postgresql-org-docs",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.sqlite.org/c3ref/intro.html",
        "label": "sqlite-org-c3ref-intro",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.typescriptlang.org/docs/handbook/2/basic-types.html",
        "label": "typescriptlang-org-docs-handbook-2-basic-types",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://angular.dev/ai/design-patterns",
        "label": "angular-dev-ai-design-patterns",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.hashicorp.com/terraform/enterprise",
        "label": "developer-hashicorp-com-terraform-enterprise",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/CSS/Guides/Animations/Using",
        "label": "developer-mozilla-org-en-us-docs-web-css-guides-animations-using",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://doc.rust-lang.org/book/ch03-02-data-types.html",
        "label": "doc-rust-lang-org-book-ch03-02-data-types",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.docker.com/agentic-platform/",
        "label": "docs-docker-com-agentic-platform",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.aiohttp.org/en/stable/client_quickstart.html",
        "label": "docs-aiohttp-org-en-stable-client-quickstart",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astral.sh/ruff/editors/",
        "label": "docs-astral-sh-ruff-editors",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astro.build/en/concepts/why-astro/",
        "label": "docs-astro-build-en-concepts-why-astro",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/index.html",
        "label": "docs-celeryq-dev-en-stable-getting-started-backends-and-brokers-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.djangoproject.com/en/1.9/",
        "label": "docs-djangoproject-com-en-1-9",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.kernel.org/dev-tools/index.html",
        "label": "docs-kernel-org-dev-tools-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.npmjs.com/about-the-public-npm-registry/",
        "label": "docs-npmjs-com-about-the-public-npm-registry",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/stable/how-to/capture-stdout-stderr.html",
        "label": "docs-pytest-org-en-stable-how-to-capture-stdout-stderr",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.python.org/3.10/",
        "label": "docs-python-org-3-10",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.ros.org/en/kilted/",
        "label": "docs-ros-org-en-kilted",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://expressjs.com/en/5x/guide/routing/",
        "label": "expressjs-com-en-5x-guide-routing",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://fastapi.tiangolo.com/es/",
        "label": "fastapi-tiangolo-com-es",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://flask.palletsprojects.com/en/stable/errorhandling/",
        "label": "flask-palletsprojects-com-en-stable-errorhandling",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://git-scm.com/community",
        "label": "git-scm-com-community",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://go.dev/doc/code",
        "label": "go-dev-doc-code",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://jestjs.io/docs/30.4/getting-started",
        "label": "jestjs-io-docs-30-4-getting-started",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/docs/concepts/containers/images/",
        "label": "kubernetes-io-docs-concepts-containers-images",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/docs/tasks/",
        "label": "kubernetes-io-docs-tasks",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://man7.org/linux/man-pages/dir_all_by_section.html",
        "label": "man7-org-linux-man-pages-dir-all-by-section",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/API/ANGLE_instanced_arrays/vertexAttribDivisorANGLE",
        "label": "developer-mozilla-org-en-us-docs-web-api-angle-instanced-arrays-vertex",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mochajs.org/declaring/inclusive-tests/",
        "label": "mochajs-org-declaring-inclusive-tests",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mypy.readthedocs.io/en/stable/command_line.html",
        "label": "mypy-readthedocs-io-en-stable-command-line",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nextjs.org/docs/app/getting-started/caching",
        "label": "nextjs-org-docs-app-getting-started-caching",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nodejs.org/en/blog/weekly/weekly-update2015-02-27",
        "label": "nodejs-org-en-blog-weekly-weekly-update2015-02-27",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://numpy.org/doc/stable/dev/index.html",
        "label": "numpy-org-doc-stable-dev-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pandas.pydata.org/docs/getting_started/comparison/comparison_with_r.html",
        "label": "pandas-pydata-org-docs-getting-started-comparison-comparison-with-r",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pip.pypa.io/en/stable/reference/",
        "label": "pip-pypa-io-en-stable-reference",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/about/contact/",
        "label": "postgresql-org-about-contact",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/7.2.x/",
        "label": "docs-pytest-org-en-7-2-x",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.python.org/3.15/",
        "label": "docs-python-org-3-15",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://react.dev/learn",
        "label": "react-dev-learn",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/develop/clients/hiredis/connect/",
        "label": "redis-io-docs-latest-develop-clients-hiredis-connect",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/develop/",
        "label": "redis-io-docs-latest-develop",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://requests.readthedocs.io/en/latest/community/recommended/",
        "label": "requests-readthedocs-io-en-latest-community-recommended",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://setuptools.pypa.io/en/latest/search.html",
        "label": "setuptools-pypa-io-en-latest-search",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://svelte.dev/docs/ai/overview",
        "label": "svelte-dev-docs-ai-overview",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://tailwindcss.com/docs/break-after",
        "label": "tailwindcss-com-docs-break-after",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.hashicorp.com/boundary/api-docs/alias-service",
        "label": "developer-hashicorp-com-boundary-api-docs-alias-service",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://vuejs.org/about/privacy",
        "label": "vuejs-org-about-privacy",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://webpack.js.org/concepts/configuration/",
        "label": "webpack-js-org-concepts-configuration",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.freedesktop.org/wiki/NewProject/",
        "label": "freedesktop-org-wiki-newproject",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.gnu.org/software/make/manual/make.html",
        "label": "gnu-org-software-make-manual-make",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/docs/10/index.html",
        "label": "postgresql-org-docs-10-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.sqlite.org/chronology.html",
        "label": "sqlite-org-chronology",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.typescriptlang.org/docs/handbook/2/conditional-types.html",
        "label": "typescriptlang-org-docs-handbook-2-conditional-types",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://angular.dev/ai/develop-with-ai",
        "label": "angular-dev-ai-develop-with-ai",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.hashicorp.com/terraform/intro",
        "label": "developer-hashicorp-com-terraform-intro",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/CSS/Guides/Box_model/Introduction",
        "label": "developer-mozilla-org-en-us-docs-web-css-guides-box-model-introduction",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://doc.rust-lang.org/book/ch07-02-defining-modules-to-control-scope-and-privacy.html",
        "label": "doc-rust-lang-org-book-ch07-02-defining-modules-to-control-scope-and-p",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.docker.com/agentic-platform/get-started/",
        "label": "docs-docker-com-agentic-platform-get-started",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.aiohttp.org/en/stable/client_reference.html",
        "label": "docs-aiohttp-org-en-stable-client-reference",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astral.sh/ruff/editors/features/",
        "label": "docs-astral-sh-ruff-editors-features",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astro.build/en/getting-started/",
        "label": "docs-astro-build-en-getting-started",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.celeryq.dev/en/stable/getting-started/first-steps-with-celery.html",
        "label": "docs-celeryq-dev-en-stable-getting-started-first-steps-with-celery",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.djangoproject.com/en/2.0/",
        "label": "docs-djangoproject-com-en-2-0",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.kernel.org/dev-tools/testing-overview.html",
        "label": "docs-kernel-org-dev-tools-testing-overview",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.npmjs.com/about-two-factor-authentication/",
        "label": "docs-npmjs-com-about-two-factor-authentication",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/stable/how-to/capture-warnings.html",
        "label": "docs-pytest-org-en-stable-how-to-capture-warnings",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.python.org/3.2/",
        "label": "docs-python-org-3-2",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.ros.org/en/lyrical/",
        "label": "docs-ros-org-en-lyrical",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://expressjs.com/en/5x/guide/using-middleware/",
        "label": "expressjs-com-en-5x-guide-using-middleware",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://fastapi.tiangolo.com/features/",
        "label": "fastapi-tiangolo-com-features",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://flask.palletsprojects.com/en/stable/genindex/",
        "label": "flask-palletsprojects-com-en-stable-genindex",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://git-scm.com/doc/credential-helpers",
        "label": "git-scm-com-doc-credential-helpers",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://go.dev/doc/devel/release",
        "label": "go-dev-doc-devel-release",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://jestjs.io/docs/api",
        "label": "jestjs-io-docs-api",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/docs/concepts/extend-kubernetes/api-extension/custom-resources/",
        "label": "kubernetes-io-docs-concepts-extend-kubernetes-api-extension-custom-res",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/docs/tutorials/",
        "label": "kubernetes-io-docs-tutorials",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://man7.org/linux/man-pages/dir_by_project.html",
        "label": "man7-org-linux-man-pages-dir-by-project",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/API/AbortController",
        "label": "developer-mozilla-org-en-us-docs-web-api-abortcontroller",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mochajs.org/declaring/pending-tests/",
        "label": "mochajs-org-declaring-pending-tests",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mypy.readthedocs.io/en/stable/config_file.html",
        "label": "mypy-readthedocs-io-en-stable-config-file",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nextjs.org/docs/app/getting-started/css",
        "label": "nextjs-org-docs-app-getting-started-css",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nodejs.org/en/blog/weekly/weekly-update2015-03-06",
        "label": "nodejs-org-en-blog-weekly-weekly-update2015-03-06",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://numpy.org/doc/stable/dev/underthehood.html",
        "label": "numpy-org-doc-stable-dev-underthehood",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pandas.pydata.org/docs/getting_started/comparison/comparison_with_sas.html",
        "label": "pandas-pydata-org-docs-getting-started-comparison-comparison-with-sas",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pip.pypa.io/en/stable/reference/build-system/",
        "label": "pip-pypa-io-en-stable-reference-build-system",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/about/donate/",
        "label": "postgresql-org-about-donate",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/7.3.x/",
        "label": "docs-pytest-org-en-7-3-x",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.python.org/3.16/",
        "label": "docs-python-org-3-16",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://react.dev/learn/add-react-to-an-existing-project",
        "label": "react-dev-learn-add-react-to-an-existing-project",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/develop/clients/patterns/bulk-loading/",
        "label": "redis-io-docs-latest-develop-clients-patterns-bulk-loading",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/develop/ai/",
        "label": "redis-io-docs-latest-develop-ai",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://requests.readthedocs.io/en/latest/community/release-process/",
        "label": "requests-readthedocs-io-en-latest-community-release-process",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://setuptools.pypa.io/en/latest/userguide/datafiles.html",
        "label": "setuptools-pypa-io-en-latest-userguide-datafiles",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://svelte.dev/docs/cli/overview",
        "label": "svelte-dev-docs-cli-overview",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://tailwindcss.com/docs/break-before",
        "label": "tailwindcss-com-docs-break-before",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.hashicorp.com/boundary/api-docs/app-token-service",
        "label": "developer-hashicorp-com-boundary-api-docs-app-token-service",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://vuejs.org/about/releases",
        "label": "vuejs-org-about-releases",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://webpack.js.org/concepts/dependency-graph/",
        "label": "webpack-js-org-concepts-dependency-graph",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.freedesktop.org/wiki/PrivacyPolicy/",
        "label": "freedesktop-org-wiki-privacypolicy",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/docs/11/index.html",
        "label": "postgresql-org-docs-11-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.sqlite.org/cintro.html",
        "label": "sqlite-org-cintro",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.typescriptlang.org/docs/handbook/2/everyday-types.html",
        "label": "typescriptlang-org-docs-handbook-2-everyday-types",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://angular.dev/ai/mcp",
        "label": "angular-dev-ai-mcp",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.hashicorp.com/terraform/language",
        "label": "developer-hashicorp-com-terraform-language",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/CSS/Guides/Colors/Applying_color",
        "label": "developer-mozilla-org-en-us-docs-web-css-guides-colors-applying-color",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://doc.rust-lang.org/book/foreword.html",
        "label": "doc-rust-lang-org-book-foreword",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.docker.com/agentic-platform/signup/",
        "label": "docs-docker-com-agentic-platform-signup",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.aiohttp.org/en/stable/deployment.html",
        "label": "docs-aiohttp-org-en-stable-deployment",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astral.sh/ruff/editors/migration/",
        "label": "docs-astral-sh-ruff-editors-migration",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astro.build/en/install-and-setup/",
        "label": "docs-astro-build-en-install-and-setup",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.celeryq.dev/en/stable/getting-started/index.html",
        "label": "docs-celeryq-dev-en-stable-getting-started-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.djangoproject.com/en/2.1/",
        "label": "docs-djangoproject-com-en-2-1",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.kernel.org/doc-guide/index.html",
        "label": "docs-kernel-org-doc-guide-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.npmjs.com/accessing-npm-using-2fa/",
        "label": "docs-npmjs-com-accessing-npm-using-2fa",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/stable/how-to/doctest.html",
        "label": "docs-pytest-org-en-stable-how-to-doctest",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.python.org/3.3/",
        "label": "docs-python-org-3-3",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.ros.org/en/lyrical/About-ROS.html",
        "label": "docs-ros-org-en-lyrical-about-ros",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://expressjs.com/en/5x/guide/using-template-engines/",
        "label": "expressjs-com-en-5x-guide-using-template-engines",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://fastapi.tiangolo.com/fr/",
        "label": "fastapi-tiangolo-com-fr",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://flask.palletsprojects.com/en/stable/installation/",
        "label": "flask-palletsprojects-com-en-stable-installation",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://git-scm.com/doc/ext",
        "label": "git-scm-com-doc-ext",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://go.dev/doc/diagnostics",
        "label": "go-dev-doc-diagnostics",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://jestjs.io/docs/asynchronous",
        "label": "jestjs-io-docs-asynchronous",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/network-plugins/",
        "label": "kubernetes-io-docs-concepts-extend-kubernetes-compute-storage-net-netw",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/releases/download/",
        "label": "kubernetes-io-releases-download",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://man7.org/linux/man-pages/dir_section_1.html",
        "label": "man7-org-linux-man-pages-dir-section-1",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/API/AbortController/AbortController",
        "label": "developer-mozilla-org-en-us-docs-web-api-abortcontroller-abortcontroll",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mochajs.org/declaring/retrying-tests/",
        "label": "mochajs-org-declaring-retrying-tests",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mypy.readthedocs.io/en/stable/duck_type_compatibility.html",
        "label": "mypy-readthedocs-io-en-stable-duck-type-compatibility",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nextjs.org/docs/app/getting-started/deploying",
        "label": "nextjs-org-docs-app-getting-started-deploying",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nodejs.org/en/blog/weekly/weekly-update2015-03-13",
        "label": "nodejs-org-en-blog-weekly-weekly-update2015-03-13",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://numpy.org/doc/stable/f2py/index.html",
        "label": "numpy-org-doc-stable-f2py-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pandas.pydata.org/docs/getting_started/comparison/comparison_with_spreadsheets.html",
        "label": "pandas-pydata-org-docs-getting-started-comparison-comparison-with-spre",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pip.pypa.io/en/stable/reference/inspect-report/",
        "label": "pip-pypa-io-en-stable-reference-inspect-report",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/about/donate_pg_org/",
        "label": "postgresql-org-about-donate-pg-org",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/7.4.x/",
        "label": "docs-pytest-org-en-7-4-x",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.python.org/3/",
        "label": "docs-python-org-3",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://react.dev/learn/build-a-react-app-from-scratch",
        "label": "react-dev-learn-build-a-react-app-from-scratch",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/",
        "label": "redis-io-docs-latest-develop-clients-patterns-distributed-locks",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/develop/ai/agent-builder/",
        "label": "redis-io-docs-latest-develop-ai-agent-builder",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://requests.readthedocs.io/en/latest/community/support/",
        "label": "requests-readthedocs-io-en-latest-community-support",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://setuptools.pypa.io/en/latest/userguide/declarative_config.html",
        "label": "setuptools-pypa-io-en-latest-userguide-declarative-config",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://svelte.dev/docs/kit/introduction",
        "label": "svelte-dev-docs-kit-introduction",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://tailwindcss.com/docs/break-inside",
        "label": "tailwindcss-com-docs-break-inside",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.hashicorp.com/boundary/api-docs/auth-method-service",
        "label": "developer-hashicorp-com-boundary-api-docs-auth-method-service",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://vuejs.org/about/team",
        "label": "vuejs-org-about-team",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://webpack.js.org/concepts/entry-points/",
        "label": "webpack-js-org-concepts-entry-points",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.freedesktop.org/wiki/Software/",
        "label": "freedesktop-org-wiki-software",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/docs/12/index.html",
        "label": "postgresql-org-docs-12-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.sqlite.org/cli.html",
        "label": "sqlite-org-cli",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.typescriptlang.org/docs/handbook/2/functions.html",
        "label": "typescriptlang-org-docs-handbook-2-functions",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://angular.dev/ai/webmcp",
        "label": "angular-dev-ai-webmcp",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.hashicorp.com/terraform/mcp-server",
        "label": "developer-hashicorp-com-terraform-mcp-server",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/CSS/Guides/Flexible_box_layout/Basic_concepts",
        "label": "developer-mozilla-org-en-us-docs-web-css-guides-flexible-box-layout-ba",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://doc.rust-lang.org/book/print.html",
        "label": "doc-rust-lang-org-book-print",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.docker.com/ai-overview/",
        "label": "docs-docker-com-ai-overview",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.aiohttp.org/en/stable/genindex.html",
        "label": "docs-aiohttp-org-en-stable-genindex",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astral.sh/ruff/editors/settings/",
        "label": "docs-astral-sh-ruff-editors-settings",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astro.build/en/tutorial/0-introduction/",
        "label": "docs-astro-build-en-tutorial-0-introduction",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.celeryq.dev/en/stable/getting-started/introduction.html",
        "label": "docs-celeryq-dev-en-stable-getting-started-introduction",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.djangoproject.com/en/2.2/",
        "label": "docs-djangoproject-com-en-2-2",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.kernel.org/driver-api/index.html",
        "label": "docs-kernel-org-driver-api-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.npmjs.com/changing-your-npm-username/",
        "label": "docs-npmjs-com-changing-your-npm-username",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/stable/how-to/existingtestsuite.html",
        "label": "docs-pytest-org-en-stable-how-to-existingtestsuite",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.python.org/3.4/",
        "label": "docs-python-org-3-4",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.ros.org/en/lyrical/Get-Started.html",
        "label": "docs-ros-org-en-lyrical-get-started",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://expressjs.com/en/5x/guide/writing-middleware/",
        "label": "expressjs-com-en-5x-guide-writing-middleware",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://fastapi.tiangolo.com/hi/",
        "label": "fastapi-tiangolo-com-hi",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://flask.palletsprojects.com/en/stable/lifecycle/",
        "label": "flask-palletsprojects-com-en-stable-lifecycle",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://git-scm.com/docs",
        "label": "git-scm-com-docs",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://go.dev/doc/editors",
        "label": "go-dev-doc-editors",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://jestjs.io/docs/getting-started",
        "label": "jestjs-io-docs-getting-started",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/docs/concepts/overview/components/",
        "label": "kubernetes-io-docs-concepts-overview-components",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/training/",
        "label": "kubernetes-io-training",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://man7.org/linux/man-pages/dir_section_2.html",
        "label": "man7-org-linux-man-pages-dir-section-2",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/API/AbortController/abort",
        "label": "developer-mozilla-org-en-us-docs-web-api-abortcontroller-abort",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mochajs.org/features/arrow-functions/",
        "label": "mochajs-org-features-arrow-functions",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mypy.readthedocs.io/en/stable/dynamic_typing.html",
        "label": "mypy-readthedocs-io-en-stable-dynamic-typing",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nextjs.org/docs/app/getting-started/error-handling",
        "label": "nextjs-org-docs-app-getting-started-error-handling",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nodejs.org/en/blog/weekly/weekly-update2015-03-20",
        "label": "nodejs-org-en-blog-weekly-weekly-update2015-03-20",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://numpy.org/doc/stable/genindex.html",
        "label": "numpy-org-doc-stable-genindex",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pandas.pydata.org/docs/getting_started/comparison/comparison_with_spss.html",
        "label": "pandas-pydata-org-docs-getting-started-comparison-comparison-with-spss",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pip.pypa.io/en/stable/reference/installation-report/",
        "label": "pip-pypa-io-en-stable-reference-installation-report",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/about/governance/",
        "label": "postgresql-org-about-governance",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/8.0.x/",
        "label": "docs-pytest-org-en-8-0-x",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://react.dev/learn/creating-a-react-app",
        "label": "react-dev-learn-creating-a-react-app",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/develop/clients/ruby/connect/",
        "label": "redis-io-docs-latest-develop-clients-ruby-connect",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/develop/ai/context-engine/",
        "label": "redis-io-docs-latest-develop-ai-context-engine",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://requests.readthedocs.io/en/latest/community/updates/",
        "label": "requests-readthedocs-io-en-latest-community-updates",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://setuptools.pypa.io/en/latest/userguide/dependency_management.html",
        "label": "setuptools-pypa-io-en-latest-userguide-dependency-management",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://svelte.dev/docs/llms",
        "label": "svelte-dev-docs-llms",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://tailwindcss.com/docs/colors",
        "label": "tailwindcss-com-docs-colors",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.hashicorp.com/boundary/api-docs/auth-token-service",
        "label": "developer-hashicorp-com-boundary-api-docs-auth-token-service",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://vuejs.org/api/",
        "label": "vuejs-org-api",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://webpack.js.org/concepts/hot-module-replacement/",
        "label": "webpack-js-org-concepts-hot-module-replacement",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.freedesktop.org/wiki/Software/AccountsService/",
        "label": "freedesktop-org-wiki-software-accountsservice",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/docs/13/index.html",
        "label": "postgresql-org-docs-13-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.sqlite.org/copyright.html",
        "label": "sqlite-org-copyright",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.typescriptlang.org/docs/handbook/2/generics.html",
        "label": "typescriptlang-org-docs-handbook-2-generics",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://angular.dev/essentials",
        "label": "angular-dev-essentials",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.hashicorp.com/terraform/tutorials",
        "label": "developer-hashicorp-com-terraform-tutorials",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/CSS/How_to/Layout_cookbook",
        "label": "developer-mozilla-org-en-us-docs-web-css-how-to-layout-cookbook",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://doc.rust-lang.org/cargo/",
        "label": "doc-rust-lang-org-cargo",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.docker.com/ai/docker-agent/concepts/agents/",
        "label": "docs-docker-com-ai-docker-agent-concepts-agents",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.aiohttp.org/en/stable/http_request_lifecycle.html",
        "label": "docs-aiohttp-org-en-stable-http-request-lifecycle",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astral.sh/ruff/editors/setup/",
        "label": "docs-astral-sh-ruff-editors-setup",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astro.build/en/tutorial/1-setup/",
        "label": "docs-astro-build-en-tutorial-1-setup",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.celeryq.dev/en/stable/getting-started/next-steps.html",
        "label": "docs-celeryq-dev-en-stable-getting-started-next-steps",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.djangoproject.com/en/3.0/",
        "label": "docs-djangoproject-com-en-3-0",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.kernel.org/fault-injection/index.html",
        "label": "docs-kernel-org-fault-injection-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.npmjs.com/common-errors/",
        "label": "docs-npmjs-com-common-errors",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/stable/how-to/failures.html",
        "label": "docs-pytest-org-en-stable-how-to-failures",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.python.org/3.5/",
        "label": "docs-python-org-3-5",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.ros.org/en/lyrical/Get-Started/Installation.html",
        "label": "docs-ros-org-en-lyrical-get-started-installation",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://expressjs.com/en/5x/starter/basic-routing/",
        "label": "expressjs-com-en-5x-starter-basic-routing",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://fastapi.tiangolo.com/ja/",
        "label": "fastapi-tiangolo-com-ja",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://flask.palletsprojects.com/en/stable/logging/",
        "label": "flask-palletsprojects-com-en-stable-logging",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://git-scm.com/docs/git",
        "label": "git-scm-com-docs-git",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://go.dev/doc/effective_go",
        "label": "go-dev-doc-effective-go",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://jestjs.io/docs/jest-community",
        "label": "jestjs-io-docs-jest-community",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/docs/concepts/policy/limit-range/",
        "label": "kubernetes-io-docs-concepts-policy-limit-range",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://man7.org/linux/man-pages/dir_section_3.html",
        "label": "man7-org-linux-man-pages-dir-section-3",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/API/AbortController/signal",
        "label": "developer-mozilla-org-en-us-docs-web-api-abortcontroller-signal",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mochajs.org/features/assertions/",
        "label": "mochajs-org-features-assertions",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mypy.readthedocs.io/en/stable/existing_code.html",
        "label": "mypy-readthedocs-io-en-stable-existing-code",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nextjs.org/docs/app/getting-started/fetching-data",
        "label": "nextjs-org-docs-app-getting-started-fetching-data",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nodejs.org/en/blog/weekly/weekly-update2015-03-27",
        "label": "nodejs-org-en-blog-weekly-weekly-update2015-03-27",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://numpy.org/doc/stable/glossary.html",
        "label": "numpy-org-doc-stable-glossary",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pandas.pydata.org/docs/getting_started/comparison/comparison_with_sql.html",
        "label": "pandas-pydata-org-docs-getting-started-comparison-comparison-with-sql",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pip.pypa.io/en/stable/reference/requirement-specifiers/",
        "label": "pip-pypa-io-en-stable-reference-requirement-specifiers",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/about/governance/contributors/",
        "label": "postgresql-org-about-governance-contributors",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/8.1.x/",
        "label": "docs-pytest-org-en-8-1-x",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://react.dev/learn/describing-the-ui",
        "label": "react-dev-learn-describing-the-ui",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/develop/data-types/compare-data-types/",
        "label": "redis-io-docs-latest-develop-data-types-compare-data-types",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/develop/ai/redisvl/",
        "label": "redis-io-docs-latest-develop-ai-redisvl",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://requests.readthedocs.io/en/latest/community/vulnerabilities/",
        "label": "requests-readthedocs-io-en-latest-community-vulnerabilities",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://setuptools.pypa.io/en/latest/userguide/development_mode.html",
        "label": "setuptools-pypa-io-en-latest-userguide-development-mode",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://svelte.dev/docs/svelte/$bindable",
        "label": "svelte-dev-docs-svelte-bindable",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://tailwindcss.com/docs/columns",
        "label": "tailwindcss-com-docs-columns",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.hashicorp.com/boundary/api-docs/billing-service",
        "label": "developer-hashicorp-com-boundary-api-docs-billing-service",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://vuejs.org/ecosystem/newsletters",
        "label": "vuejs-org-ecosystem-newsletters",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://webpack.js.org/concepts/loaders/",
        "label": "webpack-js-org-concepts-loaders",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.freedesktop.org/wiki/Software/PolicyKit/",
        "label": "freedesktop-org-wiki-software-policykit",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/docs/14/index.html",
        "label": "postgresql-org-docs-14-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.sqlite.org/docs.html",
        "label": "sqlite-org-docs",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.typescriptlang.org/docs/handbook/2/indexed-access-types.html",
        "label": "typescriptlang-org-docs-handbook-2-indexed-access-types",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://angular.dev/essentials/signals",
        "label": "angular-dev-essentials-signals",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/CSS/How_to/Layout_cookbook/Card",
        "label": "developer-mozilla-org-en-us-docs-web-css-how-to-layout-cookbook-card",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://doc.rust-lang.org/cargo/reference/profiles.html",
        "label": "doc-rust-lang-org-cargo-reference-profiles",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.docker.com/ai/docker-agent/configuration/overview/",
        "label": "docs-docker-com-ai-docker-agent-configuration-overview",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.aiohttp.org/en/stable/index.html",
        "label": "docs-aiohttp-org-en-stable-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astral.sh/ruff/faq/",
        "label": "docs-astral-sh-ruff-faq",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astro.build/en/tutorial/2-pages/",
        "label": "docs-astro-build-en-tutorial-2-pages",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.celeryq.dev/en/stable/getting-started/resources.html",
        "label": "docs-celeryq-dev-en-stable-getting-started-resources",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.djangoproject.com/en/3.1/",
        "label": "docs-djangoproject-com-en-3-1",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.kernel.org/genindex.html",
        "label": "docs-kernel-org-genindex",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.npmjs.com/configuring-two-factor-authentication/",
        "label": "docs-npmjs-com-configuring-two-factor-authentication",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/stable/how-to/fixtures.html",
        "label": "docs-pytest-org-en-stable-how-to-fixtures",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.python.org/3.6/",
        "label": "docs-python-org-3-6",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.ros.org/en/lyrical/Get-Started/Installation/RHEL-Install-RPMs.html",
        "label": "docs-ros-org-en-lyrical-get-started-installation-rhel-install-rpms",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://expressjs.com/en/5x/starter/examples/",
        "label": "expressjs-com-en-5x-starter-examples",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://fastapi.tiangolo.com/ko/",
        "label": "fastapi-tiangolo-com-ko",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://flask.palletsprojects.com/en/stable/patterns/",
        "label": "flask-palletsprojects-com-en-stable-patterns",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://git-scm.com/docs/git-add",
        "label": "git-scm-com-docs-git-add",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://jestjs.io/docs/jest-platform",
        "label": "jestjs-io-docs-jest-platform",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/docs/concepts/resource-management/dynamic-resource-allocation/dra-api/",
        "label": "kubernetes-io-docs-concepts-resource-management-dynamic-resource-alloc",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://man7.org/linux/man-pages/dir_section_4.html",
        "label": "man7-org-linux-man-pages-dir-section-4",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/API/AbortSignal",
        "label": "developer-mozilla-org-en-us-docs-web-api-abortsignal",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mochajs.org/features/asynchronous-code/",
        "label": "mochajs-org-features-asynchronous-code",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mypy.readthedocs.io/en/stable/final_attrs.html",
        "label": "mypy-readthedocs-io-en-stable-final-attrs",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nextjs.org/docs/app/getting-started/fonts",
        "label": "nextjs-org-docs-app-getting-started-fonts",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nodejs.org/en/blog/weekly/weekly-update2015-04-03",
        "label": "nodejs-org-en-blog-weekly-weekly-update2015-04-03",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://numpy.org/doc/stable/index.html",
        "label": "numpy-org-doc-stable-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pandas.pydata.org/docs/getting_started/comparison/comparison_with_stata.html",
        "label": "pandas-pydata-org-docs-getting-started-comparison-comparison-with-stat",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pip.pypa.io/en/stable/reference/requirements-file-format/",
        "label": "pip-pypa-io-en-stable-reference-requirements-file-format",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/about/governance/sysadmin/",
        "label": "postgresql-org-about-governance-sysadmin",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/8.2.x/",
        "label": "docs-pytest-org-en-8-2-x",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://react.dev/learn/editor-setup",
        "label": "react-dev-learn-editor-setup",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/develop/get-started/data-store/",
        "label": "redis-io-docs-latest-develop-get-started-data-store",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/develop/clients/dotnet/",
        "label": "redis-io-docs-latest-develop-clients-dotnet",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://requests.readthedocs.io/en/latest/dev/authors/",
        "label": "requests-readthedocs-io-en-latest-dev-authors",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://setuptools.pypa.io/en/latest/userguide/distribution.html",
        "label": "setuptools-pypa-io-en-latest-userguide-distribution",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://svelte.dev/docs/svelte/$derived",
        "label": "svelte-dev-docs-svelte-derived",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://tailwindcss.com/docs/compatibility",
        "label": "tailwindcss-com-docs-compatibility",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.hashicorp.com/boundary/api-docs/credential-library-service",
        "label": "developer-hashicorp-com-boundary-api-docs-credential-library-service",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://vuejs.org/ecosystem/themes",
        "label": "vuejs-org-ecosystem-themes",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://webpack.js.org/concepts/manifest/",
        "label": "webpack-js-org-concepts-manifest",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.freedesktop.org/wiki/Software/dbus/",
        "label": "freedesktop-org-wiki-software-dbus",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/docs/15/index.html",
        "label": "postgresql-org-docs-15-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.sqlite.org/download.html",
        "label": "sqlite-org-download",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/",
        "label": "arstechnica-com",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/",
        "label": "blog-cloudflare-com",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/",
        "label": "blog-jim-nielsen-com",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://danluu.com/",
        "label": "danluu-com",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://github.blog/",
        "label": "github-blog",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/",
        "label": "jvns-ca",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://lobste.rs/",
        "label": "lobste-rs",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://martinfowler.com/",
        "label": "martinfowler-com",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://news.ycombinator.com",
        "label": "news-ycombinator-com",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://overreacted.io/",
        "label": "overreacted-io",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/",
        "label": "simonwillison-net",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/",
        "label": "stackoverflow-blog",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/",
        "label": "joelonsoftware-com",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/",
        "label": "theregister-com",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/ai/",
        "label": "arstechnica-com-ai",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/de-de/",
        "label": "blog-cloudflare-com-de-de",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/2023/temporarily-abled/",
        "label": "blog-jim-nielsen-com-2023-temporarily-abled",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://github.blog/ai-and-ml/",
        "label": "github-blog-ai-and-ml",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/about/",
        "label": "jvns-ca-about",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://lobste.rs/active",
        "label": "lobste-rs-active",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://martinfowler.com/articles/are-harnesses-worth-the-cost.html",
        "label": "martinfowler-com-articles-are-harnesses-worth-the-cost",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://overreacted.io/a-lean-syntax-primer/",
        "label": "overreacted-io-a-lean-syntax-primer",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/2026/Nov/2/llm/",
        "label": "simonwillison-net-2026-nov-2-llm",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/2026/07/30/introducing-stack-internal-new-platform-experience/",
        "label": "stackoverflow-blog-2026-07-30-introducing-stack-internal-new-platform-",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/2000/04/06/things-you-should-never-do-part-i/",
        "label": "joelonsoftware-com-2000-04-06-things-you-should-never-do-part-i",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/applications",
        "label": "theregister-com-applications",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/cars/",
        "label": "arstechnica-com-cars",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/es-es/",
        "label": "blog-cloudflare-com-es-es",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/2024/rss-in-html/",
        "label": "blog-jim-nielsen-com-2024-rss-in-html",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://github.blog/ai-and-ml/generative-ai/",
        "label": "github-blog-ai-and-ml-generative-ai",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/blog/2023/07/10/lima--a-nice-way-to-run-linux-vms-on-mac/",
        "label": "jvns-ca-blog-2023-07-10-lima-a-nice-way-to-run-linux-vms-on-mac",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://lobste.rs/comments",
        "label": "lobste-rs-comments",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://martinfowler.com/articles/continuousIntegration.html",
        "label": "martinfowler-com-articles-continuousintegration",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://news.ycombinator.com/front",
        "label": "news-ycombinator-com-front",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://overreacted.io/a-social-filesystem/",
        "label": "overreacted-io-a-social-filesystem",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/2026/Nov/3/a-sound-that-leaks-your-exact-location/",
        "label": "simonwillison-net-2026-nov-3-a-sound-that-leaks-your-exact-location",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/2026/09/03/security-control-and-accessibility-si-2026-6/",
        "label": "stackoverflow-blog-2026-09-03-security-control-and-accessibility-si-20",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/2000/05/12/strategy-letter-i-ben-and-jerrys-vs-amazon/",
        "label": "joelonsoftware-com-2000-05-12-strategy-letter-i-ben-and-jerrys-vs-amaz",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/channel",
        "label": "theregister-com-channel",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/cars/2026/11/beyond-screens-mercedes-benzs-new-holographic-heads-up-display/",
        "label": "arstechnica-com-cars-2026-11-beyond-screens-mercedes-benzs-new-hologra",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/es-la/",
        "label": "blog-cloudflare-com-es-la",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/2024/sanding-ui/",
        "label": "blog-jim-nielsen-com-2024-sanding-ui",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://github.blog/ai-and-ml/generative-ai/how-ai-code-generation-works/",
        "label": "github-blog-ai-and-ml-generative-ai-how-ai-code-generation-works",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/blog/2023/08/08/what-helps-people-get-comfortable-on-the-command-line-/",
        "label": "jvns-ca-blog-2023-08-08-what-helps-people-get-comfortable-on-the-comma",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://lobste.rs/domains/alangrow.com",
        "label": "lobste-rs-domains-alangrow-com",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://martinfowler.com/articles/exploring-gen-ai/an-accidental-blackboard.html",
        "label": "martinfowler-com-articles-exploring-gen-ai-an-accidental-blackboard",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://news.ycombinator.com/newest",
        "label": "news-ycombinator-com-newest",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://overreacted.io/beyond-booleans/",
        "label": "overreacted-io-beyond-booleans",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/2026/Nov/3/october-newsletter/",
        "label": "simonwillison-net-2026-nov-3-october-newsletter",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/2026/09/30/anyone-can-start-building-verified-knowledge-with-stack-internal/",
        "label": "stackoverflow-blog-2026-09-30-anyone-can-start-building-verified-knowl",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/2000/08/09/the-joel-test-12-steps-to-better-code/",
        "label": "joelonsoftware-com-2000-08-09-the-joel-test-12-steps-to-better-code",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/cso",
        "label": "theregister-com-cso",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/civis/",
        "label": "arstechnica-com-civis",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/fr-fr/",
        "label": "blog-cloudflare-com-fr-fr",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/2024/you-are-what-you-read/",
        "label": "blog-jim-nielsen-com-2024-you-are-what-you-read",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://github.blog/ai-and-ml/github-copilot/",
        "label": "github-blog-ai-and-ml-github-copilot",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/blog/2024/09/12/reasons-i--still--love-fish/",
        "label": "jvns-ca-blog-2024-09-12-reasons-i-still-love-fish",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://lobste.rs/login",
        "label": "lobste-rs-login",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://martinfowler.com/articles/making-data-ready-for-agentic-ai.html",
        "label": "martinfowler-com-articles-making-data-ready-for-agentic-ai",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://news.ycombinator.com/news",
        "label": "news-ycombinator-com-news",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://overreacted.io/hire-me-in-japan/",
        "label": "overreacted-io-hire-me-in-japan",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/about/",
        "label": "simonwillison-net-about",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/2026/10/06/the-results-of-the-2026-developer-survey-are-here/",
        "label": "stackoverflow-blog-2026-10-06-the-results-of-the-2026-developer-survey",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/2002/01/06/fire-and-motion/",
        "label": "joelonsoftware-com-2002-01-06-fire-and-motion",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/cxo",
        "label": "theregister-com-cxo",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/culture/",
        "label": "arstechnica-com-culture",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/it-it/",
        "label": "blog-cloudflare-com-it-it",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/2025/icons-in-menus/",
        "label": "blog-jim-nielsen-com-2025-icons-in-menus",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://github.blog/ai-and-ml/llms/",
        "label": "github-blog-ai-and-ml-llms",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/blog/2024/09/27/some-go-web-dev-notes/",
        "label": "jvns-ca-blog-2024-09-27-some-go-web-dev-notes",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://lobste.rs/recent",
        "label": "lobste-rs-recent",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://martinfowler.com/articles/never-send-slides/",
        "label": "martinfowler-com-articles-never-send-slides",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://overreacted.io/how-i-vibed-a-proof-of-conways-conjecture/",
        "label": "overreacted-io-how-i-vibed-a-proof-of-conways-conjecture",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/blogmarks/",
        "label": "simonwillison-net-blogmarks",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/2026/10/28/delivering-controlled-and-concentrated-context/",
        "label": "stackoverflow-blog-2026-10-28-delivering-controlled-and-concentrated-c",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/2002/02/13/the-iceberg-secret-revealed/",
        "label": "joelonsoftware-com-2002-02-13-the-iceberg-secret-revealed",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/cyber_crime",
        "label": "theregister-com-cyber-crime",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/features/",
        "label": "arstechnica-com-features",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/ja-jp/",
        "label": "blog-cloudflare-com-ja-jp",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/2025/my-atomic-rabbithole/",
        "label": "blog-jim-nielsen-com-2025-my-atomic-rabbithole",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://github.blog/ai-and-ml/machine-learning/",
        "label": "github-blog-ai-and-ml-machine-learning",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/blog/2025/06/10/how-to-compile-a-c-program/",
        "label": "jvns-ca-blog-2025-06-10-how-to-compile-a-c-program",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://lobste.rs/search",
        "label": "lobste-rs-search",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://martinfowler.com/articles/never-send-slides/build-your-slide-template.html",
        "label": "martinfowler-com-articles-never-send-slides-build-your-slide-template",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://overreacted.io/how-imports-work-in-rsc/",
        "label": "overreacted-io-how-imports-work-in-rsc",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/elsewhere/",
        "label": "simonwillison-net-elsewhere",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/2026/10/29/the-reports-of-stack-overflow-s-death-are-greatly-exaggerated/",
        "label": "stackoverflow-blog-2026-10-29-the-reports-of-stack-overflow-s-death-ar",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/2002/11/11/the-law-of-leaky-abstractions/",
        "label": "joelonsoftware-com-2002-11-11-the-law-of-leaky-abstractions",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/databases",
        "label": "theregister-com-databases",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/gadgets/",
        "label": "arstechnica-com-gadgets",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/ko-kr/",
        "label": "blog-cloudflare-com-ko-kr",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/2026/celebrating-sim-cards/",
        "label": "blog-jim-nielsen-com-2026-celebrating-sim-cards",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://github.blog/changelog/",
        "label": "github-blog-changelog",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/blog/2025/06/24/new-zine--the-secret-rules-of-the-terminal/",
        "label": "jvns-ca-blog-2025-06-24-new-zine-the-secret-rules-of-the-terminal",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://lobste.rs/t/historical",
        "label": "lobste-rs-t-historical",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://martinfowler.com/articles/never-send-slides/slide-principles.html",
        "label": "martinfowler-com-articles-never-send-slides-slide-principles",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://overreacted.io/how-to-fix-any-bug/",
        "label": "overreacted-io-how-to-fix-any-bug",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/elsewhere/release/",
        "label": "simonwillison-net-elsewhere-release",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/2026/11/03/oh-your-agents-need-oauth2-too/",
        "label": "stackoverflow-blog-2026-11-03-oh-your-agents-need-oauth2-too",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/2003/10/08/the-absolute-minimum-every-software-developer-absolutely-positively-must-know-about-unicode-and-character-sets-no-excuses/",
        "label": "joelonsoftware-com-2003-10-08-the-absolute-minimum-every-software-deve",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/devops",
        "label": "theregister-com-devops",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/gadgets/2026/11/this-48-e-paper-display-can-be-whatever-you-want-it-to-be/",
        "label": "arstechnica-com-gadgets-2026-11-this-48-e-paper-display-can-be-whateve",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/tag/ai/",
        "label": "blog-cloudflare-com-tag-ai",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/2026/dear-software-makers/",
        "label": "blog-jim-nielsen-com-2026-dear-software-makers",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://github.blog/developer-skills/",
        "label": "github-blog-developer-skills",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/blog/2025/10/10/notes-on-switching-to-helix-from-vim/",
        "label": "jvns-ca-blog-2025-10-10-notes-on-switching-to-helix-from-vim",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://lobste.rs/t/linux",
        "label": "lobste-rs-t-linux",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://martinfowler.com/articles/software-meets-hardware.html",
        "label": "martinfowler-com-articles-software-meets-hardware",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://overreacted.io/im-doing-a-little-consulting/",
        "label": "overreacted-io-im-doing-a-little-consulting",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/entries/",
        "label": "simonwillison-net-entries",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/agentic-ai/",
        "label": "stackoverflow-blog-agentic-ai",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/2004/06/13/how-microsoft-lost-the-api-war/",
        "label": "joelonsoftware-com-2004-06-13-how-microsoft-lost-the-api-war",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/edge_iot",
        "label": "theregister-com-edge-iot",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/gaming/",
        "label": "arstechnica-com-gaming",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/tag/cloudflare-radar/",
        "label": "blog-cloudflare-com-tag-cloudflare-radar",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/2026/icons-in-every-menu/",
        "label": "blog-jim-nielsen-com-2026-icons-in-every-menu",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://github.blog/developer-skills/application-development/",
        "label": "github-blog-developer-skills-application-development",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/blog/2026/01/08/a-data-model-for-git/",
        "label": "jvns-ca-blog-2026-01-08-a-data-model-for-git",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://lobste.rs/t/unix",
        "label": "lobste-rs-t-unix",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://martinfowler.com/bliki/AgenticEngineering.html",
        "label": "martinfowler-com-bliki-agenticengineering",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://overreacted.io/introducing-rsc-explorer/",
        "label": "overreacted-io-introducing-rsc-explorer",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/guides/",
        "label": "simonwillison-net-guides",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/ai/",
        "label": "stackoverflow-blog-ai",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/2005/12/29/the-perils-of-javaschools-2/",
        "label": "joelonsoftware-com-2005-12-29-the-perils-of-javaschools-2",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/hpc",
        "label": "theregister-com-hpc",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/gaming/2026/11/googles-legend-of-zelda-easter-egg-transports-you-back-to-1998/",
        "label": "arstechnica-com-gaming-2026-11-googles-legend-of-zelda-easter-egg-tran",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/tag/developers/",
        "label": "blog-cloudflare-com-tag-developers",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/2026/macos-dock-icons-over-time/",
        "label": "blog-jim-nielsen-com-2026-macos-dock-icons-over-time",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://github.blog/developer-skills/career-growth/",
        "label": "github-blog-developer-skills-career-growth",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/blog/2026/01/27/some-notes-on-starting-to-use-django/",
        "label": "jvns-ca-blog-2026-01-27-some-notes-on-starting-to-use-django",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://martinfowler.com/bliki/BoundedContext.html",
        "label": "martinfowler-com-bliki-boundedcontext",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://overreacted.io/open-social/",
        "label": "overreacted-io-open-social",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/newsletters/",
        "label": "simonwillison-net-newsletters",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/author/pbeaudette/",
        "label": "stackoverflow-blog-author-pbeaudette",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/2006/04/11/the-development-abstraction-layer-2/",
        "label": "joelonsoftware-com-2006-04-11-the-development-abstraction-layer-2",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/networks",
        "label": "theregister-com-networks",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/health/",
        "label": "arstechnica-com-health",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/tag/life-at-cloudflare/",
        "label": "blog-cloudflare-com-tag-life-at-cloudflare",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/2026/skip-retirement-for-utopia/",
        "label": "blog-jim-nielsen-com-2026-skip-retirement-for-utopia",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://github.blog/developer-skills/github-education/",
        "label": "github-blog-developer-skills-github-education",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/blog/2026/02/18/man-pages/",
        "label": "jvns-ca-blog-2026-02-18-man-pages",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://martinfowler.com/bliki/ExtremeProgramming.html",
        "label": "martinfowler-com-bliki-extremeprogramming",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://overreacted.io/suppressions-of-suppressions/",
        "label": "overreacted-io-suppressions-of-suppressions",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/newsletters/monthly-2026-09-september/",
        "label": "simonwillison-net-newsletters-monthly-2026-09-september",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/business/",
        "label": "stackoverflow-blog-business",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/2008/04/16/stackoverflowcom/",
        "label": "joelonsoftware-com-2008-04-16-stackoverflowcom",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/off_prem",
        "label": "theregister-com-off-prem",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/health/2026/11/third-person-dies-of-bird-flu-since-virus-started-jumping-to-livestock/",
        "label": "arstechnica-com-health-2026-11-third-person-dies-of-bird-flu-since-vir",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/tag/partners/",
        "label": "blog-cloudflare-com-tag-partners",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/2026/why-dad-didnt-have-cellphone/",
        "label": "blog-jim-nielsen-com-2026-why-dad-didnt-have-cellphone",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://github.blog/developer-skills/github/",
        "label": "github-blog-developer-skills-github",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/blog/2026/03/10/examples-for-the-tcpdump-and-dig-man-pages/",
        "label": "jvns-ca-blog-2026-03-10-examples-for-the-tcpdump-and-dig-man-pages",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://martinfowler.com/bliki/LLM-Voice.html",
        "label": "martinfowler-com-bliki-llm-voice",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://overreacted.io/the-math-is-haunted/",
        "label": "overreacted-io-the-math-is-haunted",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/notes/",
        "label": "simonwillison-net-notes",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/company/",
        "label": "stackoverflow-blog-company",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/2020/06/18/hash-a-free-online-platform-for-modeling-the-world/",
        "label": "joelonsoftware-com-2020-06-18-hash-a-free-online-platform-for-modeling",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/on_prem",
        "label": "theregister-com-on-prem",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/information-technology/",
        "label": "arstechnica-com-information-technology",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/tag/policy/",
        "label": "blog-cloudflare-com-tag-policy",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/archive/",
        "label": "blog-jim-nielsen-com-archive",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://github.blog/developer-skills/programming-languages-and-frameworks/",
        "label": "github-blog-developer-skills-programming-languages-and-frameworks",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/blog/2026/05/02/testing-vue-components-in-the-browser/",
        "label": "jvns-ca-blog-2026-05-02-testing-vue-components-in-the-browser",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://martinfowler.com/bliki/OpenSpace.html",
        "label": "martinfowler-com-bliki-openspace",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://overreacted.io/there-are-no-instances-in-atproto/",
        "label": "overreacted-io-there-are-no-instances-in-atproto",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/quotations/",
        "label": "simonwillison-net-quotations",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/contributed/",
        "label": "stackoverflow-blog-contributed",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/2021/06/02/kinda-a-big-announcement/",
        "label": "joelonsoftware-com-2021-06-02-kinda-a-big-announcement",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/patches",
        "label": "theregister-com-patches",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/reviews/",
        "label": "arstechnica-com-reviews",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/tag/product-news/",
        "label": "blog-cloudflare-com-tag-product-news",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/feed.html",
        "label": "blog-jim-nielsen-com-feed",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://github.blog/engineering/",
        "label": "github-blog-engineering",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/blog/2026/05/04/css-colour-palettes/",
        "label": "jvns-ca-blog-2026-05-04-css-colour-palettes",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://martinfowler.com/fragments/2026-10-21.html",
        "label": "martinfowler-com-fragments-2026-10-21",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://overreacted.io/where-its-at/",
        "label": "overreacted-io-where-its-at",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/tags/",
        "label": "simonwillison-net-tags",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/newsletter/",
        "label": "stackoverflow-blog-newsletter",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/2022/01/27/making-the-web-better-with-blocks/",
        "label": "joelonsoftware-com-2022-01-27-making-the-web-better-with-blocks",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/personal_tech",
        "label": "theregister-com-personal-tech",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/science/",
        "label": "arstechnica-com-science",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/tag/security/",
        "label": "blog-cloudflare-com-tag-security",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/menu/",
        "label": "blog-jim-nielsen-com-menu",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://github.blog/engineering/architecture-optimization/",
        "label": "github-blog-engineering-architecture-optimization",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/blog/2026/05/15/moving-away-from-tailwind--and-learning-to-structure-my-css-/",
        "label": "jvns-ca-blog-2026-05-15-moving-away-from-tailwind-and-learning-to-stru",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://martinfowler.com/fragments/2026-10-28.html",
        "label": "martinfowler-com-fragments-2026-10-28",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/tags/anthropic/",
        "label": "simonwillison-net-tags-anthropic",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/oauth2/",
        "label": "stackoverflow-blog-oauth2",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/2022/12/19/progress-on-the-block-protocol/",
        "label": "joelonsoftware-com-2022-12-19-progress-on-the-block-protocol",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/public-sector",
        "label": "theregister-com-public-sector",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/science/2026/11/research-roundup-6-cool-science-stories-we-almost-missed-7/",
        "label": "arstechnica-com-science-2026-11-research-roundup-6-cool-science-storie",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/tag/speed-and-reliability/",
        "label": "blog-cloudflare-com-tag-speed-and-reliability",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/posts/hacker-news/",
        "label": "blog-jim-nielsen-com-posts-hacker-news",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://github.blog/engineering/engineering-principles/",
        "label": "github-blog-engineering-engineering-principles",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/blog/2026/07/17/learning-about-running-sqlite/",
        "label": "jvns-ca-blog-2026-07-17-learning-about-running-sqlite",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://martinfowler.com/rachels-ramblings/",
        "label": "martinfowler-com-rachels-ramblings",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/tags/coding-agents/",
        "label": "simonwillison-net-tags-coding-agents",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/open-source/",
        "label": "stackoverflow-blog-open-source",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/about-me/",
        "label": "joelonsoftware-com-about-me",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/research",
        "label": "theregister-com-research",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/search/",
        "label": "arstechnica-com-search",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/tag/zero-trust/",
        "label": "blog-cloudflare-com-tag-zero-trust",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/posts/trending/",
        "label": "blog-jim-nielsen-com-posts-trending",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://github.blog/engineering/infrastructure/",
        "label": "github-blog-engineering-infrastructure",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/blog/2026/07/21/more-nice-django-things/",
        "label": "jvns-ca-blog-2026-07-21-more-nice-django-things",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://martinfowler.com/rachels-ramblings/good-devs-wary.html",
        "label": "martinfowler-com-rachels-ramblings-good-devs-wary",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/tags/generative-ai/",
        "label": "simonwillison-net-tags-generative-ai",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/podcast/",
        "label": "stackoverflow-blog-podcast",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/archives/",
        "label": "joelonsoftware-com-archives",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/saas",
        "label": "theregister-com-saas",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/security/",
        "label": "arstechnica-com-security",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/zh-cn/",
        "label": "blog-cloudflare-com-zh-cn",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/search/",
        "label": "blog-jim-nielsen-com-search",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://github.blog/engineering/platform-security/",
        "label": "github-blog-engineering-platform-security",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/blog/2026/09/27/replacing-the-old-battery-on-rechargeable-bike-lights/",
        "label": "jvns-ca-blog-2026-09-27-replacing-the-old-battery-on-rechargeable-bike",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/tags/llm-pricing/",
        "label": "simonwillison-net-tags-llm-pricing",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/productivity/",
        "label": "stackoverflow-blog-productivity",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/author/joelonsoftware/",
        "label": "joelonsoftware-com-author-joelonsoftware",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/security",
        "label": "theregister-com-security",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://auth.w3.org/login",
        "label": "auth-w3-org-login",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://auth.wikimedia.org/enwiki/wiki/Special:CreateAccount?useformat=desktop&usesul3=1&centralauthLoginToken=c4fbb96132ff5ccd1919472b0f8e9cc3",
        "label": "auth-wikimedia-org-enwiki-wiki-special-createaccount",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/w/api.php?action=featuredfeed&amp;feed=featured&amp;feedformat=atom",
        "label": "en-wikipedia-org-w-api-php",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://html.spec.whatwg.org/multipage/",
        "label": "html-spec-whatwg-org-multipage",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/",
        "label": "rfc-editor-org",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.mozilla.org/en-US/",
        "label": "mozilla-org-en-us",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc2818/",
        "label": "rfc-editor-org-info-rfc2818",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/",
        "label": "w3-org",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://auth.wikimedia.org/enwiki/wiki/Special:UserLogin?useformat=desktop&usesul3=1&returntoquery=%3F&centralauthLoginToken=77a07957259f131c22f68b6778d404e5",
        "label": "auth-wikimedia-org-enwiki-wiki-special-userlogin",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/about/contact/",
        "label": "rfc-editor-org-about-contact",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc9110/",
        "label": "rfc-editor-org-info-rfc9110",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/TR/",
        "label": "w3-org-tr",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/about/rfc-editor/",
        "label": "rfc-editor-org-about-rfc-editor",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/rfc/rfc9110.html",
        "label": "rfc-editor-org-rfc-rfc9110",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/Translations/",
        "label": "w3-org-translations",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/w/index.php?title=Main_Page&amp;action=edit",
        "label": "en-wikipedia-org-w-index-php",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/about/rpc-reports/",
        "label": "rfc-editor-org-about-rpc-reports",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/about/positive-work-environment/",
        "label": "w3-org-about-positive-work-environment",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/authors/ise/ise-checklist/",
        "label": "rfc-editor-org-authors-ise-ise-checklist",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/email/",
        "label": "w3-org-email",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/authors/ise/ise-reviewer-guidelines/",
        "label": "rfc-editor-org-authors-ise-ise-reviewer-guidelines",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/get-involved/",
        "label": "w3-org-get-involved",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/authors/ise/iseb/",
        "label": "rfc-editor-org-authors-ise-iseb",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/get-involved/promote/",
        "label": "w3-org-get-involved-promote",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/authors/rfc-how-to/",
        "label": "rfc-editor-org-authors-rfc-how-to",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/groups/",
        "label": "w3-org-groups",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/authors/rfc-independent-submissions/",
        "label": "rfc-editor-org-authors-rfc-independent-submissions",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/groups/other/tag/",
        "label": "w3-org-groups-other-tag",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/authors/rfc-style-guide/",
        "label": "rfc-editor-org-authors-rfc-style-guide",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/guide/",
        "label": "w3-org-guide",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc1/",
        "label": "rfc-editor-org-info-rfc1",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/invited-experts/",
        "label": "w3-org-invited-experts",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc10/",
        "label": "rfc-editor-org-info-rfc10",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/ja/",
        "label": "w3-org-ja",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Aria",
        "label": "en-wikipedia-org-wiki-aria",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc11/",
        "label": "rfc-editor-org-info-rfc11",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/liaisons/",
        "label": "w3-org-liaisons",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Bass_(voice_type)",
        "label": "en-wikipedia-org-wiki-bass-voice-type",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc12/",
        "label": "rfc-editor-org-info-rfc12",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/membership/",
        "label": "w3-org-membership",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Basso_continuo",
        "label": "en-wikipedia-org-wiki-basso-continuo",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc13/",
        "label": "rfc-editor-org-info-rfc13",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/membership/list/",
        "label": "w3-org-membership-list",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Cello",
        "label": "en-wikipedia-org-wiki-cello",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc15/",
        "label": "rfc-editor-org-info-rfc15",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/mission/accessibility/",
        "label": "w3-org-mission-accessibility",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Christoph_Birkmann",
        "label": "en-wikipedia-org-wiki-christoph-birkmann",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc16/",
        "label": "rfc-editor-org-info-rfc16",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/mission/internationalization/",
        "label": "w3-org-mission-internationalization",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Church_cantata",
        "label": "en-wikipedia-org-wiki-church-cantata",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc17/",
        "label": "rfc-editor-org-info-rfc17",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/mission/privacy/",
        "label": "w3-org-mission-privacy",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/English_language",
        "label": "en-wikipedia-org-wiki-english-language",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc18/",
        "label": "rfc-editor-org-info-rfc18",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/standards/",
        "label": "w3-org-standards",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Europejara",
        "label": "en-wikipedia-org-wiki-europejara",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc19/",
        "label": "rfc-editor-org-info-rfc19",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/standards/about/",
        "label": "w3-org-standards-about",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Free_content",
        "label": "en-wikipedia-org-wiki-free-content",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc2/",
        "label": "rfc-editor-org-info-rfc2",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/standards/review/",
        "label": "w3-org-standards-review",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Help:Contents",
        "label": "en-wikipedia-org-wiki-help-contents",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc20/",
        "label": "rfc-editor-org-info-rfc20",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/standards/types/",
        "label": "w3-org-standards-types",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Help:Introduction",
        "label": "en-wikipedia-org-wiki-help-introduction",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc21/",
        "label": "rfc-editor-org-info-rfc21",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/support-us/",
        "label": "w3-org-support-us",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Help:Introduction_to_Wikipedia",
        "label": "en-wikipedia-org-wiki-help-introduction-to-wikipedia",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc22/",
        "label": "rfc-editor-org-info-rfc22",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.w3.org/zh-hans/",
        "label": "w3-org-zh-hans",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Ich_geh_und_suche_mit_Verlangen,_BWV_49",
        "label": "en-wikipedia-org-wiki-ich-geh-und-suche-mit-verlangen-bwv-49",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc23/",
        "label": "rfc-editor-org-info-rfc23",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Jesus",
        "label": "en-wikipedia-org-wiki-jesus",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc24/",
        "label": "rfc-editor-org-info-rfc24",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Johann_Sebastian_Bach",
        "label": "en-wikipedia-org-wiki-johann-sebastian-bach",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc25/",
        "label": "rfc-editor-org-info-rfc25",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Klaus_Hofmann",
        "label": "en-wikipedia-org-wiki-klaus-hofmann",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc27/",
        "label": "rfc-editor-org-info-rfc27",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Libretto",
        "label": "en-wikipedia-org-wiki-libretto",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc28/",
        "label": "rfc-editor-org-info-rfc28",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Main_Page",
        "label": "en-wikipedia-org-wiki-main-page",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc29/",
        "label": "rfc-editor-org-info-rfc29",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Movement_(music)",
        "label": "en-wikipedia-org-wiki-movement-music",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc3/",
        "label": "rfc-editor-org-info-rfc3",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Parable_of_the_Great_Banquet",
        "label": "en-wikipedia-org-wiki-parable-of-the-great-banquet",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc30/",
        "label": "rfc-editor-org-info-rfc30",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Pipe_organ",
        "label": "en-wikipedia-org-wiki-pipe-organ",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc31/",
        "label": "rfc-editor-org-info-rfc31",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Portal:Current_events",
        "label": "en-wikipedia-org-wiki-portal-current-events",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc32/",
        "label": "rfc-editor-org-info-rfc32",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Recitative",
        "label": "en-wikipedia-org-wiki-recitative",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc33/",
        "label": "rfc-editor-org-info-rfc33",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Shameemul_Haq",
        "label": "en-wikipedia-org-wiki-shameemul-haq",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc34/",
        "label": "rfc-editor-org-info-rfc34",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Sinfonia",
        "label": "en-wikipedia-org-wiki-sinfonia",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc35/",
        "label": "rfc-editor-org-info-rfc35",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Song_of_Songs",
        "label": "en-wikipedia-org-wiki-song-of-songs",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc36/",
        "label": "rfc-editor-org-info-rfc36",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Soprano",
        "label": "en-wikipedia-org-wiki-soprano",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc37/",
        "label": "rfc-editor-org-info-rfc37",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Sovetsky_Soyuz-class_battleship",
        "label": "en-wikipedia-org-wiki-sovetsky-soyuz-class-battleship",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc38/",
        "label": "rfc-editor-org-info-rfc38",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Special:RecentChanges",
        "label": "en-wikipedia-org-wiki-special-recentchanges",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc39/",
        "label": "rfc-editor-org-info-rfc39",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Special:RecentChangesLinked/Main_Page",
        "label": "en-wikipedia-org-wiki-special-recentchangeslinked-main-page",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc4/",
        "label": "rfc-editor-org-info-rfc4",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Special:Search",
        "label": "en-wikipedia-org-wiki-special-search",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc40/",
        "label": "rfc-editor-org-info-rfc40",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Special:SpecialPages",
        "label": "en-wikipedia-org-wiki-special-specialpages",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc41/",
        "label": "rfc-editor-org-info-rfc41",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Special:Statistics",
        "label": "en-wikipedia-org-wiki-special-statistics",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc42/",
        "label": "rfc-editor-org-info-rfc42",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Special:WhatLinksHere/Main_Page",
        "label": "en-wikipedia-org-wiki-special-whatlinkshere-main-page",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc43/",
        "label": "rfc-editor-org-info-rfc43",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Stanza",
        "label": "en-wikipedia-org-wiki-stanza",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc44/",
        "label": "rfc-editor-org-info-rfc44",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Talk:Main_Page",
        "label": "en-wikipedia-org-wiki-talk-main-page",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc45/",
        "label": "rfc-editor-org-info-rfc45",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Trinity_Sunday",
        "label": "en-wikipedia-org-wiki-trinity-sunday",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc46/",
        "label": "rfc-editor-org-info-rfc46",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Wie_sch%C3%B6n_leuchtet_der_Morgenstern",
        "label": "en-wikipedia-org-wiki-wie-sch-c3-b6n-leuchtet-der-morgenstern",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc47/",
        "label": "rfc-editor-org-info-rfc47",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Wikipedia",
        "label": "en-wikipedia-org-wiki-wikipedia",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc48/",
        "label": "rfc-editor-org-info-rfc48",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Wikipedia:About",
        "label": "en-wikipedia-org-wiki-wikipedia-about",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc49/",
        "label": "rfc-editor-org-info-rfc49",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Wikipedia:Community_portal",
        "label": "en-wikipedia-org-wiki-wikipedia-community-portal",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc5/",
        "label": "rfc-editor-org-info-rfc5",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Wikipedia:Contact_us",
        "label": "en-wikipedia-org-wiki-wikipedia-contact-us",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc50/",
        "label": "rfc-editor-org-info-rfc50",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Wikipedia:Contents",
        "label": "en-wikipedia-org-wiki-wikipedia-contents",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc51/",
        "label": "rfc-editor-org-info-rfc51",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Wikipedia:File_Upload_Wizard",
        "label": "en-wikipedia-org-wiki-wikipedia-file-upload-wizard",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc52/",
        "label": "rfc-editor-org-info-rfc52",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc53/",
        "label": "rfc-editor-org-info-rfc53",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Zombie_Nightmare",
        "label": "en-wikipedia-org-wiki-zombie-nightmare",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc54/",
        "label": "rfc-editor-org-info-rfc54",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc55/",
        "label": "rfc-editor-org-info-rfc55",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://www.rfc-editor.org/info/rfc56/",
        "label": "rfc-editor-org-info-rfc56",
        "kind": "article",
        "expect": [],
    },
    {
        "url": "https://github.com/",
        "label": "github-com",
        "kind": "repo",
        "expect": [],
    },
    {
        "url": "https://github.com/fastapi/fastapi",
        "label": "github-com-fastapi-fastapi",
        "kind": "repo",
        "expect": [],
    },
    {
        "url": "https://github.com/login?return_to=https%3A%2F%2Fgithub.com%2Fastral-sh%2Fruff",
        "label": "github-com-login",
        "kind": "repo",
        "expect": [],
    },
    {
        "url": "https://github.com/pallets/flask",
        "label": "github-com-pallets-flask",
        "kind": "repo",
        "expect": [],
    },
    {
        "url": "https://github.com/psf/requests",
        "label": "github-com-psf-requests",
        "kind": "repo",
        "expect": [],
    },
    {
        "url": "https://angular.dev/events/v22",
        "label": "angular-dev-events-v22",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/space/",
        "label": "arstechnica-com-space",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.cloudflare.com/zh-tw/",
        "label": "blog-cloudflare-com-zh-tw",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://blog.jim-nielsen.com/subscribe/",
        "label": "blog-jim-nielsen-com-subscribe",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/CSS/How_to/Layout_cookbook/Center_an_element",
        "label": "developer-mozilla-org-en-us-docs-web-css-how-to-layout-cookbook-center",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://doc.rust-lang.org/cargo/reference/publishing.html",
        "label": "doc-rust-lang-org-cargo-reference-publishing",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.docker.com/ai/docker-agent/getting-started/introduction/",
        "label": "docs-docker-com-ai-docker-agent-getting-started-introduction",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.aiohttp.org/en/stable/logging.html",
        "label": "docs-aiohttp-org-en-stable-logging",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astral.sh/ruff/formatter/",
        "label": "docs-astral-sh-ruff-formatter",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astro.build/en/tutorial/3-components/",
        "label": "docs-astro-build-en-tutorial-3-components",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.celeryq.dev/en/stable/py-modindex.html",
        "label": "docs-celeryq-dev-en-stable-py-modindex",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.djangoproject.com/en/3.2/",
        "label": "docs-djangoproject-com-en-3-2",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.kernel.org/kbuild/index.html",
        "label": "docs-kernel-org-kbuild-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.npmjs.com/creating-a-new-npm-user-account/",
        "label": "docs-npmjs-com-creating-a-new-npm-user-account",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/stable/how-to/index.html",
        "label": "docs-pytest-org-en-stable-how-to-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.python.org/3.7/",
        "label": "docs-python-org-3-7",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.ros.org/en/lyrical/Get-Started/Installation/Ubuntu-Install-Debs.html",
        "label": "docs-ros-org-en-lyrical-get-started-installation-ubuntu-install-debs",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://expressjs.com/en/5x/starter/faq/",
        "label": "expressjs-com-en-5x-starter-faq",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://fastapi.tiangolo.com/learn/",
        "label": "fastapi-tiangolo-com-learn",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://flask.palletsprojects.com/en/stable/py-modindex/",
        "label": "flask-palletsprojects-com-en-stable-py-modindex",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://git-scm.com/docs/git-branch",
        "label": "git-scm-com-docs-git-branch",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://github.blog/engineering/user-experience/",
        "label": "github-blog-engineering-user-experience",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://go.dev/doc/gc-guide",
        "label": "go-dev-doc-gc-guide",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://jestjs.io/docs/mock-functions",
        "label": "jestjs-io-docs-mock-functions",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/categories/favorite/",
        "label": "jvns-ca-categories-favorite",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/docs/concepts/resource-management/resource-managers/",
        "label": "kubernetes-io-docs-concepts-resource-management-resource-managers",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://man7.org/linux/man-pages/dir_section_5.html",
        "label": "man7-org-linux-man-pages-dir-section-5",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/API/AbortSignal/abort_event",
        "label": "developer-mozilla-org-en-us-docs-web-api-abortsignal-abort-event",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mochajs.org/features/diffs/",
        "label": "mochajs-org-features-diffs",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mypy.readthedocs.io/en/stable/generics.html",
        "label": "mypy-readthedocs-io-en-stable-generics",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nextjs.org/docs/app/getting-started/images",
        "label": "nextjs-org-docs-app-getting-started-images",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nodejs.org/en/blog/weekly/weekly-update2015-04-10",
        "label": "nodejs-org-en-blog-weekly-weekly-update2015-04-10",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://numpy.org/doc/stable/license.html",
        "label": "numpy-org-doc-stable-license",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pandas.pydata.org/docs/getting_started/comparison/index.html",
        "label": "pandas-pydata-org-docs-getting-started-comparison-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pip.pypa.io/en/stable/search/",
        "label": "pip-pypa-io-en-stable-search",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/about/licence/",
        "label": "postgresql-org-about-licence",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/8.3.x/",
        "label": "docs-pytest-org-en-8-3-x",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://react.dev/learn/importing-and-exporting-components",
        "label": "react-dev-learn-importing-and-exporting-components",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/develop/programmability/functions-intro/",
        "label": "redis-io-docs-latest-develop-programmability-functions-intro",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://redis.io/docs/latest/develop/clients/go/",
        "label": "redis-io-docs-latest-develop-clients-go",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://requests.readthedocs.io/en/latest/dev/contributing/",
        "label": "requests-readthedocs-io-en-latest-dev-contributing",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://setuptools.pypa.io/en/latest/userguide/entry_point.html",
        "label": "setuptools-pypa-io-en-latest-userguide-entry-point",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://simonwillison.net/tags/llm/",
        "label": "simonwillison-net-tags-llm",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://stackoverflow.blog/releases/",
        "label": "stackoverflow-blog-releases",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://svelte.dev/docs/svelte/$effect",
        "label": "svelte-dev-docs-svelte-effect",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://tailwindcss.com/docs/dark-mode",
        "label": "tailwindcss-com-docs-dark-mode",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.hashicorp.com/boundary/api-docs/credential-service",
        "label": "developer-hashicorp-com-boundary-api-docs-credential-service",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://vuejs.org/error-reference/",
        "label": "vuejs-org-error-reference",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://webpack.js.org/concepts/module-federation/",
        "label": "webpack-js-org-concepts-module-federation",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.freedesktop.org/wiki/Software/desktop-file-utils/",
        "label": "freedesktop-org-wiki-software-desktop-file-utils",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.joelonsoftware.com/buy-the-books/",
        "label": "joelonsoftware-com-buy-the-books",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/docs/16/index.html",
        "label": "postgresql-org-docs-16-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.sqlite.org/faq.html",
        "label": "sqlite-org-faq",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.theregister.com/software",
        "label": "theregister-com-software",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://www.typescriptlang.org/docs/handbook/2/keyof-types.html",
        "label": "typescriptlang-org-docs-handbook-2-keyof-types",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://angular.dev/guide",
        "label": "angular-dev-guide",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://arstechnica.com/space/2026/11/at-home-in-space-opens-caps-air-and-space-museum-renovation/",
        "label": "arstechnica-com-space-2026-11-at-home-in-space-opens-caps-air-and-spac",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/CSS/How_to/Layout_cookbook/Column_layouts",
        "label": "developer-mozilla-org-en-us-docs-web-css-how-to-layout-cookbook-column",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://doc.rust-lang.org/nomicon/index.html",
        "label": "doc-rust-lang-org-nomicon-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.docker.com/ai/docker-agent/providers/overview/",
        "label": "docs-docker-com-ai-docker-agent-providers-overview",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.aiohttp.org/en/stable/search.html",
        "label": "docs-aiohttp-org-en-stable-search",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astral.sh/ruff/installation/",
        "label": "docs-astral-sh-ruff-installation",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.astro.build/en/tutorial/4-layouts/",
        "label": "docs-astro-build-en-tutorial-4-layouts",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.celeryq.dev/en/stable/search.html",
        "label": "docs-celeryq-dev-en-stable-search",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.djangoproject.com/en/4.0/",
        "label": "docs-djangoproject-com-en-4-0",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.kernel.org/kernel-hacking/index.html",
        "label": "docs-kernel-org-kernel-hacking-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.npmjs.com/creating-a-strong-password/",
        "label": "docs-npmjs-com-creating-a-strong-password",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/stable/how-to/logging.html",
        "label": "docs-pytest-org-en-stable-how-to-logging",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.python.org/3.8/",
        "label": "docs-python-org-3-8",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.ros.org/en/lyrical/Get-Started/Installation/Windows-Install-Binary.html",
        "label": "docs-ros-org-en-lyrical-get-started-installation-windows-install-binar",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://expressjs.com/en/5x/starter/generator/",
        "label": "expressjs-com-en-5x-starter-generator",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://fastapi.tiangolo.com/newsletter/",
        "label": "fastapi-tiangolo-com-newsletter",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://git-scm.com/docs/git-bugreport",
        "label": "git-scm-com-docs-git-bugreport",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://go.dev/doc/install",
        "label": "go-dev-doc-install",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://jestjs.io/docs/more-resources",
        "label": "jestjs-io-docs-more-resources",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://jvns.ca/newsletter/",
        "label": "jvns-ca-newsletter",
        "kind": "blog",
        "expect": [],
    },
    {
        "url": "https://kubernetes.io/docs/concepts/scheduling-eviction/kube-scheduler/",
        "label": "kubernetes-io-docs-concepts-scheduling-eviction-kube-scheduler",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://man7.org/linux/man-pages/dir_section_6.html",
        "label": "man7-org-linux-man-pages-dir-section-6",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/API/AbortSignal/abort_static",
        "label": "developer-mozilla-org-en-us-docs-web-api-abortsignal-abort-static",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mochajs.org/features/error-codes/",
        "label": "mochajs-org-features-error-codes",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://mypy.readthedocs.io/en/stable/genindex.html",
        "label": "mypy-readthedocs-io-en-stable-genindex",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nextjs.org/docs/app/getting-started/installation",
        "label": "nextjs-org-docs-app-getting-started-installation",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://nodejs.org/en/blog/weekly/weekly-update2015-04-17",
        "label": "nodejs-org-en-blog-weekly-weekly-update2015-04-17",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://numpy.org/doc/stable/numpy_2_0_migration_guide.html",
        "label": "numpy-org-doc-stable-numpy-2-0-migration-guide",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pandas.pydata.org/docs/getting_started/index.html",
        "label": "pandas-pydata-org-docs-getting-started-index",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://pip.pypa.io/en/stable/topics/",
        "label": "pip-pypa-io-en-stable-topics",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://www.postgresql.org/about/policies/",
        "label": "postgresql-org-about-policies",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://docs.pytest.org/en/8.4.x/",
        "label": "docs-pytest-org-en-8-4-x",
        "kind": "documentation",
        "expect": [],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Unicode",
        "label": "unicode-article",
        "kind": "article",
        "expect": ["Unicode"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/JSON",
        "label": "json-article",
        "kind": "article",
        "expect": ["JSON"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/YAML",
        "label": "yaml-article",
        "kind": "article",
        "expect": ["YAML"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Regular_expression",
        "label": "regex-article",
        "kind": "article",
        "expect": ["regular expression"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Hypertext_Transfer_Protocol",
        "label": "http-article",
        "kind": "article",
        "expect": ["HTTP"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/SQL",
        "label": "sql-article",
        "kind": "article",
        "expect": ["SQL"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Git",
        "label": "git-article",
        "kind": "article",
        "expect": ["Git"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Docker_(software)",
        "label": "docker-article",
        "kind": "article",
        "expect": ["Docker"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Kubernetes",
        "label": "kubernetes-article",
        "kind": "article",
        "expect": ["Kubernetes"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Linux",
        "label": "linux-article",
        "kind": "article",
        "expect": ["Linux"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Compiler",
        "label": "compiler-article",
        "kind": "article",
        "expect": ["compiler"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Operating_system",
        "label": "os-article",
        "kind": "article",
        "expect": ["operating system"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Cryptography",
        "label": "crypto-article",
        "kind": "article",
        "expect": ["cryptography"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Machine_learning",
        "label": "ml-article",
        "kind": "article",
        "expect": ["machine learning"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Transformer_(deep_learning_architecture)",
        "label": "transformer-article",
        "kind": "article",
        "expect": ["transformer"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/World_Wide_Web",
        "label": "www-article",
        "kind": "article",
        "expect": ["World Wide Web"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Domain_Name_System",
        "label": "dns-article",
        "kind": "article",
        "expect": ["domain name"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Public_key_certificate",
        "label": "pkix-article",
        "kind": "article",
        "expect": ["certificate"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Representational_state_transfer",
        "label": "rest-article",
        "kind": "article",
        "expect": ["REST"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Uniform_Resource_Identifier",
        "label": "uri-article",
        "kind": "article",
        "expect": ["URI"],
    },
    {
        "url": "https://www.rfc-editor.org/rfc/rfc2616.html",
        "label": "rfc2616",
        "kind": "article",
        # The word "Acknowledgements" does not appear in the source; assert on
        # text from the closing section instead.
        "expect": ["HTTP", "Internet Society"],
    },
    {
        "url": "https://www.rfc-editor.org/rfc/rfc7519.html",
        "label": "rfc7519",
        "kind": "article",
        "expect": ["JSON Web Token"],
    },
    {
        "url": "https://www.rfc-editor.org/rfc/rfc8446.html",
        "label": "rfc8446",
        "kind": "article",
        "expect": ["TLS"],
    },
    {
        "url": "https://www.rfc-editor.org/rfc/rfc3986.html",
        "label": "rfc3986",
        "kind": "article",
        "expect": ["URI"],
    },
    {
        "url": "https://datatracker.ietf.org/doc/html/rfc7231",
        "label": "rfc7231",
        "kind": "article",
        "expect": ["HTTP"],
    },
    {
        "url": "https://www.w3.org/TR/html52/",
        "label": "w3c-html52",
        "kind": "article",
        "expect": ["HTML"],
    },
    {
        "url": "https://docs.python.org/3/library/asyncio.html",
        "label": "stdlib-asyncio",
        "kind": "documentation",
        "expect": ["asyncio"],
    },
    {
        "url": "https://docs.python.org/3/library/re.html",
        "label": "stdlib-re",
        "kind": "documentation",
        "expect": ["regular expression"],
    },
    {
        "url": "https://docs.python.org/3/library/sqlite3.html",
        "label": "stdlib-sqlite3",
        "kind": "documentation",
        "expect": ["sqlite3"],
    },
    {
        "url": "https://docs.python.org/3/library/unittest.html",
        "label": "stdlib-unittest",
        "kind": "documentation",
        "expect": ["unittest"],
    },
    {
        "url": "https://docs.python.org/3/tutorial/index.html",
        "label": "py-tutorial",
        "kind": "documentation",
        "expect": ["Python"],
    },
    {
        "url": "https://docs.python.org/3/library/typing.html",
        "label": "stdlib-typing",
        "kind": "documentation",
        "expect": ["typing"],
    },
    {
        "url": "https://doc.rust-lang.org/book/ch01-00-getting-started.html",
        "label": "rust-getting-started",
        "kind": "documentation",
        "expect": ["Rust"],
    },
    {
        "url": "https://doc.rust-lang.org/book/ch06-00-enums.html",
        "label": "rust-enums",
        "kind": "documentation",
        "expect": ["enum"],
    },
    {
        "url": "https://doc.rust-lang.org/book/ch10-00-generics.html",
        "label": "rust-generics",
        "kind": "documentation",
        "expect": ["generic"],
    },
    {
        "url": "https://doc.rust-lang.org/book/ch13-00-functional-features.html",
        "label": "rust-functional",
        "kind": "documentation",
        "expect": ["closure"],
    },
    {
        "url": "https://doc.rust-lang.org/rust-by-example/index.html",
        "label": "rust-by-example",
        "kind": "documentation",
        "expect": ["Rust"],
    },
    {
        "url": "https://go.dev/doc/faq",
        "label": "go-faq",
        "kind": "documentation",
        "expect": ["Go"],
    },
    {
        "url": "https://docs.djangoproject.com/en/stable/intro/tutorial01/",
        "label": "django-tutorial",
        "kind": "documentation",
        "expect": ["Django"],
    },
    {
        "url": "https://docs.djangoproject.com/en/stable/topics/db/models/",
        "label": "django-models",
        "kind": "documentation",
        "expect": ["model"],
    },
    {
        "url": "https://fastapi.tiangolo.com/tutorial/path-params/",
        "label": "fastapi-path-params",
        "kind": "documentation",
        "expect": ["path"],
    },
    {
        "url": "https://fastapi.tiangolo.com/tutorial/body/",
        "label": "fastapi-body",
        "kind": "documentation",
        "expect": ["body"],
    },
    {
        "url": "https://flask.palletsprojects.com/en/stable/quickstart/",
        "label": "flask-quickstart",
        "kind": "documentation",
        "expect": ["Flask"],
    },
    {
        "url": "https://numpy.org/doc/stable/user/absolute_beginners.html",
        "label": "numpy-beginners",
        "kind": "documentation",
        "expect": ["array"],
    },
    {
        "url": "https://pandas.pydata.org/docs/user_guide/10min.html",
        "label": "pandas-10min",
        "kind": "documentation",
        "expect": ["DataFrame"],
    },
    {
        "url": "https://docs.pytest.org/en/stable/how-to/assert.html",
        "label": "pytest-assert",
        "kind": "documentation",
        "expect": ["assert"],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/JavaScript/Guide/Introduction",
        "label": "mdn-js-intro",
        "kind": "documentation",
        "expect": ["JavaScript"],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/404",
        "label": "mdn-404",
        "kind": "documentation",
        "expect": ["404"],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/CSS/display",
        "label": "mdn-css-display",
        "kind": "documentation",
        "expect": ["display"],
    },
    {
        "url": "https://developer.mozilla.org/en-US/docs/Web/API/Fetch_API/Using_Fetch",
        "label": "mdn-fetch",
        "kind": "documentation",
        "expect": ["fetch"],
    },
    {
        "url": "https://www.postgresql.org/docs/current/tutorial-select.html",
        "label": "pg-select",
        "kind": "documentation",
        "expect": ["SELECT"],
    },
    {
        "url": "https://www.postgresql.org/docs/current/sql-createindex.html",
        "label": "pg-createindex",
        "kind": "documentation",
        "expect": ["INDEX"],
    },
    {
        "url": "https://redis.io/docs/latest/develop/data-types/",
        "label": "redis-data-types",
        "kind": "documentation",
        "expect": ["Redis"],
    },
    {
        "url": "https://docs.docker.com/get-started/",
        "label": "docker-get-started",
        "kind": "documentation",
        "expect": ["Docker"],
    },
    {
        "url": "https://kubernetes.io/docs/concepts/overview/",
        "label": "k8s-concepts",
        "kind": "documentation",
        "expect": ["Kubernetes"],
    },
    {
        "url": "https://git-scm.com/docs/git-rebase",
        "label": "git-rebase",
        "kind": "documentation",
        "expect": ["rebase"],
    },
    {
        "url": "https://www.gnu.org/software/bash/manual/bash.html",
        "label": "bash-manual",
        "kind": "documentation",
        "expect": ["Bash"],
    },
    {
        "url": "https://man7.org/linux/man-pages/man1/grep.1.html",
        "label": "grep-man",
        "kind": "documentation",
        "expect": ["grep"],
    },
    {
        "url": "https://peps.python.org/pep-0020/",
        "label": "pep20",
        "kind": "documentation",
        # "Zen of Python" lives only in <title>; assert on the poem itself.
        "expect": ["Beautiful is better than ugly"],
    },
    {
        "url": "https://peps.python.org/pep-0008/",
        "label": "pep8",
        "kind": "documentation",
        "expect": ["Style Guide"],
    },
    {
        "url": "https://www.sqlite.org/whentouse.html",
        "label": "sqlite-whentouse",
        "kind": "documentation",
        "expect": ["SQLite"],
    },
    {
        "url": "https://www.sqlite.org/quirks.html",
        "label": "sqlite-quirks",
        "kind": "documentation",
        "expect": ["SQLite"],
    },
    {
        "url": "https://www.sqlite.org/lang_select.html",
        "label": "sqlite-select",
        "kind": "documentation",
        "expect": ["SELECT"],
    },
    {
        "url": "https://www.sqlite.org/fileformat2.html",
        "label": "sqlite-fileformat",
        "kind": "documentation",
        "expect": ["format"],
    },
    {
        "url": "https://www.sqlite.org/optoverview.html",
        "label": "sqlite-optoverview",
        "kind": "documentation",
        "expect": ["query"],
    },
    {
        "url": "https://github.com/psf/black",
        "label": "gh-black",
        "kind": "repo",
        "expect": ["Black"],
    },
    {
        "url": "https://github.com/python-poetry/poetry",
        "label": "gh-poetry",
        "kind": "repo",
        "expect": ["Poetry"],
    },
    {
        "url": "https://github.com/tiangolo/fastapi",
        "label": "gh-fastapi",
        "kind": "repo",
        "expect": ["FastAPI"],
    },
    {
        "url": "https://github.com/pandas-dev/pandas",
        "label": "gh-pandas",
        "kind": "repo",
        "expect": ["pandas"],
    },
    {
        "url": "https://github.com/numpy/numpy",
        "label": "gh-numpy",
        "kind": "repo",
        "expect": ["NumPy"],
    },
    {
        "url": "https://github.com/torvalds/linux",
        "label": "gh-linux",
        "kind": "repo",
        "expect": ["Linux"],
    },
    {
        "url": "https://github.com/microsoft/vscode",
        "label": "gh-vscode",
        "kind": "repo",
        "expect": ["Visual Studio Code"],
    },
    {
        "url": "https://github.com/rust-lang/rust",
        "label": "gh-rust",
        "kind": "repo",
        "expect": ["Rust"],
    },
    {
        "url": "https://github.com/golang/go",
        "label": "gh-go",
        "kind": "repo",
        "expect": ["Go"],
    },
    {
        "url": "https://github.com/nodejs/node",
        "label": "gh-node",
        "kind": "repo",
        "expect": ["Node.js"],
    },
    {
        "url": "https://github.com/django/django",
        "label": "gh-django",
        "kind": "repo",
        "expect": ["Django"],
    },
    {
        "url": "https://github.com/encode/httpx",
        "label": "gh-httpx",
        "kind": "repo",
        "expect": ["HTTP"],
    },
    {
        "url": "https://github.com/pytest-dev/pytest",
        "label": "gh-pytest",
        "kind": "repo",
        "expect": ["pytest"],
    },
    {
        "url": "https://github.com/pre-commit/pre-commit",
        "label": "gh-precommit",
        "kind": "repo",
        "expect": ["pre-commit"],
    },
    {
        "url": "https://github.com/astral-sh/ruff",
        "label": "gh-ruff",
        "kind": "repo",
        "expect": ["Ruff"],
    },
    {
        "url": "https://github.com/python/cpython",
        "label": "gh-cpython",
        "kind": "repo",
        "expect": ["Python"],
    },
    {
        "url": "https://github.com/redis/redis",
        "label": "gh-redis",
        "kind": "repo",
        "expect": ["Redis"],
    },
    {
        "url": "https://github.com/kubernetes/kubernetes",
        "label": "gh-kubernetes",
        "kind": "repo",
        "expect": ["Kubernetes"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/List_of_programming_languages",
        "label": "languages-table",
        "kind": "table",
        "expect": ["language"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Comparison_of_programming_languages",
        "label": "language-compare-table",
        "kind": "table",
        "expect": ["language"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/List_of_HTTP_status_codes",
        "label": "http-status-table",
        "kind": "table",
        "expect": ["200"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes",
        "label": "iso639-table",
        "kind": "table",
        "expect": ["language"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/List_of_countries_and_dependencies_by_population",
        "label": "population-table",
        "kind": "table",
        "expect": ["population"],
    },
    {
        "url": "https://en.wikipedia.org/wiki/Comparison_of_web_browsers",
        "label": "browsers-table",
        "kind": "table",
        "expect": ["browser"],
    },
    {
        "url": "https://example.org/",
        "label": "example-org",
        "kind": "edge",
        "expect": ["documentation examples"],
    },
    {
        "url": "https://www.iana.org/help/example-domains",
        "label": "iana-example",
        "kind": "edge",
        "expect": ["example"],
    },
]


@dataclass
class PageResult:
    label: str
    kind: str
    url: str
    status: str
    html_bytes: int = 0
    html_tokens: int = 0
    md_tokens: int = 0
    extraction_method: str = ""
    duration_ms: float = 0.0
    note: str = ""
    missing: tuple[str, ...] = ()


def _tokenizer(spec: str):
    if spec == "whitespace":
        return "whitespace", lambda t: len(t.split())
    if spec.startswith("tiktoken:"):
        try:
            import tiktoken
        except ImportError as exc:
            raise SystemExit(
                "tiktoken is not installed; run `uv sync --frozen --extra bench` "
                "or pass --tokenizer whitespace"
            ) from exc
        name = spec.split(":", 1)[1]
        enc = tiktoken.get_encoding(name)
        return f"tiktoken:{name}", lambda t: len(enc.encode(t))
    raise SystemExit(f"unknown tokenizer spec: {spec}")


def _fetch(url: str, timeout: float = 30.0) -> tuple[str, int]:
    headers = {"User-Agent": "domstrip-benchmark/1.0 (+https://domstrip.dev)"}
    with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.text, len(resp.content)


def _cache_path(cache: Path, label: str) -> Path:
    digest = hashlib.sha256(label.encode()).hexdigest()[:12]
    return cache / f"{label}-{digest}.html"


def load_page(source: dict[str, object], cache: Path, *, refresh: bool) -> tuple[str, int, str]:
    """Return ``(html, html_bytes, note)`` from cache, fetching if needed."""
    label = str(source["label"])
    path = _cache_path(cache, label)
    if path.exists() and not refresh:
        body = path.read_bytes()
        return body.decode("utf-8", "replace"), len(body), "cached"
    html, size = _fetch(str(source["url"]))
    cache.mkdir(parents=True, exist_ok=True)
    path.write_bytes(html.encode("utf-8"))
    return html, size, "fetched"


def _missing_content(markdown: str, expect: object) -> tuple[str, ...]:
    if not expect:
        return ()
    folded = markdown.casefold()
    return tuple(s for s in expect if str(s).casefold() not in folded)  # type: ignore[union-attr]


def run(cache: Path, tokenizer_spec: str, *, refresh: bool) -> tuple[list[PageResult], str]:
    label, count = _tokenizer(tokenizer_spec)
    results: list[PageResult] = []

    for source in SOURCES:
        name = str(source["label"])
        try:
            html, size, note = load_page(source, cache, refresh=refresh)
        except Exception as exc:
            results.append(
                PageResult(
                    label=name,
                    kind=str(source["kind"]),
                    url=str(source["url"]),
                    status="fetch_error",
                    note=f"{type(exc).__name__}: {exc}"[:200],
                )
            )
            continue

        start = time.perf_counter()
        try:
            out = clean(html, "auto")
        except AppError as exc:
            results.append(
                PageResult(
                    label=name,
                    kind=str(source["kind"]),
                    url=str(source["url"]),
                    status=f"rejected:{exc.code}",
                    html_bytes=size,
                    note=note,
                )
            )
            continue
        elapsed = (time.perf_counter() - start) * 1000.0

        results.append(
            PageResult(
                label=name,
                kind=str(source["kind"]),
                url=str(source["url"]),
                status="ok",
                html_bytes=size,
                html_tokens=count(html),
                md_tokens=count(out.markdown),
                extraction_method=out.extraction_method,
                duration_ms=round(elapsed, 1),
                note=note,
                missing=_missing_content(out.markdown, source.get("expect")),
            )
        )
    return results, label


def _pct(md: int, base: int) -> str:
    if not base:
        return "n/a"
    return f"{(1 - md / base):+.2%}"


def report(results: list[PageResult], tokenizer: str, *, verify: bool) -> int:
    ok = [r for r in results if r.status == "ok"]
    if not ok:
        print("No pages measured successfully.", file=sys.stderr)
        return 1

    print(f"tokenizer={tokenizer}  pages_ok={len(ok)}  metric=1-(md/html)\n")
    header = f"{'page':<22}{'kind':<16}{'KB':>7}{'html':>9}{'md':>8}{'reduction':>11}{'mode':>10}"
    print(header)
    print("-" * len(header))
    for r in ok:
        print(
            f"{r.label:<22}{r.kind:<16}{r.html_bytes / 1024:>7.0f}{r.html_tokens:>9}"
            f"{r.md_tokens:>8}{_pct(r.md_tokens, r.html_tokens):>11}"
            f"{r.extraction_method:>10}"
        )

    # Macro average: each page weighted equally, so one 4 MB table cannot
    # dominate the headline the way a pooled token total would.
    by_kind: dict[str, list[PageResult]] = defaultdict(list)
    for r in ok:
        by_kind[r.kind].append(r)

    print("\nper page type (mean of per-page reduction vs raw HTML)")
    for kind in sorted(by_kind):
        rows = by_kind[kind]
        mean = sum(r.md_tokens / r.html_tokens for r in rows if r.html_tokens) / len(rows)
        print(f"  {kind:<16} n={len(rows):<3} reduction={1 - mean:+.2%}")

    ratios = [r.md_tokens / r.html_tokens for r in ok if r.html_tokens]
    macro = sum(ratios) / len(ratios)
    pooled_html = sum(r.html_tokens for r in ok)
    pooled_md = sum(r.md_tokens for r in ok)
    print("\nmacro average (each page equally weighted)")
    print(f"  reduction={1 - macro:+.2%}  median={1 - sorted(ratios)[len(ratios) // 2]:+.2%}")
    print(f"  pooled={_pct(pooled_md, pooled_html)} (dominated by the largest pages)")

    durations = sorted(r.duration_ms for r in ok)
    p95 = durations[max(0, int(len(durations) * 0.95) - 1)]
    print(f"  p95_extraction_ms={p95:.1f}")

    failed = [r for r in results if r.status != "ok"]
    if failed:
        print(f"\nnot measured ({len(failed)}):")
        for r in failed:
            print(f"  {r.label:<22}{r.status:<26}{r.note[:70]}")

    dropped = [r for r in ok if r.missing]
    if dropped:
        print(f"\nCONTENT MISSING ({len(dropped)}):")
        for r in dropped:
            print(f"  {r.label:<22}drops {', '.join(r.missing)}")
        if verify:
            return 2
    elif verify:
        print("\ncontent check: all expected substrings present")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    ap.add_argument("--tokenizer", default=DEFAULT_TOKENIZER)
    ap.add_argument("--refresh", action="store_true", help="re-fetch every URL")
    ap.add_argument(
        "--verify",
        action="store_true",
        help="exit non-zero if expected content is missing from any page",
    )
    ap.add_argument("--json", type=Path, help="also write results as JSON")
    args = ap.parse_args()

    results, label = run(args.cache, args.tokenizer, refresh=args.refresh)
    code = report(results, label, verify=args.verify)

    if args.json:
        payload = {
            "tokenizer": label,
            "generated_by": "benchmarks/real_pages.py",
            "metric": "1 - (md_tokens / html_tokens)",
            "pages": [asdict(r) for r in results],
        }
        args.json.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.json}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

# Token cost of raw HTML vs extracted Markdown

A reproducible benchmark comparing the tokens an agent spends on a page's raw
HTML with the tokens in the Markdown DOMStrip returns for it.

**Headline: 84.08% fewer tokens than raw HTML** across **1,038 real pages from
74 sites** (macro average; median 90.12%). Reproducible from a clean clone.

```
tokenizer=tiktoken:o200k_base  pages_ok=1037  metric=1-(md/html)

per page type (mean of per-page reduction vs raw HTML)
  documentation    n=611  reduction=+83.87%
  blog             n=230  reduction=+84.61%
  article          n=165  reduction=+82.61%
  repo             n=23   reduction=+95.81%
  table            n=6    reduction=+82.94%
  edge             n=2    reduction=+75.66%

macro average (each page equally weighted)
  reduction=+84.08%  median=+90.12%
  pooled=+87.16% (dominated by the largest pages)
  p95_extraction_ms=160
```

Totals: **76,382,820 HTML tokens to 9,809,743 Markdown tokens.**

---

## Reproduce it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python real_pages.py --verify   # fetch anything missing, then check content
```

The script caches fetched HTML in `.corpus/` (git-ignored), so the first run
downloads 1,038 pages and later runs are offline and fast. `results-o200k.json`
records the exact per-page token counts behind the figures above.

Expect small differences from the published numbers if a source page has
changed since the manifest was generated. `--refresh` re-fetches everything.

---

## Corpus

**1,038 pages across 74 sites.** Built by mining public sitemaps and running
bounded same-site crawls, then selecting **round-robin with a per-site cap** so
no single host dominates, and **quota'd by kind**, because reachable sitemaps
are overwhelmingly documentation:

| kind | n | coverage |
| --- | ---: | --- |
| documentation | 611 | Python, Rust, Go, Django, FastAPI, Flask, NumPy, Pandas, MDN, Postgres, Redis, Docker, Kubernetes, SQLite, Node, React, Vue, Angular, Svelte, Next, Tailwind, TypeScript, Webpack, Vite, pip, mypy, Ruff, Celery, SQLAlchemy, Terraform and more |
| blog | 230 | Cloudflare, GitHub, Stack Overflow, jvns, danluu, Martin Fowler, Simon Willison, Joel Spolsky, Ars Technica, The Register, overreacted, Jim Nielsen |
| article | 165 | Wikipedia, IETF RFCs, W3C specs, WHATWG, LWN, Mozilla |
| repo | 23 | CPython, Linux, Rust, Go, Node, VS Code, Kubernetes, pandas, NumPy, Django, FastAPI, Flask, requests, httpx, Ruff, pytest, pre-commit, Black, Poetry, Redis |
| table | 6 | GDP, population, HTTP status codes, ISO 639, browsers, editors |
| edge | 2 | example.org, IANA |

Corpus composition matters more than corpus size. An earlier 21-page version
of this benchmark read **76.03%**; the same code on 1,038 pages reads
**84.08%**. Nothing changed in the extractor. The small corpus was
article-heavy, and articles save least.

## What is measured

For each page, two token counts under one tokenizer:

- **html** — the raw response body, markup and all
- **md** — what the extractor returns

`reduction = 1 - (md / html)`. Reported three ways, because each hides something:

| figure | means | catches |
| --- | --- | --- |
| **macro** | each page weighted equally | the typical page a caller fetches |
| **median** | middle value, outlier-resistant | a few huge pages skewing the mean |
| **pooled** | total tokens across all pages | total spend; dominated by the biggest pages |

## Why there is no "vs visible text" baseline

An earlier version also compared against `text_content()`. That comparison was
withdrawn because it is not well defined:

- **No stable denominator.** A page whose whole content is one large table has a
  nearly flat `text_content()` but a large Markdown table, producing readings
  like -458% that describe the metric rather than the tool.
- **It conflates two different effects.** A large gap meant *boilerplate kept*
  on a news index and *the whole document* on an RFC. The same number meant
  opposite things, and it led to two attempted "fixes" that silently deleted
  content while improving the score.

Content selection is now checked directly with `--verify`: 91 of the pages carry
required substrings that must appear in the output, and the run exits non-zero
if any is missing.

## Honest caveats

1. **Extraction-only figures.** No network, no fetch time, no bandwidth. They
   measure the conversion step, not end-to-end latency.
2. **Tokens are model-specific.** `o200k_base` is the default; `cl100k_base`
   gives 83.96% macro / 89.96% median on the same corpus. Never quote a number
   without its tokenizer.
3. **1037 of 1038 pages measured.** The one rejection is a Discord invite page
   returning `content_not_extractable`, which is the correct behaviour.
4. **Not every page reduces well.** The best pages save >95%; a few nearly
   markup-free pages save almost nothing. The macro average is not a floor.
5. **Source pages change.** The manifest is a point-in-time record; `--refresh`
   re-fetches.
6. **JS-rendered pages are out of scope**; the corpus is server-rendered HTML.

## How the extraction code is pinned

`domstrip_extract/` holds verbatim copies of the production cleaner and error
modules. `cleaner.py` exposes `HEURISTICS_VERSION`; the value in this copy is
the version these numbers were measured against. See
`domstrip_extract/README.md`.

## Licence

MIT, matching the rest of this repository.

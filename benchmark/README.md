# Token cost of raw HTML vs extracted Markdown

A reproducible benchmark comparing the tokens an agent spends on a page's raw
HTML with the tokens in the Markdown DOMStrip returns for it.

**Headline: 76.03% fewer tokens than raw HTML** (macro average across 107 real
pages; median 81.67%). Every figure below can be reproduced offline from the
snapshot in this directory.

```
tokenizer=tiktoken:o200k_base  pages_ok=107  metric=1-(md/html)

per page type (mean of per-page reduction vs raw HTML)
  article          n=27  reduction=+65.34%
  blog             n=10  reduction=+69.84%
  documentation    n=40  reduction=+77.91%
  edge             n=6   reduction=+56.71%
  repo             n=18  reduction=+95.45%
  table            n=6   reduction=+82.94%

macro average (each page equally weighted)
  reduction=+76.03%  median=+81.67%
  pooled=+82.50% (dominated by the largest pages)
  p95_extraction_ms=270
```

Totals: 11,683,125 HTML tokens to 2,044,378 Markdown tokens.

---

## Reproduce it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python real_pages.py --verify            # offline, from the pinned snapshot
python real_pages.py --refresh --verify  # re-fetch the 107 live URLs instead
```

`--verify` exits non-zero if any page's expected content is missing from its
Markdown. That check exists because a benchmark that only counts tokens will
reward output that has quietly deleted the page.

No network access is needed for the default run: `corpus.tar.gz` holds the exact
HTML the numbers were measured on.

---

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

Pooled is 82.50% and macro is 76.03%. Quoting only the pooled number would
overstate the typical case, so both are given.

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

Content selection is now checked directly with `--verify` instead.

## Corpus

107 pages, pinned in `corpus.tar.gz` (5.4 MB compressed, 34 MB raw):

| kind | n | coverage |
| --- | ---: | --- |
| documentation | 40 | Python, Rust, Go, Django, FastAPI, Flask, NumPy, Pandas, MDN, Postgres, Redis, Docker, Kubernetes, sqlite, man pages, PEPs |
| article | 27 | 20 Wikipedia topics, 4 IETF RFCs, W3C HTML spec |
| repo | 18 | CPython, Linux, Rust, Go, Node, VS Code, pandas, Kubernetes and others |
| blog | 10 | jvns.ca, danluu.com, martinfowler.com, joelonsoftware.com, HN, Lobsters |
| table | 6 | GDP, population, HTTP status codes, ISO 639, browsers |
| edge | 6 | example.org, IANA, httpbin, motherfuckingwebsite, text.npr |

Chosen to span the shapes people actually feed to agents. A corpus of only
article pages would flatter any extractor: that is the case where extraction
looks best, and repos (95.45%) and articles (65.34%) differ by 30 points here.

## Honest caveats

1. **These are extraction-only figures.** No network, no fetch time, no
   bandwidth. They measure the conversion step, not end-to-end latency.
2. **Tokens are model-specific.** `o200k_base` is the default. `cl100k_base`
   gives 75.90% macro / 81.61% median on the same corpus, so the result is not an
   artefact of one encoding, but do not quote a number without its tokenizer.
3. **`httpbin-html` reduces by only 6.86%** — it is almost pure prose with
   little markup, so there is little to remove. A benchmark where every page
   scored ~90% would be hiding something.
4. **The snapshot is a point in time.** Pages change; `--refresh` re-fetches.
   The pinned archive is what the published numbers refer to.
5. **Extraction can still fail on JS-rendered pages**, which are out of scope
   here: the corpus contains only server-rendered HTML.

## How the extraction code is pinned

`domstrip_extract/` holds verbatim copies of the production cleaner and error
modules. `cleaner.py` exposes `HEURISTICS_VERSION`; the value in this copy is
the version these numbers were measured against. See
`domstrip_extract/README.md`.

## Licence

MIT, matching the rest of this repository.

# Vendored extraction core

`cleaner.py` and `errors.py` are **verbatim copies** of the extraction code that
runs in the DOMStrip service. They are copied here so this benchmark can be run
from a clean clone with nothing installed, and so a published number always maps
to real production code rather than a reimplementation.

| file | source | purpose |
| --- | --- | --- |
| `cleaner.py` | DOMStrip service `src/domstrip/cleaner.py` | HTML -> Markdown conversion and the `auto`/`article`/`full` selection heuristics |
| `errors.py` | DOMStrip service `src/domstrip/errors.py` | `AppError` / `ErrorCode`, the only dependency `cleaner.py` has on the service |

## Provenance

- Copied from the private DOMStrip service repository, `main`, on **2026-11-03**.
- `cleaner.py` exposes `HEURISTICS_VERSION`; the value in this copy is the
  version the published numbers were measured against. If a future copy bumps
  it, older results are no longer comparable and should be re-run.

## Do not edit by hand

Changes made directly here would silently diverge from what the service does,
which is the one thing this directory exists to prevent. Update by re-copying
from the source tree, then re-running the benchmark.

## Dependencies

`cleaner.py` imports only `lxml` and `readability-lxml` from outside the
standard library, plus `errors.py` locally. Neither touches the network,
a database, or any service credential.

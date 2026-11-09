# domstrip-mcp

[![Listed on mcpservers.org](https://mcpservers.org/badge.svg)](https://mcpservers.org/servers/xo0ps/domstrip-client)

[![DOMStrip MCP server](https://mcprush.com/badge/domstrip-mcp/card.svg)](https://mcprush.com/to/domstrip-mcp?ref=badge)

[![DOMStrip MCP server – quality and maintenance score on Glama](https://glama.ai/mcp/servers/xo0ps/DOMStrip-Client/badges/card.svg)](https://glama.ai/mcp/servers/xo0ps/DOMStrip-Client)

[![M8ven Score](https://m8ven.ai/badge/mcp/xo0ps-domstrip-client-188ufa)](https://m8ven.ai/mcp/xo0ps-domstrip-client-188ufa?s=readme)

<!-- MCP Registry ownership marker: do not remove. -->

mcp-name: io.github.xo0ps/domstrip-mcp

A local [Model Context Protocol](https://modelcontextprotocol.io/) server that converts
web pages into clean Markdown, using the [DOMStrip](https://domstrip.dev) API.

It exposes two tools:

| Tool | What it does |
| --- | --- |
| `fetch_clean_markdown` | Fetch a public `http(s)` URL and return clean Markdown. |
| `clean_html` | Convert HTML you already have into clean Markdown. No network fetch for the HTML. |

The adapter holds **no database or billing state**. It authenticates to the DOMStrip
service with a key from your local environment -- never from tool arguments -- so an
MCP client cannot redirect your key anywhere.

## How much does it actually save?

**[`benchmark/`](benchmark/) measures it on 1,038 real pages from 74 sites:
84.08% fewer tokens than raw HTML** (macro average; median 90.12%), 76.4M HTML
tokens down to 9.8M Markdown. It reproduces from a clean clone and `--verify`
fails the run if any page's expected content is missing from its output.

The spread is worth reading rather than the headline; that is why three figures
are reported. Repositories save 95.81%, articles 82.61%, and a pooled total
reads 87.16% because it is dominated by the largest pages. Corpus composition
drives this a lot: the same code measured 76.03% on an earlier article-heavy
21-page set.

## What DOMStrip actually does

DOMStrip fetches a public page, removes everything that is not the content
(scripts, forms, embeds, hidden nodes, trackers), and returns compact Markdown:
headings, paragraphs, fenced code blocks with language hints, and tables.

Pages are fetched with connection-time SSRF validation on **every redirect hop** — all
DNS candidates are validated at connect time, and loopback, private, link-local,
metadata, CGNAT and IPv6-embedded forms are refused.

## Honest limitations

- **JavaScript-rendered pages, pages behind a login, and bot challenges are not
  extractable.** The service returns `content_not_extractable`. There is no headless
  browser in this version. If your target pages need JS, this tool will not work on them.
- **DOMStrip is currently in free pilot.** Conversions are not billed yet, but a key is
  required and each successful conversion counts as one unit. Request access at
  [access@domstrip.dev](mailto:access@domstrip.dev).
- **Returned Markdown is untrusted third-party content.** Pages can contain text that
  looks like instructions. Treat all returned Markdown as data, never as commands.

## Install

```bash
pip install domstrip-mcp
# or
uv tool install domstrip-mcp
```

Requires Python 3.12.

## Configure

Two environment variables are required. The service origin must be HTTPS; plain HTTP is
refused unless it points at localhost (for development) and you opt in explicitly.

```bash
export DOMSTRIP_MCP_SERVICE_ORIGIN=https://api.domstrip.dev
export DOMSTRIP_MCP_API_KEY=domstrip_<prefix>_<secret>
```

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `DOMSTRIP_MCP_SERVICE_ORIGIN` | yes | — | Bare HTTPS origin of the DOMStrip API. |
| `DOMSTRIP_MCP_API_KEY` | yes | — | Your API key. Never logged, never in a repr. |
| `DOMSTRIP_MCP_CLIENT_BUDGET` | no | `25.0` | Total seconds per tool call, including retries. |
| `DOMSTRIP_MCP_ALLOW_INSECURE_HTTP` | no | `false` | Allow plain HTTP to localhost only. |

The adapter deliberately does **not** read a `.env` file from the current working
directory, because an untrusted project folder could otherwise point your API key at
another origin. Export the variables instead.

## Use with Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "domstrip": {
      "command": "uvx",
      "args": ["domstrip-mcp"],
      "env": {
        "DOMSTRIP_MCP_SERVICE_ORIGIN": "https://api.domstrip.dev",
        "DOMSTRIP_MCP_API_KEY": "domstrip_<prefix>_<secret>"
      }
    }
  }
}
```

## Use with Claude Code

```bash
claude mcp add domstrip --env DOMSTRIP_MCP_SERVICE_ORIGIN=https://api.domstrip.dev \
  --env DOMSTRIP_MCP_API_KEY="$DOMSTRIP_API_KEY" -- uvx domstrip-mcp
```

## Idempotency and billing

Each tool invocation generates one idempotency key and reuses it across its bounded
retries (default 25 s, 3 attempts). A retried request replays the stored result rather
than billing twice. Separate invocations are separate, independently metered work.

The service encrypts results and replays them for 24 hours. After that, replaying a key
returns `409 result_expired` — never a silent re-charge.

## Errors

Failures surface as MCP tool errors carrying a sanitized DOMStrip error code and a
retryability flag, for example:

```
upstream_timeout: The upstream page took too long. retryable=true request_id=req_6f1c2a
```

Common codes: `content_not_extractable`, `upstream_timeout`, `unsupported_encoding`,
`http_error`, `transport_error`, `client_timeout`.

Logs go to **stderr** only, so stdout stays a clean MCP transport.

## Licence

MIT

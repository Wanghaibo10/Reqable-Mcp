<h1 align="center">reqable-mcp</h1>

<p align="center">
  <strong>Bring Reqable's captured HTTP traffic into your Claude Code conversation.</strong><br/>
  Read-only · zero-touch · no Reqable Pro required.
</p>

<p align="center">
  <a href="#"><img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+"></a>
  <a href="#"><img src="https://img.shields.io/badge/platform-macOS-lightgrey.svg" alt="macOS"></a>
  <a href="#"><img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT"></a>
  <a href="#"><img src="https://img.shields.io/badge/MCP-1.27%2B-orange.svg" alt="MCP 1.27+"></a>
  <a href="#"><img src="https://img.shields.io/badge/tests-111%20passed-brightgreen.svg" alt="111 tests"></a>
  <a href="#"><img src="https://img.shields.io/badge/status-alpha-yellow.svg" alt="alpha"></a>
</p>

---

## What it does

[Reqable](https://reqable.com) is a native macOS HTTP/HTTPS debugging proxy. Every request it captures lands in a local ObjectBox/LMDB database. **`reqable-mcp` is a read-only [Model Context Protocol](https://modelcontextprotocol.io) server that hands that traffic to Claude Code** so you can ask the AI things like:

> *"List the last 10 requests Chrome sent to `api.target.com`. Tell me which fields are dynamic per request — those are likely the encryption signature."*
>
> *"Wait for the next POST to `/login`, then decode the JWT in the response and show its claims."*
>
> *"Diff request `0e65fcea-...` and `f06fdfd2-...` to figure out what changed in the auth flow."*

Reqable keeps doing what Reqable does best (mitm, GUI, breakpoints, mocks). This project just adds an **AI-readable view** on top of its existing data — no replacement, no overlap.

> [!IMPORTANT]
> **No second proxy layer while in use.** Reqable is already your system proxy when capturing. Stacking another proxy (Clash, Surge, SwitchyOmega…) on top creates a request loop that will pollute capture data and may deadlock. The daemon enforces this in three places — see [Proxy loop guard](#proxy-loop-guard) below.

---

## Features

- 🪟 **Zero-touch on Reqable** — no config files modified, no scripts enabled, no Pro required (for the MVP feature set).
- 📚 **Reads existing captures** — every request Reqable has already recorded is queryable, including before this tool was installed.
- 🔌 **Live updates** — a 250 ms LMDB poller streams new captures into a local SQLite cache as Reqable writes them.
- 🧰 **13 MCP tools** — query, search, wait, diff, decode, transform; full list [below](#available-tools).
- 🛡️ **Proxy-loop hardened** — env scrub + `scutil --proxy` detection + zero HTTP-client imports anywhere in the codebase.
- ⚡ **Pure-Python, single process** — Claude Code spawns it, Claude Code disconnects, it goes away. No launchd, no daemon socket, no orphan state.
- 🔍 **Self-describing schema** — ObjectBox entity layout is parsed straight out of LMDB metadata; no `.fbs` files shipped, no version pinning.

---

## Quick start

```bash
git clone https://github.com/Wanghaibo10/Reqable-Mcp.git
cd Reqable-Mcp
./install.sh
# restart Claude Code, then in any chat:
#   "Use list_recent to show me the 5 newest captures."
```

Sanity-check from the shell at any time:

```bash
.venv/bin/reqable-mcp status
```

---

## Installation

### Requirements

- macOS (only platform Reqable supports today)
- Python ≥ 3.10  — `brew install python@3.13` if you need it
- [Reqable](https://reqable.com) installed and used at least once
- Claude Code (the agent that talks to MCP servers)

### One-line install

```bash
./install.sh
```

What it does, in order:

1. Picks the newest Python ≥ 3.10 on `$PATH`
2. Creates `.venv/` and installs the package in editable mode
3. Creates `~/.reqable-mcp/` with `0700` permissions (cache, logs)
4. Adds the server entry to `~/.claude/mcp.json`

What it does **not** do:

- Touch any file under `~/Library/Application Support/com.reqable.macosx/`
- Install a launchd plist or any background service
- Require root or `sudo`

### Manual install

<details>
<summary>If you'd rather wire things up yourself</summary>

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e .

mkdir -m 700 -p ~/.reqable-mcp
```

Then add this entry to `~/.claude/mcp.json` (create the file if missing):

```json
{
  "mcpServers": {
    "reqable": {
      "command": "/absolute/path/to/.venv/bin/reqable-mcp",
      "args": ["serve"]
    }
  }
}
```

Restart Claude Code. `/mcp` should list `reqable` as connected.
</details>

### Uninstall

```bash
./uninstall.sh
```

Removes only `~/.reqable-mcp/` and the `reqable` entry in `~/.claude/mcp.json`. Reqable's own data and configuration are untouched.

---

## Available tools

| Tool | Tier | What it does |
| --- | --- | --- |
| `list_recent` | query | Most recent captures, filterable by `host` / `method` / `status` / `app`. |
| `get_request` | query | One capture in full — headers + body (body fetched on demand). |
| `search_url` | query | URL substring or regex search. |
| `search_body` | query | Full-text search across request/response body content. |
| `to_curl` | query | Render a capture as a runnable `curl` command (auto-adds `--noproxy '*'`). |
| `list_apps_seen` | query | Recently captured apps (Chrome / Safari / native binaries). |
| `stats` | query | Host / method / status distribution over a window. |
| `diff_requests` | query | Field-level diff between two captures (metadata + headers). |
| `wait_for` | sync | Block until the next matching capture appears (or until timeout). |
| `find_dynamic_fields` | reverse | Detect fields that vary request-to-request — likely tokens / signatures. |
| `decode_jwt` | reverse | Decode a JWT, or extract one from a captured request's headers. |
| `extract_auth` | reverse | List Authorization / Cookie / `X-*-Token` values seen on a host. |
| `status` | meta | Daemon + LMDB schema + sync counters (also a CLI command). |

Each tool's docstring is what Claude Code shows when it lists tools — keep that in mind if you hack on them.

---

## Usage examples

Once installed, just talk to Claude Code. Every interaction below is what an actual conversation looks like — Claude calls the tool itself based on intent.

### Watch what your browser is doing

> **You:** What URLs has Chrome hit in the last 5 minutes? Group by host.
>
> **Claude** *(uses `list_apps_seen` then `list_recent` filtered by app)*: In the last 5 min Chrome made 168 requests across these hosts: `www.vipdlt.com` (36), `outlook.live.com` (23), `s.c-ctrip.com` (14)…

### Find the encryption parameter

> **You:** I'm reverse-engineering `target-api.com`. Which request fields look like they're freshly generated each call?
>
> **Claude** *(uses `find_dynamic_fields(host="target-api.com")`)*: Across 20 sampled requests these vary on every call — likely signatures / nonces:
>
> - `header:x-bm-sensor-data` (long base64)
> - `query:_t` (millisecond timestamps)
> - `body.requestId` (UUIDs)
>
> Stable across all 20: `header:host`, `header:user-agent`, `query:appid`.

### Wait for the user, then analyze

> **You:** I'm about to click "checkout" — wait for the order POST and tell me what auth headers it carries.
>
> **Claude** *(uses `wait_for(host="api.shop.com", method="POST", path_pattern="/orders", timeout_seconds=120)` then `extract_auth`)*: Got it — capture `b51c72ac-…`. The request carried `Authorization: Bearer eyJhbGciOiJIUz…`, plus four cookies: `session_id`, `csrf`, `device_fp`, `cart_token`. The Bearer is a JWT — decoded payload has `sub: 4892731`, `exp` in 14 minutes.

### Reproduce a request from the shell

> **You:** Give me a curl that reproduces the latest call to `/api/v2/quote`.
>
> **Claude** *(uses `search_url("/api/v2/quote", limit=1)` → `to_curl`)*:
>
> ```bash
> curl --noproxy '*' -X POST \
>   -H 'host: api.example.com' \
>   -H 'authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6Ikp…' \
>   -H 'content-type: application/json' \
>   --data-raw '{"hotelId":12345,"checkin":"2026-05-01"}' \
>   'https://api.example.com/api/v2/quote'
> ```

---

## How it works

```
[browser / app]
       │
       ▼
[Reqable mitm proxy]                         (Reqable.app — unchanged)
       │
       ├──▶ [box/data.mdb]              ◀── LMDB poller (250 ms, idle backoff)
       │       (ObjectBox: metadata)
       │
       └──▶ [capture/{conn}-{sess}-     ◀── Body source (read on demand)
              {req_raw|res-raw|res-extract}-body.reqable]

       ┌─────────────── reqable-mcp serve (single process) ───────────────┐
       │  proxy_guard      env scrub + scutil --proxy detection           │
       │  LmdbSource       FlatBuffers parse, base64+gzip+json decode     │
       │  BodySource       capture/ file lookup by (conn_ts, conn_id, sid)│
       │  Database         SQLite WAL + FTS5 — metadata index only        │
       │  WaitQueue        threading.Event broadcast for wait_for         │
       │  FastMCP          stdio JSON-RPC, 13 tools registered            │
       └──────────────────────────────┬──────────────────────────────────┘
                                      │ stdio
                                      ▼
                              [Claude Code]
```

**Key design choices** (see [`.spec/reqable-mcp/spec.md`](.spec/reqable-mcp/spec.md) for full rationale):

- **Truth lives in Reqable.** Our SQLite is a queryable cache; bodies are never copied — they stay in `capture/*.reqable`.
- **Schema is self-describing.** ObjectBox encodes its entity layout *into* the LMDB. We parse that, so Reqable adding fields doesn't break us.
- **Single process.** Claude Code launches `reqable-mcp serve`, the daemon lives only as long as that stdio session. Cold-start ≈ 250 ms.
- **No HTTP clients anywhere.** A test in CI greps the source tree to keep `requests`/`urllib3`/`aiohttp`/`httpx` out — they'd inherit env-driven proxy config and break the loop guard.

---

## Proxy loop guard

> [!CAUTION]
> Reqable as system proxy is fine. **Any second proxy on top is not.**

The daemon enforces this on three layers:

| Layer | Mechanism |
| --- | --- |
| **L1 — process env** | `scrub_env()` deletes every `*_PROXY` variable on startup and sets `NO_PROXY=*`. |
| **L2 — system proxy detection** | `scutil --proxy` is parsed at startup; non-loopback proxies emit a stderr warning. Set `REQABLE_MCP_STRICT_PROXY=1` to exit instead. |
| **L3 — code review** | The codebase contains zero imports of `requests` / `urllib3` / `aiohttp` / `httpx` — `tests/test_e2e.py::test_no_http_clients_imported` greps the source tree to enforce this on every test run. |

---

## Configuration

### Environment variables

| Variable | Default | Effect |
| --- | --- | --- |
| `REQABLE_MCP_STRICT_PROXY` | `0` | When `1`, exit with code 2 if a non-loopback system proxy is active at startup. |

### Paths

| Path | Purpose |
| --- | --- |
| `~/.reqable-mcp/cache.db` | SQLite metadata index (WAL mode). Safe to delete; rebuilt on next run. |
| `~/.reqable-mcp/daemon.log` | Daemon stderr (verbosity controlled by `--log-level`). |
| `~/.reqable-mcp/state.json` | Future use; currently unused. |
| `~/Library/Application Support/com.reqable.macosx/box/` | Reqable LMDB. Read-only. |
| `~/Library/Application Support/com.reqable.macosx/capture/` | Reqable body files. Read-only. |

### CLI commands

```text
reqable-mcp serve         # MCP stdio server (Claude Code spawns this)
reqable-mcp status        # Daemon snapshot as JSON, then exit
reqable-mcp install-help  # Print MCP registration JSON
reqable-mcp --version
reqable-mcp --help
```

---

## Compatibility

| | Tested |
| --- | --- |
| macOS | 14, 15 |
| Reqable | **3.0.40** (April 2026) |
| Python | 3.10 / 3.11 / 3.12 / 3.13 |
| MCP SDK | 1.27.0 |

ObjectBox schema is parsed at runtime, so additive changes (new fields) are tolerated automatically. Breaking changes (renamed fields, new `dbData` envelope) require updating the projection in `lmdb_source.py`. The decoder is fail-safe: any individual record that won't decode is logged and skipped, never crashes the daemon.

This project does **not** support mitmproxy / Charles / Proxyman — those use different storage formats and would each need their own source module.

---

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest                 # 111 tests; integration ones skip if no Reqable LMDB
.venv/bin/ruff check src/ tests/
.venv/bin/mypy src/
```

The repository layout:

```
.
├── .spec/reqable-mcp/         # Specification: read this before changing things
│   ├── spec.md                # Requirements + design decisions + data model
│   ├── tasks.md               # Module-by-module task plan
│   └── checklist.md           # Acceptance checklist
├── src/reqable_mcp/
│   ├── proxy_guard.py         # L1+L2 of the proxy guard
│   ├── paths.py               # Filesystem path resolution
│   ├── db.py + schema.sql     # SQLite cache layer
│   ├── wait_queue.py          # Broadcast wait queue (threading.Event)
│   ├── daemon.py              # Component composition
│   ├── mcp_server.py          # FastMCP stdio entry
│   ├── __main__.py            # `reqable-mcp` CLI dispatch
│   ├── sources/
│   │   ├── flatbuffers_reader.py   # No-schema FB parser (stdlib only)
│   │   ├── objectbox_meta.py       # ObjectBox entity introspection
│   │   ├── lmdb_source.py          # 250 ms poller + decoder
│   │   └── body_source.py          # capture/ file reader
│   └── tools/
│       ├── query.py           # 8 query tools
│       ├── wait.py            # wait_for
│       └── analysis.py        # JWT / dynamic-fields / auth extraction
├── tests/                     # 111 tests (unit + integration)
├── install.sh / uninstall.sh
└── pyproject.toml
```

Integration tests use a `real_lmdb_required` fixture that resolves to your local Reqable LMDB if one exists; otherwise they skip cleanly. Pure-logic tests have no such requirement.

---

## Limitations

- **macOS only.** Reqable is macOS-only at the time of writing.
- **Body sometimes missing.** Reqable doesn't keep every body forever — `capture/` files can be purged by Reqable. The tools surface `body_status: "unavailable"` in that case.
- **CONNECT tunnels.** TLS-handshake-only entries (no decrypted body) come through with `method=CONNECT` and limited metadata.
- **No write-back.** This is a read-only view. Tagging requests, mocking, modifying — that's [Phase 2](#roadmap).

---

## Roadmap

The MVP covers query, search, wait, and analysis. Two future phases extend into write-back territory:

- **Phase 2 — UI annotations + traffic modification.** `tag_pattern` / `comment_request` to highlight captures in Reqable's UI, plus `mock_response` / `block` / `replace_body`. Requires Reqable Pro (the script feature) and writing to `config/capture_config`'s `scriptConfig`. Entry condition: a week of MVP daily-use without daemon crashes, plus three concrete write-back use cases.
- **Phase 3 — Export + replay.** `dump_body`, `export_har`, `replay_request(uid, modifications)`.

---

## Acknowledgments

- [Reqable](https://reqable.com) — the actual hard work
- [Anthropic](https://www.anthropic.com) — Model Context Protocol and Claude Code
- [ObjectBox](https://objectbox.io) — for keeping their on-disk layout legible enough that we never had to ship an `.fbs` file

---

## License

[MIT](LICENSE)

---

<details>
<summary>简体中文摘要</summary>

`reqable-mcp` 把 Reqable 抓到的 HTTP/HTTPS 流量,通过 [Model Context Protocol](https://modelcontextprotocol.io) 只读地暴露给 Claude Code。

- ✅ 零侵入(不改 Reqable 任何文件、无需 Pro)
- ✅ 13 个工具(查询 / 搜索 / 等待 / 逆向辅助)
- ✅ 防代理回环(进程级 ENV scrub + 系统代理检测 + 代码层面禁 HTTP 客户端)
- ✅ 单进程(Claude Code 启动它,关闭它,仅此而已)

**安装:** `./install.sh`,然后重启 Claude Code。

**强约束:** Reqable 抓包时本身就是系统代理,所以**不要再叠第二层代理**(Clash / Surge 等),否则会形成回环。

详细中文版设计文档见 [`.spec/reqable-mcp/spec.md`](.spec/reqable-mcp/spec.md)(需求 + 架构 + 数据模型 + 决策追溯)。

</details>

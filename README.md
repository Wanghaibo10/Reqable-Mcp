# reqable-mcp

> 本地 MCP server,让 Claude Code 直接查询 / 分析 Reqable 抓到的实时 HTTP/HTTPS 流量 — 只读、零侵入、免 Pro。

`v0.1.0a1` · macOS only · Python ≥ 3.10

---

## 这是什么

Reqable 抓的每个请求都被它自己存到本地 ObjectBox/LMDB 数据库。`reqable-mcp` 只读地接到这套存储上,把抓包数据通过 [Model Context Protocol](https://modelcontextprotocol.io/) 暴露给 Claude Code。

适合的场景:
- 爬虫 / API 逆向工作时,让 Claude 直接看到 Reqable 抓到的请求,不用复制粘贴
- 让 Claude 自动跟踪某 host 的请求模式(`find_dynamic_fields` 找加密字段)
- 让 Claude 阻塞等待你手动操作触发的请求(`wait_for`)
- 让 Claude 把抓到的请求转 curl、解 JWT、提取 auth 字段

不替代 Reqable;Reqable 继续做 mitm 代理 / GUI / 改包。这只是给它加个"AI 视角的查询 API"。

---

## 强约束:防代理回环

⚠️ **MCP 工作期间不要在系统层再开一层代理**(Clash / Surge / SwitchyOmega 等)。

Reqable 自己作为系统代理(127.0.0.1:9001)是正常状态。但若再串第二层代理,daemon 进程任何外发流量会形成回环。`proxy_guard` 模块在三处强制 bypass:

1. **进程内**:`scrub_env()` 启动时移除所有 `*_PROXY` 环境变量,设 `NO_PROXY=*`
2. **系统级**:启动时调用 `scutil --proxy` 检测;若发现非环回代理,stderr 警告
3. **代码层**:整个项目禁止 `import requests / urllib3 / aiohttp / httpx`(只用 LMDB readonly + SQLite + 文件 I/O)

可设 `REQABLE_MCP_STRICT_PROXY=1` 让检测失败时直接退出。

---

## 安装

```bash
./install.sh
```

脚本会:
1. 检查 Python ≥ 3.10
2. 创建 `.venv/` 装 `reqable-mcp` + 依赖
3. 创建 `~/.reqable-mcp/`(0700 权限)
4. 在 `~/.claude/mcp.json` 注册 server

**完全不动 Reqable 任何数据或配置文件。**

装完后:
- 确保 Reqable 已开,正在抓包
- 重启 Claude Code → 在对话里 `/mcp` 应能看到 `reqable` 已连接

shell 也能 sanity check:

```bash
.venv/bin/reqable-mcp status
```

会输出 daemon 状态 + LMDB 连通性 + schema 版本 JSON。

### 卸载

```bash
./uninstall.sh
```

只清 `~/.reqable-mcp/` 和 `~/.claude/mcp.json` 里的 `reqable` 项。Reqable 数据 / 配置一行不动。

---

## 工具清单(MVP)

| Tier | 工具 | 用途 |
|---|---|---|
| 1 查询 | `list_recent` | 列最近 N 条请求(可按 host/method/status/app 筛) |
| 1 查询 | `get_request` | 取单条完整内容(含 body) |
| 1 查询 | `search_url` | URL 子串或正则搜 |
| 1 查询 | `search_body` | 在 req/res body 里全文搜 |
| 1 查询 | `to_curl` | 转 curl 命令(自动加 `--noproxy '*'`) |
| 1 查询 | `list_apps_seen` | 列出近期抓到的 app(Chrome / Safari …) |
| 1 查询 | `stats` | 一段窗口的 host/method/status 分布 |
| 1 查询 | `diff_requests` | 两条请求字段级 diff |
| 4 等待 | `wait_for` | 阻塞等下一个匹配的请求(可超时) |
| 5 分析 | `find_dynamic_fields` | 自动检测每次请求都变的字段(候选加密 token) |
| 5 分析 | `decode_jwt` | 解 JWT(token 字符串或从 uid 头里找) |
| 5 分析 | `extract_auth` | 列出 host 上所有 Authorization / Cookie / X-*-Token |
| 内置 | `status` | daemon 状态 / 计数器 / schema |

每个工具的 docstring 在 Claude Code 工具列表里直接可见。

---

## 架构

```
[App / 浏览器]
       ↓
[Reqable mitm proxy]
       ↓ 写
[Reqable ObjectBox/LMDB]                   [Reqable capture/{conn-id}*-body.reqable]
       ↑ readonly mmap                        ↑ open(uid)
       │                                       │
       │ 250ms 轮询 + 增量解码                 │ 按需读 raw body
       │                                       │
   [reqable-mcp daemon (in-process)]
   ┌──────────────────────────────────────────────────┐
   │  proxy_guard:    scrub_env / scutil --proxy      │
   │  LmdbSource:     增量同步,FlatBuffers 解析        │
   │  BodySource:     按 conn 三元组读 capture/ 文件   │
   │  Database:       SQLite WAL 索引 cache            │
   │  WaitQueue:      threading.Event 阻塞等待         │
   │  FastMCP server: stdio JSON-RPC                  │
   └──────────────────────────────────────────────────┘
       ↑ stdio
   [Claude Code]
```

**单进程**:Claude Code 启动 `reqable-mcp serve`,它内含 LMDB 后台 poller / SQLite cache / wait queue。Claude Code 退出 → 子进程结束 → 一切清理。无 launchd、无独立 daemon、无 IPC socket。

### 数据流约定

- **真值**:Reqable 自己的 `box/data.mdb`(metadata) + `capture/*-body.reqable`(body)
- **索引**:`~/.reqable-mcp/cache.db`(只缓存元数据;body 不入 SQLite,按需从 LMDB / capture/ 取)
- **同步状态**:cache.db 的 `sync_state.last_ob_id` 游标;增量从那继续

---

## 仓库结构

```
reqable-mcp/
├─ .spec/reqable-mcp/         # 设计规格(看这里了解为什么这么做)
│  ├─ spec.md                  # 需求 + 设计决策 + 数据模型
│  ├─ tasks.md                 # 模块化任务拆解
│  └─ checklist.md             # 验收清单
├─ src/reqable_mcp/
│  ├─ proxy_guard.py            # 防代理回环
│  ├─ paths.py                  # 路径常量
│  ├─ db.py + schema.sql        # SQLite cache
│  ├─ wait_queue.py             # wait_for 实现
│  ├─ daemon.py                 # 组件聚合
│  ├─ mcp_server.py             # FastMCP stdio 入口
│  ├─ __main__.py               # CLI: serve / status / install-help
│  ├─ sources/
│  │  ├─ flatbuffers_reader.py  # 无 schema FB 解析
│  │  ├─ objectbox_meta.py      # ObjectBox entity schema 自描述提取
│  │  ├─ lmdb_source.py         # LMDB 轮询 + 增量入库
│  │  └─ body_source.py         # capture/ 目录 body 文件读取
│  └─ tools/
│     ├─ query.py               # Tier 1
│     ├─ wait.py                # Tier 4
│     └─ analysis.py            # Tier 5
├─ tests/                       # 100+ 测试,部分需真实 Reqable LMDB
├─ install.sh / uninstall.sh
└─ pyproject.toml
```

---

## 兼容性

针对 **Reqable 3.0.40**(2026-04)实测打通。

ObjectBox/LMDB 内部 schema 是 *自描述* 的(每个 entity 的字段定义存在 LMDB 元数据 key 里),所以 Reqable 加字段时我们自动忽略;只有删字段或换序列化(目前是 `dbData = base64(gzip(JSON))`)才会破坏。如果哪天 Reqable 大版本升级把 dbData 编码改了,需要重新探查 — `objectbox_meta.py` 自带 fail-safe,无法解码的 record 跳过 + log,不会让 daemon 崩溃。

不支持 Reqable 之外的抓包工具(mitmproxy / Charles / Proxyman)— 它们各自有不同存储格式,需要单独实现。

---

## Phase 2 / 3 路线(暂未实现)

只读 LMDB 已覆盖**绝大部分**查询 / 分析需求。两个未来方向需要"反向写回 Reqable":

- **Phase 2**:Tier 2 标记(`tag_pattern` / `comment_request`,在 Reqable UI 里高亮)+ Tier 3 改包(`mock_response` / `block` / `replace_body`)。需要启用 Reqable Pro + 写 `capture_config.scriptConfig` 让 Reqable 加载我们的 `addons.py` Python 脚本。
- **Phase 3**:Tier 6 导出(`dump_body` / `export_har`)+ replay。

进入 Phase 2 的前置条件:MVP 真实使用 ≥ 1 周稳定;收到 ≥ 3 个具体的反向写回需求场景。

---

## 测试

```bash
.venv/bin/pytest
```

测试分两类:
- **单元测试**:`proxy_guard / db / flatbuffers_reader / wait_queue / body_source` 等 — 不需要 Reqable
- **集成测试**:`objectbox_meta / lmdb_source / daemon / tools_*` 等 — 需要本机 Reqable 跑过且有捕获,否则 `pytest.skip`(通过 `conftest.py` 的 `real_lmdb_required` fixture 控制)

---

## License

MIT。

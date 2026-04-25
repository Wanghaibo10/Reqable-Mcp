# reqable-mcp 设计规格(v2 — 方案 C 混合架构)

## 概述

为 Reqable(macOS 抓包工具)提供本地 MCP Server,让 Claude Code 直接查询/分析 Reqable 抓到的 HTTP/HTTPS/WebSocket 流量。

**主数据源:** Reqable 的本地 LMDB 数据库(只读、零侵入)。
**可选增强:** Reqable Python 脚本扩展点(addons.py),用于反向标记 / 改包等需要写回的能力 — 仅 Phase 2。

---

## 背景与动机

### 当前痛点

- Claude Code / 用户做爬虫逆向 / API 调试时,需要在 Reqable 中手动找请求、复制 curl、贴给 AI — 来回切换。
- Rockxy 内置 MCP server 但 0.x、独立开发者、不支持 HTTP/2/3 / 移动设备抓包 — 不能用作主力。
- Reqable 闭源,不能像 Rockxy 那样改源码加 MCP。

### 关键技术发现

通过逆向探查(2026-04-25)确认:

1. **Reqable 用 ObjectBox(Flutter ORM,基于 LMDB + FlatBuffers)** 存抓包记录
2. **LMDB 文件可被 Python `lmdb` 库 readonly 打开,与 Reqable 写入并发安全**(LMDB 多读单写,只读不抢锁)
3. **ObjectBox entity schema 自包含在 LMDB 元数据 key(`\x00\x00\x00\x00\x00\x00\x00\x0b` 等)中**,**不需要外部 fbs 文件**
4. `CaptureRecordHistoryEntity` 字段:`id (long) / uid (string,UUID) / timestamp (long ms) / dbData (bytes) / dbUniqueId (string)`
5. **`dbData` = base64(gzip(JSON))** — JSON 顶层字段:`['id','uid','origin','session','reqStatus','resStatus','error','interceptions','appInfo','reqLogs','resLogs','comment','sslBypassed','sslEnabled']`
6. **LMDB 的 `uid` 字段(UUID)正好是 Reqable `rest/{uuid}-req.bin` / `-res.bin` 文件名的前缀** — 元数据(LMDB)与原始 raw(rest 目录)通过 UUID 一一对应

→ 可以**完全只读 + 零侵入**实现绝大部分 MCP 工具,仅在需要"反向写回 Reqable UI(highlight/comment)"或"改包"时才挂 Hook。

---

## 设计决策

| 决策点 | 选择 | 理由 | 备选 |
|---|---|---|---|
| **主数据源** | Reqable LMDB(只读) | 零侵入、零配置、免 Pro;历史数据立刻可用;Reqable 升级风险低 | 全 hook(需 Pro+用户启用脚本) |
| **可选数据源** | Reqable Python `addons.py` Hook | 仅 Phase 2 启用,做反向写回 + 改包 | 放弃这些能力 / frida 注入 |
| **schema 解析** | 从 LMDB 元数据 key 解 ObjectBox internal model,无外部依赖 | 自包含、不依赖 fbs 文件、Reqable 升级时 schema 也跟着变 | 从 ObjectBox 项目拉 .fbs / 逆向 Dart AOT |
| **进程模型** | 单进程:Claude Code 启动 `reqable-mcp serve`,内含 LMDB poller / DB writer / wait queue / MCP stdio | 简单、launchd 不要、wait_for 同进程实现 | 独立 daemon + RPC |
| **本地 SQLite cache** | 用,作为查询索引层 + 持久化 wait_for / 规则历史 | 加快 search/list 查询,跨 Claude Code session 复用 | 直接每次查 LMDB(慢) |
| **数据真值** | LMDB(metadata + dbData JSON)+ rest/{uuid}.bin(原始 raw) | 单一真源,SQLite 仅为索引 | SQLite 也存 raw(冗余) |
| **LMDB 增量同步** | 轮询 + 游标(last seen `id`) | 简单稳定;LMDB 没有原生 watch API | inotify / fsevents 监听文件 mtime |
| **轮询间隔** | 默认 250ms,空闲时退避到 2s | 平衡实时性与 CPU | 固定 100ms / 1s |
| **Hook(Phase 2)与 LMDB 关系** | Hook 仅做"规则引擎应用"(标颜色 / 改包),**不再重复采集** | 避免双数据源 ID 映射难题 | hook 也采集 → 需要 (ctime-cid-sid) ↔ UUID 映射 |
| **代理回环防护** | 进程级 ENV scrub + 启动检测系统代理 | 用户强约束 | 信任用户手动关 |
| **Reqable 启动绑定**(Phase 2) | install.sh 修改 `~/Library/Application Support/com.reqable.macosx/config/capture_config` 的 `scriptConfig.scripts/isEnabled` | 自动启用脚本,免去用户手动启用 | 完全手动 |

---

## 范围界定

### MVP(在范围内)— 只读 LMDB 主体

- [ ] **LmdbSource** — 后台线程轮询 Reqable LMDB,增量同步到本地 SQLite cache
- [ ] **FlatBuffers decoder** — 解 ObjectBox entity meta + 用户数据,无外部 schema 依赖
- [ ] **RestSource** — 按 UUID 直接读 `rest/{uuid}-{req,res}.bin`(JSON 格式),拿完整原始请求/响应
- [ ] **本地 SQLite cache** — 索引层(uid 主键、timestamp / host / app_name 索引)
- [ ] **MCP stdio server** — 注册工具,Claude Code 调用
- [ ] **代理回环防护** — `proxy_guard.py`
- [ ] **Tier 1 查询(8 个)**:`list_recent / get_request / search_url / search_body / to_curl / list_apps_seen / stats / diff_requests`
- [ ] **Tier 4 阻塞等待**:`wait_for(host?, path_pattern?, method?, app?, status?, timeout_seconds)`
- [ ] **Tier 5 逆向辅助(3 个)**:`find_dynamic_fields / decode_jwt / extract_auth`
- [ ] **install.sh** — 仅创建数据目录 + 注册 MCP 到 `~/.claude/mcp.json`,**不动 Reqable 任何配置**
- [ ] 单元测试 + 端到端测试

### Phase 2 — Hook 能力增强(在范围内,但 MVP 后)

- [ ] **HookSource** — `addons.py` 仅做规则应用(不重复采集),IPC 推送规则命中记录到 daemon
- [ ] **install.sh 增强** — 自动写 `capture_config.scriptConfig`(要求 Reqable 已退出)
- [ ] **Tier 2 反向标记** — `tag_pattern / untag_pattern / comment_request / list_tags`
- [ ] **Tier 3 改包规则** — `mock_response / block / replace_body / inject_header / replace_field`

### Phase 3 — 增值工具(可选)

- [ ] **Tier 6 导出**:`dump_body / export_har / decode_body / prettify`
- [ ] `replay_request(uid, modifications)` — 重发请求

### 不在范围内

- 不替代 Reqable / 不实现自己的 mitm
- 不做 GUI / Web UI
- 不支持 Reqable 之外的抓包工具
- 不做云存储 / 多机同步
- 不破解 / 反编译 Reqable 二进制(只读 LMDB 的 ObjectBox schema 是自包含的,不算逆向 Reqable 内部代码)
- 不实现 HTTP/3 / gRPC(等 Reqable 自己开放后用 LMDB 自然就能拿到)
- 不向 Reqable LMDB 写入(只读,绝对不写,避免破坏 Reqable 数据完整性)

---

## 架构设计

### 总体数据流

```
[被测 App / 浏览器]
       ↓
[Reqable mitm](Reqable 自己跑)
       ↓ 写
[Reqable ObjectBox / LMDB]                    [Reqable rest/{uuid}.bin]
       ↑ readonly mmap                          ↑ open(uuid)
       │                                        │
       │ 轮询 + 增量解码                        │ 按需读 raw body
       │                                        │
   [reqable-mcp daemon]
   ┌──────────────────────────────────────────────────┐
   │  proxy_guard         scrub_env                    │
   │  LmdbSource           250ms 轮询,FlatBuffers 解码│
   │  RestSource           按 uid 读 rest/.bin         │
   │  Database (SQLite)    本地索引 cache              │
   │  WaitQueue            阻塞等待  threading.Event   │
   │  RuleEngine (P2)      tag/modify/mock 应用        │
   │  IPCServer (P2)       Unix socket,听 addons      │
   │  MCP stdio server     注册工具,响应 Claude Code  │
   └──────────────────────────────────────────────────┘
       ↑ stdio
   [Claude Code]
```

### 进程边界

| 进程 | 启动方式 | 生命周期 | 通信 |
|---|---|---|---|
| Reqable.app | 用户开 GUI | 用户控 | (无关,我们只读它的文件) |
| `reqable-mcp serve` | Claude Code 启动子进程(stdio MCP) | Claude Code session 期间 | stdin/stdout JSON-RPC |
| `python3 main.py request/response`(Phase 2) | Reqable fork(每请求一次) | 单次,~200ms | Unix socket → daemon |

**MVP 没有独立 daemon**:LMDB poller / DB writer / wait queue 都是 `reqable-mcp serve` 进程内的线程。Claude Code 退出 → MCP server 退出 → 一切退出。

### 文件结构

```
<project-root>/                      # 项目根
├─ .spec/reqable-mcp/                              # 本目录
│  ├─ spec.md  tasks.md  checklist.md
├─ README.md
├─ pyproject.toml                                  # 包元数据;script entry: reqable-mcp
├─ install.sh                                      # 一键安装(MVP:仅注册 MCP)
├─ uninstall.sh
├─ src/reqable_mcp/
│  ├─ __init__.py
│  ├─ __main__.py                                  # 入口:reqable-mcp {serve, status, install-help}
│  ├─ proxy_guard.py                               # 代理 ENV 剥离 + 检测
│  ├─ paths.py                                     # 解析 Reqable 数据目录、本地数据目录
│  ├─ db.py                                        # SQLite 封装(WAL、索引、CRUD)
│  ├─ schema.sql                                   # SQLite DDL
│  ├─ wait_queue.py                                # wait_for 实现
│  ├─ sources/
│  │   ├─ __init__.py
│  │   ├─ flatbuffers_reader.py                    # 通用 FB 解析(table/vtable/string/vector)
│  │   ├─ objectbox_meta.py                        # 解 ObjectBox entity meta → schema dict
│  │   ├─ lmdb_source.py                           # LmdbSource:轮询、解码、增量入 SQLite
│  │   └─ rest_source.py                           # RestSource:按 uid 读 rest/{uuid}.bin
│  ├─ daemon.py                                    # 启动 + 编排各组件
│  ├─ mcp_server.py                                # MCP stdio server,工具注册总入口
│  └─ tools/
│     ├─ __init__.py
│     ├─ query.py                                  # Tier 1
│     ├─ wait.py                                   # Tier 4
│     ├─ analysis.py                               # Tier 5
│     ├─ tag.py                                    # P2 占位
│     ├─ modify.py                                 # P2 占位
│     └─ export.py                                 # P3 占位
├─ addon/                                          # P2 才用,MVP 阶段空目录或不创建
│  ├─ main.py
│  └─ addons.py
└─ tests/
   ├─ test_proxy_guard.py
   ├─ test_flatbuffers_reader.py
   ├─ test_objectbox_meta.py
   ├─ test_lmdb_source.py        # 用 fixture LMDB
   ├─ test_rest_source.py        # 用 fixture rest dir
   ├─ test_db.py
   ├─ test_wait_queue.py
   ├─ test_tools_query.py
   ├─ test_tools_wait.py
   ├─ test_tools_analysis.py
   └─ test_e2e.py
```

### 安装路径(运行时落地)

**MVP — 极简,不动 Reqable**

```
~/.reqable-mcp/                                    # 数据目录(权限 0700)
   ├─ cache.db                                     # SQLite 本地索引 cache
   ├─ cache.db-wal
   ├─ cache.db-shm
   ├─ daemon.log
   └─ state.json                                   # 上次 LMDB 同步到的 internal_id 游标

~/.claude/mcp.json (或 settings.json)              # MCP 注册
   "mcpServers": { "reqable": { "command": "reqable-mcp", "args": ["serve"] } }
```

**Phase 2 增加(用户启用 Hook 后)**

```
~/Library/Application Support/com.reqable.macosx/scripts/exec/<NEW_UUID>/
   ├─ main.py     ← 我们的 Hook 入口
   ├─ addons.py   ← 规则应用(tag/modify/mock)
   └─ reqable.py  ← Reqable 自带 SDK 副本

~/Library/Application Support/com.reqable.macosx/config/capture_config
   .scriptConfig.scripts += [<our_uuid>]
   .scriptConfig.isEnabled = true
   (备份原文件到 capture_config.bak.reqable-mcp)

~/.reqable-mcp/daemon.sock                         # IPC socket(Phase 2)
~/.reqable-mcp/rules.json                          # 规则下发(Phase 2)
```

---

## 数据模型

### Reqable LMDB(只读真源,我们不动)

| 实体 | 字段 | 用途 |
|---|---|---|
| `CaptureRecordHistoryEntity` | id, uid, timestamp, dbData, dbUniqueId | 每条抓到的请求/响应 |
| `CaptureSessionHistoryEntity` | (探索中) | 抓包会话 |
| 其他 entity | — | 与本工具无关 |

`dbData` 解码:`json.loads(gzip.decompress(base64.b64decode(dbData)))` → JSON,顶层 keys:
```
id uid origin session reqStatus resStatus error interceptions
appInfo reqLogs resLogs comment sslBypassed sslEnabled
```
其中 `session.request` / `session.response` 含完整 method / url / headers / body(类似 SDK 的 HttpRequest/HttpResponse 格式)。

### 本地 SQLite cache

```sql
CREATE TABLE IF NOT EXISTS captures (
  uid              TEXT PRIMARY KEY,         -- LMDB 的 UUID
  ob_id            INTEGER UNIQUE,           -- ObjectBox internal id(用于增量游标)
  ts               INTEGER NOT NULL,         -- timestamp (unix ms)
  scheme           TEXT,
  host             TEXT,
  port             INTEGER,
  url              TEXT,
  path             TEXT,
  method           TEXT,
  status           INTEGER,
  protocol         TEXT,
  req_mime         TEXT,
  res_mime         TEXT,
  app_name         TEXT,
  app_id           TEXT,
  app_path         TEXT,
  req_body_size    INTEGER,
  res_body_size    INTEGER,
  rtt_ms           INTEGER,
  comment          TEXT,                     -- LMDB 自带的 comment
  ssl_bypassed     INTEGER,
  has_error        INTEGER,
  source           TEXT NOT NULL DEFAULT 'lmdb',  -- lmdb | hook(P2)
  raw_summary      TEXT                      -- 简短 summary(req URL + status,辅助 search 显示)
);
CREATE INDEX idx_captures_ts        ON captures(ts DESC);
CREATE INDEX idx_captures_host_ts   ON captures(host, ts DESC);
CREATE INDEX idx_captures_app_ts    ON captures(app_name, ts DESC);
CREATE INDEX idx_captures_method_status ON captures(method, status);

-- 全文搜索:URL + summary(轻量,不索引 body)
CREATE VIRTUAL TABLE IF NOT EXISTS captures_fts USING fts5(
  uid UNINDEXED,
  url, summary,
  content='captures', content_rowid='rowid'
);
-- triggers to keep fts5 in sync 见 schema.sql

-- 同步状态
CREATE TABLE IF NOT EXISTS sync_state (
  source         TEXT PRIMARY KEY,           -- 'lmdb'
  last_ob_id     INTEGER NOT NULL DEFAULT 0,
  last_ts        INTEGER NOT NULL DEFAULT 0,
  last_run_ts    INTEGER NOT NULL DEFAULT 0
);

-- Phase 2:规则与命中
CREATE TABLE IF NOT EXISTS rules (
  rule_id      TEXT PRIMARY KEY,
  type         TEXT NOT NULL,                -- tag | modify | mock | block
  enabled      INTEGER NOT NULL DEFAULT 1,
  spec_json    TEXT NOT NULL,
  created_ts   INTEGER,
  expires_ts   INTEGER,
  hits         INTEGER NOT NULL DEFAULT 0
);
```

**重要:** body 字段不入 SQLite。需要 body 时按 uid 现取:
1. 先看 LMDB dbData JSON 里有没有(短 body 通常内联)
2. 否则 `~/Library/Application Support/com.reqable.macosx/rest/{uid}-{req,res}.bin`

`get_request(uid, include_body=True)` 走这个查询路径。

### SQLite PRAGMA(同上一版)

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA temp_store = MEMORY;
PRAGMA cache_size = -8000;
PRAGMA busy_timeout = 5000;
```

---

## 核心组件

### `flatbuffers_reader.py` — 通用 FB 解析

无 schema 通用解析器,提供:

```python
def deref(buf: bytes, off: int) -> int:                # uoffset → 绝对偏移
def parse_table(buf, table_off) -> dict:               # 返回 vt/vt_size/tbl_size/fields(fid → abs_off)
def read_string_at(buf, off) -> str:
def read_bytes_at(buf, off) -> bytes:
def read_vector_of_offsets(buf, off) -> list[int]:     # 返回 [sub-table abs_off]
def read_uint(buf, off, byte_size) -> int:             # 1/2/4/8
```

不依赖 `flatbuffers` Python 包(那个包需要 generated 代码)。

### `objectbox_meta.py` — Schema 提取

```python
@dataclass
class Property:
    pid: int          # ObjectBox internal property id
    vt_index: int     # 在用户数据 table vtable 中的索引
    name: str
    type_code: int    # 1=Bool 5=Int 6=Long 9=String 10=Date 23=ByteVector ...

@dataclass
class Entity:
    eid: int          # ObjectBox internal entity id
    name: str         # e.g. "CaptureRecordHistoryEntity"
    properties: list[Property]
    last_property_id: int

def load_schema(env: lmdb.Environment) -> dict[str, Entity]:
    """扫 LMDB 主 DB 元数据 keys (\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x0b 起),返回 entity name → Entity。"""
```

### `lmdb_source.py` — 主数据源

```python
class LmdbSource:
    def __init__(self, lmdb_path: str, db: Database, schema: dict[str, Entity]):
        ...

    def start(self, poll_interval_ms: int = 250):
        """启动后台线程,持续轮询 LMDB 增量入 SQLite。"""

    def _scan_once(self) -> int:
        """单次扫描,返回新入库行数。
        - 用 last_ob_id 游标,从 SQLite sync_state 读
        - 打开 LMDB readonly env,设 cursor.set_range(prefix_for_data + last_ob_id+1)
        - 遇到 metadata key 跳过
        - 解码每条:Entity FB → 取 dbData → base64+gunzip+json.loads
        - 提取:uid, timestamp, host/method/status/url/headers/app(JSON 内 session/appInfo)
        - INSERT OR IGNORE captures(...)
        - 更新 sync_state.last_ob_id
        - 唤醒匹配的 wait_queue waiters
        """
```

**轮询策略:**
- 默认 250ms 一次
- 连续 3 次空转(无新数据)→ 退避到 1000ms
- 连续 10 次空转 → 退避到 2000ms
- 一旦命中 → 立即重置到 250ms

**ObjectBox 数据 key 格式:**

观察到的 key 是 8 字节,前 4 字节是 `1800002c`(实际是 entity id 等内部编码),后 4 字节是 internal id。需要研究 ObjectBox key 编码规范来正确做 set_range,先以"扫全表 + 过滤"开局,优化项推后。

### `rest_source.py` — 原始 raw

```python
class RestSource:
    def __init__(self, rest_dir: str): ...

    def get_request_raw(self, uid: str) -> dict | None:
        path = f'{self.rest_dir}/{uid}-req.bin'
        if not os.path.exists(path): return None
        with open(path) as f:
            return json.load(f)   # Reqable 已经存的就是 JSON

    def get_response_raw(self, uid: str) -> dict | None: ...
```

### `wait_queue.py` — 阻塞等待

```python
class Waiter:
    id: str
    filter_spec: dict   # {host?, path_pattern?, method?, status?, app?}
    event: threading.Event
    matched_uid: str | None

class WaitQueue:
    def add(self, filter_spec: dict, timeout_s: float) -> str: ...
    def notify(self, capture: dict):
        """LmdbSource 解码到一条新记录后调用。遍历 active waiters,匹配则 set event。"""
    def wait(self, waiter_id: str, timeout_s: float) -> dict | None: ...
```

---

## MCP 工具签名(MVP)

### Tier 1 — 查询

```python
list_recent(limit: int = 20,
            host: str | None = None, method: str | None = None,
            status: int | None = None, app: str | None = None) -> list[dict]
get_request(uid: str, include_body: bool = True,
            include_response_body: bool = True) -> dict
search_url(pattern: str, regex: bool = False, limit: int = 20) -> list[dict]
search_body(query: str, target: Literal["req","res","both"] = "both",
            limit: int = 20) -> list[dict]   # 走 LMDB JSON / rest/.bin
to_curl(uid: str, multiline: bool = True) -> str
list_apps_seen(window_minutes: int = 60) -> list[dict]
stats(window_minutes: int = 5) -> dict
diff_requests(uid_a: str, uid_b: str) -> dict
```

### Tier 4 — 阻塞等待

```python
wait_for(host: str | None = None,
         path_pattern: str | None = None,
         method: str | None = None,
         app: str | None = None,
         status: int | None = None,
         timeout_seconds: int = 30) -> dict | None
```

### Tier 5 — 逆向辅助

```python
find_dynamic_fields(host: str, sample_size: int = 20,
                    field_locations: list[str] = ["headers","body","queries"]) -> dict
decode_jwt(token_or_uid: str) -> dict
extract_auth(host: str, window_minutes: int = 60) -> list[dict]
```

---

## 防系统代理回环(强约束)

(同 v1,无变化,仍需在 LMDB 主体方案下保留)

### 用户原话

"在 mcp 使用的时候,禁止开系统的代理"

### 三层防护

1. **L1 — 进程内剥离**:`scrub_env()` 在 daemon / mcp_server / addons 启动第一行调用,移除 `HTTP_PROXY / HTTPS_PROXY / ALL_PROXY` 及小写;设 `NO_PROXY=*`
2. **L2 — 系统级检测**:`detect_system_proxy()` 调 `scutil --proxy`;检测到指向 *非 Reqable 自己* 的代理(Clash / Surge 等)→ stderr 警告;`REQABLE_MCP_STRICT_PROXY=1` 时退出
3. **L3 — 进程不发外部 HTTP**:daemon / mcp_server 全部本地操作(LMDB readonly + SQLite + Unix socket);**任何引入 requests/urllib3/aiohttp 的 PR 必须拒绝**

### 文档说明

> ⚠️ Reqable 抓包时,系统代理指向 Reqable 自己是正常状态。本工具 daemon / MCP server 进程已强制 bypass 所有代理设置,不会形成回环。

---

## 性能预算

| 阶段 | 预算 |
|---|---|
| LMDB 单次轮询(无新数据) | < 5ms |
| LMDB 单次轮询(N=10 新记录,含 base64+gunzip+json) | < 50ms |
| SQLite 单条 INSERT | < 1ms |
| MCP 工具响应(简单查询) | < 50ms (P95) |
| `wait_for` 命中延迟 | < poll_interval(250ms 默认) |
| daemon 内存占用 | < 100MB(无大 body 在内存) |
| SQLite cache 大小(1万条记录) | < 50MB |

---

## 兼容性 / 升级风险

| 风险 | 评估 | 应对 |
|---|---|---|
| Reqable 改 ObjectBox schema(加字段) | **低风险** — FB 向后兼容,新字段 vtable 末尾追加,我们不读就忽略 | 自动忽略未知 vt index |
| Reqable 改 ObjectBox schema(改字段类型) | 中风险 — 比如 timestamp 从 long 变 string | schema 解析时检测 type_code 不匹配 → log + skip |
| Reqable 改 dbData 编码(不再 base64+gzip) | 中风险 — 需要重新探查 | dbData 解码 try/except,失败时降级到只取 metadata |
| Reqable 改 ObjectBox key 编码 | 低风险 — internal,不轻易改 | 全表扫 + 过滤,不依赖 key 结构 |
| Reqable 改 rest/{uuid}.bin 路径或格式 | 中风险 — RestSource 失败 → get_request 拿不到 body | 降级:先尝试 LMDB dbData JSON 内的 body 字段 |
| Python lmdb 库 ABI 不兼容 | 低风险 — 库稳定多年 | 锁定主版本 |

**核心容错原则:** schema/格式异常时,**降级到"只有 metadata,没有 body"** 而非崩溃。`get_request` 返回里加字段 `body_status: 'ok' | 'unavailable'`。

---

## 安全考虑

- `~/.reqable-mcp/` 权限 0700
- LMDB / rest 目录:**只读**打开,绝不写
- 抓包数据敏感(token / cookie / 密码) — daemon 不外发、不上传、不日志 body 明文
- MCP 工具返回敏感字符串是用户主动调用,落入 Claude Code 上下文是预期行为
- **绝对不向 Reqable 数据目录写任何东西**(MVP)。Phase 2 改 capture_config 时:写前备份、要求 Reqable 已退出、可一键还原

---

## 用户体验(MVP)

```
1. pip install reqable-mcp     (或 install.sh)
2. ~/.claude/mcp.json 自动加 "reqable" server
3. 重启 Claude Code → 工具列表里有 reqable.list_recent / wait_for / ...
4. (Reqable 该开开,该抓抓,我们旁路读)
```

无需:Reqable Pro / 启用脚本 / launchd / 手动配置 — 装上就用。

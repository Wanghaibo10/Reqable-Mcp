# reqable-mcp 任务拆解(v2 — 方案 C)

按依赖顺序排,每项尽量原子。✅ 在每项实现完成后勾选。

---

## 模块 0:项目骨架(无依赖)

- [ ] 0.1 创建项目根 + 包结构
  - 路径:`<project-root>/`
  - 子目录:`src/reqable_mcp/{sources,tools}`、`tests/`、`addon/`(P2 才用)
- [ ] 0.2 `pyproject.toml`
  - 包名:`reqable-mcp`
  - 依赖:`lmdb>=1.4`、`mcp>=1.0`(MCP SDK)
  - 入口:`reqable-mcp = reqable_mcp.__main__:main`
  - Python ≥ 3.10
- [ ] 0.3 `.gitignore`(`.venv/`, `__pycache__/`, `*.db*`, `*.sock`, `dist/`, `*.egg-info/`)
- [ ] 0.4 `README.md` 占位

## 模块 1:基础设施 — proxy_guard / paths / db

依赖:模块 0

- [ ] 1.1 `src/reqable_mcp/proxy_guard.py`
  - `scrub_env()` / `detect_system_proxy()` / `assert_proxy_safe(strict=False)`
  - `detect_system_proxy()` 解析 `scutil --proxy` 输出
- [ ] 1.2 `src/reqable_mcp/paths.py`
  - 解析 macOS 路径常量:`REQABLE_LMDB_DIR`、`REQABLE_REST_DIR`、`OUR_DATA_DIR`、`OUR_CACHE_DB`
  - 校验 Reqable 数据目录存在,不存在就报清晰错误
- [ ] 1.3 `src/reqable_mcp/schema.sql` — DDL 落盘
  - captures 表 + 索引、captures_fts(FTS5)+ triggers、sync_state 表、(P2 占位)rules 表
- [ ] 1.4 `src/reqable_mcp/db.py`
  - 类 `Database`:`connect()` / `init_schema()` / 主要操作:
    - `upsert_capture(record_dict)` (INSERT OR REPLACE)
    - `get_capture(uid) -> dict | None`
    - `query_recent(filters, limit) -> list[dict]`
    - `search_url(pattern, regex, limit) -> list[dict]`
    - `search_body(query, target, limit) -> list[dict]` — 用 FTS5;实际 body 命中需要 dbData 解码后判断,FTS 主要 index URL+summary
    - `get_sync_cursor(source) -> int` / `set_sync_cursor(source, ob_id)`
  - PRAGMA(WAL/synchronous/cache_size/busy_timeout)
- [ ] 1.5 测试:`tests/test_proxy_guard.py`、`tests/test_db.py`

## 模块 2:FlatBuffers + ObjectBox 解析

依赖:模块 0(纯逻辑,不依赖其他)

- [ ] 2.1 `src/reqable_mcp/sources/flatbuffers_reader.py`
  - 函数:`u8/u16/u32/i32/u64/deref/parse_table/read_string_at/read_bytes_at/read_vector_of_offsets/read_uint`
  - 完全无外部依赖(纯 stdlib struct)
  - docstring 标注:不依赖 `flatbuffers` 包(那个需要 generated 代码)
- [ ] 2.2 `src/reqable_mcp/sources/objectbox_meta.py`
  - `@dataclass Property(pid, vt_index, name, type_code)`
  - `@dataclass Entity(eid, name, properties, last_property_id)`
  - `load_schema(env: lmdb.Environment) -> dict[str, Entity]`
  - 实现:扫主 DB key prefix `\x00\x00\x00\x00\x00\x00\x00`,解每个 entity meta blob
  - 仅关心需要的 entity(`CaptureRecordHistoryEntity`),其他略过
- [ ] 2.3 测试:`tests/test_flatbuffers_reader.py`、`tests/test_objectbox_meta.py`
  - fixture:从用户机器拷一份小 LMDB(脱敏)做单元测试夹具
  - 验证能解出 `CaptureRecordHistoryEntity` 5 个字段及 vt_index

## 模块 3:数据源(LMDB + Rest)

依赖:模块 1、2

- [ ] 3.1 `src/reqable_mcp/sources/lmdb_source.py`
  - `class LmdbSource(lmdb_path, db, schema, on_new_capture: Callable)`
  - `start(poll_interval_ms=250)` — 启 daemon thread
  - `_scan_once() -> int`:
    1. 打开 LMDB readonly env (`lock=False`,共享 mmap)
    2. 用 schema 提取 CaptureRecordHistoryEntity 的 vt_index 映射
    3. 从 `db.get_sync_cursor('lmdb')` 拿 last_ob_id
    4. cursor 遍历主 DB,跳过元数据 keys
    5. 对每条 value 解 FB → 取 dbData → b64 + gunzip + json
    6. 提取 metadata(uid / ts / host / method / status / url / app / mime / sizes / comment)
    7. `db.upsert_capture(record)`,然后 `on_new_capture(record)` 通知 wait queue
    8. 更新 sync_cursor
  - 退避策略:连续空转 → 250→1000→2000ms;命中 → 重置
  - 异常处理:单条失败 log + skip,不中断整次扫描
- [ ] 3.2 `src/reqable_mcp/sources/rest_source.py`
  - `class RestSource(rest_dir)`
  - `get_request_raw(uid) -> dict | None`(读 `{rest_dir}/{uid}-req.bin`)
  - `get_response_raw(uid) -> dict | None`
  - 失败容忍:文件不存在 / JSON 解析失败 → 返回 None
- [ ] 3.3 测试:`tests/test_lmdb_source.py`、`tests/test_rest_source.py`
  - fixture LMDB 跑 `_scan_once`,断言记录数、字段提取正确
  - rest 解析多种文件名 / 损坏 JSON 容错

## 模块 4:wait_queue

依赖:模块 0(纯逻辑)

- [ ] 4.1 `src/reqable_mcp/wait_queue.py`
  - `Waiter` / `WaitQueue` 类
  - `add(filter_spec) -> waiter_id`
  - `notify(capture_dict)` — 遍历活跃 waiters,匹配 → set event 唤醒
  - `wait(waiter_id, timeout_s) -> dict | None`
  - 内存为主,跨进程不持久化(MVP 简化;Claude Code 进程退出 = wait 取消)
  - 匹配函数:host(精确)、path_pattern(regex)、method(精确)、app(精确)、status(精确,可选)
- [ ] 4.2 测试:`tests/test_wait_queue.py`
  - 注册 → notify → 命中
  - 超时返回 None
  - 多个并发 waiters 各自命中

## 模块 5:daemon + MCP server 入口

依赖:模块 1、2、3、4

- [ ] 5.1 `src/reqable_mcp/daemon.py`
  - `class Daemon(config)`:聚合 db / lmdb_source / rest_source / wait_queue
  - `start()`:scrub_env → assert_proxy_safe → load_schema(LMDB) → 启 LmdbSource 线程 → 返回(供 MCP server 使用)
  - 单实例(同一时刻只能有一个 reqable-mcp serve;用 PID 文件 + flock)
- [ ] 5.2 `src/reqable_mcp/__main__.py`
  - 命令分发:`reqable-mcp serve` / `reqable-mcp status` / `reqable-mcp install-help`
- [ ] 5.3 `src/reqable_mcp/mcp_server.py`
  - 用 `mcp` SDK 启动 stdio server
  - 注册 Tier 1/4/5 工具(从 tools/ 子模块引入)
  - 启动顺序:`Daemon.start()` → 注册工具 → `server.run_stdio()`
- [ ] 5.4 测试:`tests/test_daemon.py`(集成,启 daemon → 等首次 LMDB 扫完 → 断言 db.query_recent 返回有数据)

## 模块 6:Tier 1 查询工具

依赖:模块 5

- [ ] 6.1 `src/reqable_mcp/tools/query.py`
  - `list_recent` — 直接 SQLite 查询
  - `get_request(uid, include_body, include_response_body)`:
    1. 从 SQLite 拿 metadata
    2. 从 LMDB dbData JSON 解 body(如果有内联)
    3. fallback:RestSource 读 rest/{uid}-{req,res}.bin
    4. 返回字段加 `body_status: ok | unavailable | truncated`
  - `search_url(pattern, regex, limit)` — 走 SQLite 索引(LIKE)/ FTS5(regex)
  - `search_body(query, target, limit)` — 现实现:遍历最近 N 条,LMDB dbData JSON 中 grep;Phase 2 优化为 FTS5 增量索引
  - `to_curl(uid, multiline)`:取完整请求 → 拼 curl;multipart 用 `-F`,binary body 警告
  - `list_apps_seen(window_minutes)`:按 app_name 分组计数
  - `stats(window_minutes)`:host / method / status 分布
  - `diff_requests(uid_a, uid_b)`:method/url/headers/body 字段级 diff
- [ ] 6.2 测试:`tests/test_tools_query.py`

## 模块 7:Tier 4 + Tier 5 工具

依赖:模块 6

- [ ] 7.1 `src/reqable_mcp/tools/wait.py`
  - `wait_for(host?, path_pattern?, method?, app?, status?, timeout_seconds)`
  - 调 daemon.wait_queue.add → wait → 返回 capture dict 或 None
- [ ] 7.2 `src/reqable_mcp/tools/analysis.py`
  - `find_dynamic_fields(host, sample_size, field_locations)`:
    - 取该 host 最近 N 条请求,逐字段比对每次的值
    - 字段范围:headers / queries / body 中的 JSON 顶层 key
    - 启发式:某字段 ≥ 80% 请求出现且每次值都不同 → 列入 dynamic;每次值相同 → stable
  - `decode_jwt(token_or_uid)`:
    - 检测是否三段 base64url 形式 → 是则直接解
    - 否则当 uid,从该请求的 Authorization / Cookie / set-cookie 找 JWT
  - `extract_auth(host, window_minutes)`:
    - 列出 Authorization / Cookie / `X-*-Token` / `X-Csrf-*` / `Bearer ...` 等
- [ ] 7.3 测试:`tests/test_tools_wait.py`、`tests/test_tools_analysis.py`

## 模块 8:安装 / 卸载脚本(MVP — 最简)

依赖:模块 5、6、7

- [ ] 8.1 `install.sh`
  - 检测 Python ≥ 3.10
  - `pip install -e .`
  - 创建 `~/.reqable-mcp/`(权限 0700)
  - 检测 Reqable LMDB 路径存在,不存在则警告(用户可能没用过 Reqable)
  - 写 `~/.claude/mcp.json` 注册 server(若已注册则跳过)
- [ ] 8.2 `uninstall.sh`
  - 反向:从 mcp.json 移除、删 `~/.reqable-mcp/`
  - 完全不动 Reqable 数据 / 配置
- [ ] 8.3 README 完整版
  - 简介 / 架构图 / 安装 / 工具清单 / 故障排查 / "禁开第三方系统代理"提示

## 模块 9:端到端验证

依赖:全部上面

- [ ] 9.1 `tests/test_e2e.py`
  - 启 daemon(指向 fixture LMDB)→ 等 _scan_once 完成 → 通过 MCP 工具调 list_recent / get_request / wait_for / find_dynamic_fields,断言行为
- [ ] 9.2 真实 Reqable 跑通
  - 安装 → 启动 Claude Code → MCP 工具列表能看到 reqable-mcp.*
  - 用浏览器随便逛几个站,在 Claude Code 调 list_recent → 看到流量
  - 调 wait_for(host='example.com'),浏览器访问 → 阻塞返回正确记录
  - 调 find_dynamic_fields → 看到合理输出
- [ ] 9.3 性能验证
  - LMDB 18910 条历史记录全量首次同步 < 30s
  - 持续抓包时,新记录入 SQLite 延迟 < 500ms (poll_interval+解码)
  - daemon 进程内存稳定 < 100MB(连续运行 1 小时)
- [ ] 9.4 代理回环验证
  - daemon 进程内 `os.environ.get('HTTP_PROXY')` 为 None
  - 第三方系统代理 → stderr 警告
  - `REQABLE_MCP_STRICT_PROXY=1` 第三方代理 → 退出
- [ ] 9.5 容错验证
  - 删 `~/.reqable-mcp/cache.db` → daemon 重启自愈,从 LMDB 重新同步
  - LMDB 中某条记录 dbData 损坏 → log + skip,不影响其他记录
  - rest/{uid}-req.bin 不存在 → get_request 返回 body_status='unavailable'

## 模块 10:文档与 memory

依赖:模块 9 ✅

- [ ] 10.1 README 完整文档
- [ ] 10.2 `~/.claude/projects/<your-project>/memory/` 加 reference 类记忆
  - "reqable-mcp 项目位置 / 主数据源 LMDB 只读 / 默认装在 ~/.reqable-mcp/"

---

## 任务 → 文件 速查

| 文件 | 任务 |
|---|---|
| `pyproject.toml` | 0.2 |
| `src/reqable_mcp/proxy_guard.py` | 1.1 |
| `src/reqable_mcp/paths.py` | 1.2 |
| `src/reqable_mcp/schema.sql` | 1.3 |
| `src/reqable_mcp/db.py` | 1.4 |
| `src/reqable_mcp/sources/flatbuffers_reader.py` | 2.1 |
| `src/reqable_mcp/sources/objectbox_meta.py` | 2.2 |
| `src/reqable_mcp/sources/lmdb_source.py` | 3.1 |
| `src/reqable_mcp/sources/rest_source.py` | 3.2 |
| `src/reqable_mcp/wait_queue.py` | 4.1 |
| `src/reqable_mcp/daemon.py` | 5.1 |
| `src/reqable_mcp/__main__.py` | 5.2 |
| `src/reqable_mcp/mcp_server.py` | 5.3 |
| `src/reqable_mcp/tools/query.py` | 6.1 |
| `src/reqable_mcp/tools/wait.py` | 7.1 |
| `src/reqable_mcp/tools/analysis.py` | 7.2 |
| `install.sh` | 8.1 |
| `uninstall.sh` | 8.2 |
| `README.md` | 0.4 → 8.3 → 10.1 |

---

## Phase 2 — 写回与改流量(主体,基于实测 Reqable SDK)

> **背景** — Reqable 的 Python 脚本扩展点已经从本机 `scripts/exec/{uuid}/`
> 完整摸清:`reqable.py`(SDK)+ `main.py`(入口)+ `addons.py`(用户脚本)。
> Hook 入口为 `onRequest(context, request)` 与 `onResponse(context, response)`,
> 全部字段(method/path/queries/headers/body/code)可读可写。`context` 暴露
> `highlight`(7 色枚举)/ `comment` / `env`(跨 capture 持久 dict)/ `shared`
> (单次 onReq→onResp 共享)。
>
> **执行模型** — Reqable 对每个请求 fork 一个 Python 进程跑 main.py,
> 通过 `request.bin`(JSON in)→ `request.bin.cb`(JSON out)与脚本通信。
> 冷启动 ≈ 200ms,所以**所有规则匹配/状态都放在 daemon**,addons 只做
> "查询规则 + 应用 + 上报 hit"的薄壳。
>
> **依赖顺序的考量** — 先做 daemon 端 IPC(M11),再写 addons 模板能脱机
> 跑(M12),最后才动 Reqable 配置(M13)。这样 M11/M12 完全在我们项目
> 内可测,直到 M13 才有真实副作用。

### 配置与文件路径

| 用途 | 路径 |
|---|---|
| Reqable 主配置(要改) | `~/Library/Application Support/com.reqable.macosx/config/capture_config` |
| 脚本环境配置 | `~/Library/Application Support/com.reqable.macosx/config/script_environment` |
| 脚本部署目录(我们写) | `~/.reqable-mcp/hook/` |
| daemon IPC socket | `~/.reqable-mcp/daemon.sock` |
| 规则持久化文件 | `~/.reqable-mcp/rules.json` |
| 备份 | `~/.reqable-mcp/backup/capture_config.{ts}.bak` |

### M11 — daemon IPC(纯本地,不动 Reqable)

依赖:MVP

- [ ] 11.1 `src/reqable_mcp/ipc/protocol.py`
  - line-delimited JSON over Unix socket
  - 请求:`{"v":1,"op":"<get_rules|report_hit>","args":{...}}`
  - 响应:`{"ok":true,"data":...}` 或 `{"ok":false,"error":"..."}`
  - 单次 round-trip,无长连接(addons 进程短命)
- [ ] 11.2 `src/reqable_mcp/ipc/server.py`
  - `class IpcServer(socket_path, rule_engine, on_hit)`
  - `start()` — 启 listener thread + 每连接一个短 handler thread
  - 5s 读超时 → 关连接(防 addons 卡死后挂死 daemon)
  - 子进程崩 / 半包 → 静默 close,不影响主循环
- [ ] 11.3 `src/reqable_mcp/rules.py`
  - `@dataclass Rule(id, kind, host_pattern, path_pattern, side, action_payload, ttl_s, created_ts, hits)`
  - `kind ∈ {tag, comment, inject_header, replace_body, mock, block}`
  - `class RuleEngine`:`add / remove / list / match(context_dict) → list[Rule]`
  - 持久化:`rules.json` 原子写(tempfile + os.replace)
  - 启动加载,过期项 sweep
- [ ] 11.4 `Daemon.start()` 接入 IpcServer + RuleEngine
  - socket 文件 0600 perm,启动前 unlink
  - 退出时 stop + unlink
- [ ] 11.5 测试:`tests/test_ipc.py` / `tests/test_rules.py`
  - 启 server → 客户端发 `get_rules` → 验响应
  - rule TTL 过期自动剔除
  - 并发多 connection 不互相阻塞

### M12 — addons 模板(脚本生成,不动 Reqable)

依赖:M11

- [ ] 12.1 `src/reqable_mcp/hook/template/main.py` — copy from
  `scripts/exec/{uuid}/main.py`(reqable 官方逻辑)逐字保留
- [ ] 12.2 `src/reqable_mcp/hook/template/reqable.py` — copy from 官方
- [ ] 12.3 `src/reqable_mcp/hook/template/addons.py` — 我们的薄壳:
  ```python
  from reqable import *
  import os, json, socket
  SOCK = os.path.expanduser("~/.reqable-mcp/daemon.sock")

  def _ask(side, ctx, msg):
      try:
          s = socket.socket(socket.AF_UNIX); s.settimeout(0.3); s.connect(SOCK)
          s.sendall((json.dumps({
              "v":1,"op":"get_rules",
              "args":{"side":side,"host":ctx.host,"path":msg.path if side=="request" else msg.request.path,
                      "method":msg.method if side=="request" else msg.request.method}
          })+"\n").encode())
          buf = b""; 
          while b"\n" not in buf: buf += s.recv(4096)
          s.close()
          return json.loads(buf.split(b"\n",1)[0]).get("data",[])
      except Exception:
          return []  # fail-open

  def _apply(rules, ctx, msg, side):
      hits = []
      for r in rules:
          k = r["kind"]
          if k == "tag":           ctx.highlight = Highlight[r["color"]]
          elif k == "comment":     ctx.comment = r["text"]
          elif k == "inject_header": msg.headers[r["name"]] = r["value"]
          elif k == "replace_body":  msg.body = r["body"]
          elif side == "response" and k == "mock":
              msg.code = r["status"]; msg.body = r["body"]
              for h, v in (r.get("headers") or {}).items(): msg.headers[h] = v
          elif side == "request" and k == "block":
              # block 暂用空响应短路 — 由 M13 实测语义后定稿
              raise RuntimeError("blocked by reqable-mcp rule "+r["id"])
          hits.append(r["id"])
      return hits

  def _report(hits, ctx, side):
      if not hits: return
      try:
          s = socket.socket(socket.AF_UNIX); s.settimeout(0.3); s.connect(SOCK)
          s.sendall((json.dumps({"v":1,"op":"report_hit",
              "args":{"side":side,"uid":ctx.uid,"rule_ids":hits}})+"\n").encode())
          s.close()
      except Exception:
          pass

  def onRequest(context, request):
      hits = _apply(_ask("request", context, request), context, request, "request")
      _report(hits, context, "request")
      return request

  def onResponse(context, response):
      hits = _apply(_ask("response", context, response), context, response, "response")
      _report(hits, context, "response")
      return response
  ```
- [ ] 12.4 `src/reqable_mcp/hook/deploy.py`
  - `deploy_to(target_dir: Path)` — 把模板拷到 `~/.reqable-mcp/hook/`
  - 幂等:存在则 diff;同则跳过,不同则覆盖
- [ ] 12.5 测试:`tests/test_hook_deploy.py`(纯文件操作)
- [ ] 12.6 脱机端到端:手工跑 `python3 hook/main.py request hook/template/sample_request.bin`
  → 验证 cb 文件生成 + 内容符合预期

### M13 — install_hook(动 Reqable 配置,可逆)

依赖:M11、M12

- [x] 13.1 `install_hook.sh` 流程(完成,但 hook 没被 Reqable 调用,见 13.5)
- [x] 13.2 `uninstall_hook.sh`(完成 + 实测验证还原)
- [x] 13.4 单元测试 16 个(纯 fixture)

#### 13.5 实装失败的根因 — 真 schema 比预想复杂

**实测发现**(2026-04-25,Reqable 3.0.40 在本机 macOS):

我们的 `install-hook` 写入 `capture_config.scriptConfig.scripts[0]` 一条
带 `path` 字段指向 `~/.reqable-mcp/hook/` 的条目,期待 Reqable 启动后
fork python3 跑那个目录里的 main.py。**实际行为**:

* Reqable **接受**了我们的 schema(配置项保留,没回滚)。
* 但**从不调用** hook(daemon `connections_total` 始终为 0,`scripts/exec/`
  没有新 uuid 目录,Reqable 日志里没有 reqable-mcp 相关条目)。
* `script_environment` 被 Reqable 启动时**强制重写**为内置默认值
  (`{"executor": "python3", "version": "3.9.6", "home": ""}`),
  说明 Reqable 主动 detect Python,会忽略我们指定的 venv executor。

**根因**(从 Reqable 二进制 strings + LMDB schema 反推):

1. `UserScriptTemplateEntity` 的 schema 是 `{id, timestamp, name, script}`,
   其中 `script` 字段是 PROP_STRING(type=9)—— **Reqable 把 Python
   源码直接存在 LMDB 里**,不是文件路径。
2. `~/Library/.../scripts/exec/{uuid}/` 是 Reqable **运行时**临时建的:
   每次 fork 前从 LMDB UserScriptTemplate.script 读源码,写到 addons.py,
   附上内置 main.py + reqable.py,再 fork。执行完目录可能被删(也可能
   遗留作历史,本机有 2025 年的旧 uuid 目录)。
3. `capture_config.scriptConfig.scripts[]` 的每项是**引用** template
   的 id,字段大概是 `scriptId`(strings 里有 `scriptId`)。
   `path` 字段我们填了但 Reqable 忽略 —— 它只在 `execHome`(根目录)用,
   不针对单个 script。

**这违反了 MVP 原始硬约束**:"绝对不向 Reqable LMDB 写入"
(spec.md "强约束"段)。要让 hook 真正运行,必须在 LMDB 加一条
UserScriptTemplate 行,或绕过这个机制。

#### 13.6 后续路径(三选一,需用户决策)

- [ ] 13.6.A **写 LMDB UserScriptTemplate** ——
  破解 ObjectBox 写路径(分配 ob_id、构造 FlatBuffers 表、维护
  indexes)。可行性高(我们已会读;写就是逆向),但工程量大,
  且永久打破"只读 LMDB"约束。
- [ ] 13.6.B **让用户在 Reqable UI 手动建一次脚本** ——
  Pro 授权前提下用户在 UI 创建一条 UserScriptTemplate,粘贴我们
  ``addons.py`` 内容。我们 ``install-hook`` 改成只更新 capture_config
  的 ``scriptConfig.scripts[]`` 引用 template id。**问题**:每次我们
  升级 addons 模板,用户都要重新粘贴一次。
- [ ] 13.6.C **不靠 hook 改流量,只靠规则** ——
  放弃 onRequest/onResponse 拦截能力,把 Phase 2 缩水到只做"反向标记"
  (tag/comment 走另一种机制)。可能可以从 LMDB 直接 update 已捕获 record
  的 highlight / comment 字段而不动 hook —— 但同样要写 LMDB。

**当前状态**:hook 已通过 ``./uninstall_hook.sh`` 完全回滚,Reqable
配置干净。M11 (IPC + RuleEngine) 和 M12 (addons 模板 + deploy) 这两块
代码都没浪费 —— 任何后续路径都需要它们。

### M14 — 标记类 MCP 工具(Tier 2)

依赖:M11、M13

- [ ] 14.1 `src/reqable_mcp/tools/tag.py`
  - `tag_pattern(host=, path_pattern=, color=, ttl_seconds=300) → rule_id`
  - `comment_pattern(host=, path_pattern=, text=, ttl_seconds=300) → rule_id`
  - `untag(rule_id)` / `clear_tags()`
  - `list_rules(kind?=)` — 含 hit 计数
- [ ] 14.2 测试:`tests/test_tools_tag.py`(规则注册 → addons hit 模拟 → 验 db 中 capture 行 highlight 字段被更新)

### M15 — 改包类 MCP 工具(Tier 3,危险)

依赖:M14 已完成;Reqable addons SDK(`reqable.py`,1003 行)实测过

**SDK 调研结论(2026-04-25)**

`/Applications/Reqable.app/Contents/Frameworks/App.framework/.../assets/resources/overrides-python.zip` 只是给被脚本"代理出去的"Python 程序套代理,**不是 addons SDK**。真实 SDK 在 Reqable 运行时落到 `~/Library/Application Support/com.reqable.macosx/scripts/exec/<uuid>/{reqable.py, main.py, addons.py}`,fork-per-request 跑 `main.py request|response <file>`,读 JSON,调 addons.onRequest/onResponse,把 `result.serialize()` 写到 `<file>.cb`。返回 None 不写 cb,Reqable 原样放行。

可改字段:
- `HttpRequest`: `method / path / queries / headers / body / trailers`
- `HttpResponse`: `code(100-600) / headers / body / trailers`
- `Context.host` **没有 setter** → 改 host 重定向不可行
- body 接受 `str/bytes/dict/HttpBody`(dict 自动 json.dumps,bytes 经 IPC JSON 传不过来)

三个能力的真实实现路径:
- **replace_body**:`msg.body = ...` 直接生效。binary 不支持(JSON 不能编码 bytes)
- **mock_response**:Reqable **没有从 onRequest 短路返回响应的 API**。只能在 onResponse 改 status/headers/body。**上游请求一定会发出去,客户端看到伪造响应,但上游也被打了一次** — docstring 必须把这个约束写明
- **block_request**:在 onRequest `raise RuntimeError`。Reqable 抓到异常会中止 session,客户端看到 connection 错误。这是唯一阻止上游被打的方式

**已就位的代码**(M11/M12 阶段预埋):
- `rules.py` 的 `Rule/RuleEngine` 已支持 `replace_body / mock / block` 三种 kind,`add()` 已强制 `mock=response` / `block=request`
- IPC `get_rules / report_hit` 已透传 payload,**协议无需扩展**
- `hook/template/addons.py::_apply_rule` 已实现这三种 kind 的应用逻辑(line 156-198)

**M15 实际工作量**:工具层 + 几处护栏 + 测试

- [ ] 15.1 在 `src/reqable_mcp/tools/rules.py` 末尾追加三个工具(同模块,与 M14 工具复用 `_engine_or_error` 等帮手):
  - `replace_body(body, host=None, path_pattern=None, method=None, side="request"|"response", ttl_seconds=300)` — body 限 str 或 dict(JSON 自动序列化),拒绝 None/bytes/list,大小 ≤ `BODY_MAX_BYTES`
  - `mock_response(status=None, body=None, headers=None, host=None, path_pattern=None, method=None, ttl_seconds=300)` — side 隐含 response。docstring 顶端 **「上游仍会被请求」** 警告。status 校验 100-600。headers dict[str,str]
  - `block_request(host=None, path_pattern=None, method=None, ttl_seconds=300)` — side 隐含 request。docstring 顶端「请求被拦截,客户端看到 connection error」
- [ ] 15.2 `rules.py` 加 `BODY_MAX_BYTES = 64 * 1024`(IPC 帧 256KB,留余量给规则数组)
- [ ] 15.3 `addons.py` 模板 `_apply_rule` 修两处:
  - block 命中:**先 report_hit 再 raise**,否则统计永远 0(目前 raise 会跳出 for 循环,后面 `_report_hits` 不执行)
  - replace_body 的 `bytes` 分支删除(IPC 传不过来,误导)
- [ ] 15.4 测试 `tests/test_tools_rules.py` 增量:
  - 三个新工具的快乐路径 + payload 校验错误路径(body 太大、status 越界、type 错误)
  - `mock_response` 工具默认 side=response、`block_request` 工具默认 side=request 的传参验证
  - daemon 重启后规则持久化(已有的 `TestPersistenceBetweenDaemons` 模式扩展)
- [ ] 15.5 `tests/test_hook_e2e.py` 加端到端单测:把 stub addons 跑起来,装 mock/replace/block 规则,断言 daemon 收到 hit、规则的 payload 字段被 addons 读出来后效果对(注:不能真起 Reqable,只能验证 addons.py + daemon socket 的握手)
- [ ] 15.6 `panic_button` / `disable_all_rules` **不需要做** — `clear_rules` 已经是它(M14 完成)

注:之前 M16 列的"规则后台 reaper / status 显示 active_rules"在 daemon 已有 `record_hit / list_all` 自动过滤过期规则,且 `status` 已经返回 `rule_engine.stats()`(by_kind / total_hits / active)。M16 大部分已落实,留一条"后台 30s reaper"作为后续优化。

### M16 — 规则管理 + 安全护栏

依赖:M14、M15

- [ ] 16.1 daemon 启动时扫一次 rules.json,清掉过期的
- [ ] 16.2 后台 reaper 每 30s 清过期规则
- [ ] 16.3 `status` 工具增加 `active_rules` / `rule_hits_total` 字段
- [ ] 16.4 hook 模板的 `_ask` 失败计数:连续 N 次 timeout → daemon 在 stderr warn(可能 daemon 死了,Reqable 还开着 hook)
- [ ] 16.5 `panic_button` 也走 IPC,addons.py 启动后第一件事就拉 rule list — 清空就立刻不再注入

---

## Phase 3 — 增值工具(可选,Phase 2 落地后)

### M17 — 重放与导出

依赖:M11(用 IPC 通知 hit?可选)

- [ ] 17.1 `src/reqable_mcp/tools/replay.py`
  - `replay_request(uid, modifications={...})` — stdlib `urllib.request`,严格设
    `OpenerDirector` + `ProxyHandler({})` 旁路所有代理
  - 返回新响应的 status / headers / body
  - 不写回 LMDB(我们的 db 仍只读),仅返回给调用者
- [ ] 17.2 `src/reqable_mcp/tools/export.py`
  - `dump_body(uid, side="req"|"res", path)` — 写到本地文件
  - `export_har(uids|host|window, path)` — HAR 1.2 格式
  - `decode_body(uid, side)` — gzip/br/deflate/zstd 解码
  - `prettify(uid, side)` — JSON / XML / HTML 格式化

### M18 — 自动 token 中继(SDK env 字段杀手锏)

依赖:M11

- [ ] 18.1 `auto_token_relay(source_host, source_field, target_host, target_header, ttl_seconds)`
  - 注册一条特殊规则:onResponse 时如果 host=source_host,
    从 body/header 提取 source_field → 写入 `context.env["_relay_<name>"]`
  - onRequest 时如果 host=target_host,从 env 读 → 注入 target_header
  - 这是 Reqable SDK `context.env` 跨 capture 持久的实际应用

### M19 — body 全文检索增量化

- [ ] 19.1 FTS5 索引 body(可配置开关,默认关 — 节省 SQLite 空间)
- [ ] 19.2 `search_body` 大数据量下走 FTS5,fallback 现有线性扫描

---

## Phase 4 — 想象空间(用户提需求再定)

- [ ] 自动 highlight 4xx/5xx / 慢请求(>1s)
- [ ] multipart 改包(改上传文件)
- [ ] capture 数据回溯导出 mitmproxy flow 格式
- [ ] 规则 dry-run 模式(只 log 不真改)

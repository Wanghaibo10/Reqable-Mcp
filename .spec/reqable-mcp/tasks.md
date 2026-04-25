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

## Phase 2 任务(MVP 验收 + 1 周稳定运行后)

- [ ] P2.1 `addon/main.py` + `addon/addons.py` — Reqable 脚本入口,含规则应用 + 反向写
- [ ] P2.2 `src/reqable_mcp/sources/hook_source.py` — IPC server,接 addons 推送的 hit 事件
- [ ] P2.3 `src/reqable_mcp/rules.py` — 规则引擎(tag/modify/mock/block),`rules.json` 原子写
- [ ] P2.4 `src/reqable_mcp/tools/tag.py` — `tag_pattern / untag_pattern / comment_request / list_tags`
- [ ] P2.5 `src/reqable_mcp/tools/modify.py` — `mock_response / block / replace_body / inject_header / replace_field`
- [ ] P2.6 install.sh 增加 hook 启用流程:
  - 检测 Reqable 已退出 → 否则提示用户先关
  - 创建 `scripts/exec/<NEW_UUID>/` + 复制 main.py/addons.py/reqable.py
  - 备份 capture_config → 修改 `scriptConfig.scripts/isEnabled` → 写回
- [ ] P2.7 uninstall.sh 加还原 capture_config 步骤
- [ ] P2.8 端到端测试 hook 链路:Claude 调 tag_pattern → addons 应用规则 → Reqable UI 显示颜色

## Phase 3 任务(可选)

- [ ] P3.1 `src/reqable_mcp/tools/export.py` — `dump_body / export_har / decode_body / prettify`
- [ ] P3.2 `replay_request(uid, modifications)` — 重发请求(用 stdlib `urllib`,严格 bypass 系统代理)
- [ ] P3.3 FTS5 增量索引 body(优化 search_body 大数据量性能)

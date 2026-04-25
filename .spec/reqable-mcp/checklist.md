# reqable-mcp 验证清单(v2 — 方案 C)

## 代码实现

- [ ] tasks.md 模块 0~10 全部完成
- [ ] 全部单元测试通过(`pytest tests/`)
- [ ] 端到端测试通过(`tests/test_e2e.py`)
- [ ] 代码风格统一(ruff/black 跑过,无警告)
- [ ] 类型注解完整(`mypy src/`,无 error)
- [ ] **MVP 阶段不引入 `addon/` 实际逻辑**(目录可存在,占位)
- [ ] **MVP 阶段不修改任何 Reqable 数据/配置文件**(grep 代码确认无写 `~/Library/Application Support/com.reqable.macosx/`)

## 数据源:LMDB 解析正确性

- [ ] `objectbox_meta.load_schema()` 在真实 Reqable LMDB 上能解出 `CaptureRecordHistoryEntity`
- [ ] entity 5 个字段(id, uid, timestamp, dbData, dbUniqueId)的 vt_index 与 type_code 提取正确
- [ ] `LmdbSource._scan_once()` 解一条记录得:
  - uid 是 UUID 字符串(36 字符,带连字符)
  - timestamp 转为合理 Python datetime
  - dbData 解码后是 valid JSON,顶层 keys 包含 session/appInfo
  - 提取出来的 url/method/host/status 跟 Reqable UI 显示一致(抽样验证 5 条)
- [ ] 增量同步:启动后 last_ob_id 正确推进;再次启动从游标续跑,不重复入库
- [ ] dbData 损坏(故意改 base64 头) → log + skip,继续后续记录
- [ ] LMDB 在 Reqable 同时写入时,我们 readonly 打开不报错、不阻塞 Reqable

## 数据源:Rest 文件读取

- [ ] `RestSource.get_request_raw(uid)` 在已存在文件时返回 dict,字段含 method/path/headers/body
- [ ] 文件不存在 → 返回 None,不抛异常
- [ ] JSON 损坏 → 返回 None + log

## 功能验证(MVP)

### Tier 1 查询
- [ ] `list_recent()` — limit / host / method / status / app 各条件可用,结果按 ts DESC
- [ ] `get_request(uid)` — 返回完整 metadata + body;`body_status` 字段正确表示 ok/unavailable
- [ ] `get_request(uid, include_body=False)` — 不带 body
- [ ] `search_url(pattern)` — 子串和 `regex=True` 都可用
- [ ] `search_body(query, target='req'/'res'/'both')` — 命中 dbData JSON 中字符串
- [ ] `to_curl(uid)` — POST + JSON body 可以直接 `bash` 跑通(注意要避开抓包,详见代理回环)
- [ ] `list_apps_seen()` — 真实显示 Chrome / Safari / Reqable / 命令行工具
- [ ] `stats()` — host / method / status 分布合理
- [ ] `diff_requests(a, b)` — 字段级 diff

### Tier 4 阻塞等待
- [ ] `wait_for(host=...)` — 命中后立即返回(< poll_interval+50ms)
- [ ] `wait_for(timeout_seconds=2)` — 无匹配 2 秒后返回 None
- [ ] 多个并发 wait_for 互不影响

### Tier 5 逆向辅助
- [ ] `find_dynamic_fields(host)` — 在已知含动态 token 的 host 上能识别该字段
- [ ] `decode_jwt(token)` — 解出 header / payload / signature
- [ ] `decode_jwt(uid)` — 自动从该请求里找 JWT(Authorization 或 Cookie)
- [ ] `extract_auth(host)` — 列出常见 auth 字段(Authorization / Cookie / 自定义 token)

### 边界场景
- [ ] Reqable 未安装 / LMDB 不存在 → 启动时清晰报错,不崩
- [ ] 本地 cache.db 不存在 → 自动初始化 + 全量同步 LMDB
- [ ] `~/.reqable-mcp/` 权限不足 → 报错并提示
- [ ] LMDB 某条记录 dbData 解码失败 → log + skip,其他记录正常入库
- [ ] rest/{uid}-req.bin 不存在 → get_request 返回 body_status='unavailable'
- [ ] LMDB 文件被 Reqable 重启清空 → daemon 检测 last_ob_id > LMDB 当前最大,降级重新全量

### 错误处理
- [ ] 任何工具收到非法参数(uid 不存在 / pattern 编译失败) → 返回结构化 MCP error,不崩
- [ ] LMDB env 打开失败 → daemon 启动失败,清晰错误信息
- [ ] SQLite 写失败(磁盘满) → log + 内存 buffer 暂存,等下次重试

## 防代理回环验证(强约束)

- [ ] daemon 进程内:`os.environ.get('HTTP_PROXY' / 'HTTPS_PROXY' / 'ALL_PROXY')` 全为 None
- [ ] daemon 进程内:`os.environ['NO_PROXY'] == '*'`
- [ ] 系统代理设为非 Reqable 第三方代理(如 Clash 7890)→ daemon 启动 stderr 出现警告
- [ ] 系统代理为 Reqable 自己 → 不警告,正常启动
- [ ] `REQABLE_MCP_STRICT_PROXY=1` 检测到第三方 → 启动失败 + exit 2
- [ ] **代码层面禁用 HTTP 客户端**:`grep -rE "import (requests|urllib3|aiohttp|httpx)" src/` 无匹配
- [ ] `to_curl` 输出的 curl 命令含 `--noproxy '*'` 提示(让用户用时也避开代理回环)

## 集成验证

- [ ] 不动 Reqable LMDB:验证 `data.mdb` mtime 在 daemon 运行期间只由 Reqable 自身改,不由我们改
- [ ] 不动 Reqable 配置:`config/capture_config` mtime 不变
- [ ] 不动 Reqable 脚本目录:`scripts/exec/` 无新建子目录
- [ ] Reqable 重启后,daemon 自动检测、继续工作(可能需要短时间重新打开 LMDB)
- [ ] Claude Code 中 `/mcp` 列出 reqable server 状态 connected
- [ ] 在 Claude Code 对话里调用工具,无 protocol error

## 性能验证

- [ ] 18910 条历史首次全量同步 < 30s
- [ ] 持续抓包时,新记录入 SQLite 延迟 < 500ms(poll 250ms + 解码)
- [ ] daemon 内存连续 1 小时 < 100MB
- [ ] cache.db 1 万条记录后 < 50MB
- [ ] LMDB 轮询 CPU 占用 < 2%(空闲时退避后)

## 安装/卸载

- [ ] `install.sh` 在干净环境(无 daemon、无 mcp.json 注册)下一键完成
- [ ] `install.sh` 幂等:再跑一次不出错、不重复注册
- [ ] `uninstall.sh` 完全清理:`~/.reqable-mcp/`、mcp.json 中本工具的 entry
- [ ] 卸载后 Reqable 抓包不受影响(可视化对比:抓包数 / 配置文件 mtime)

## 文档

- [ ] README 含:简介 / 架构图 / 一键安装 / 工具清单 / 常见问题
- [ ] README 显著说明"防代理回环"约束
- [ ] 每个 MCP 工具的 docstring 清晰,Claude Code 能读到
- [ ] 解释 MVP 不需要 Reqable Pro / 启用脚本的"零侵入"特性
- [ ] CHANGELOG.md(v0.1.0 = MVP)

## 安全

- [ ] `~/.reqable-mcp/` 权限 0700
- [ ] LMDB env 用 `lock=False, readonly=True` 打开
- [ ] daemon log 不出现 body / token / cookie 明文(只记录 metadata 与异常类型)
- [ ] 抓包数据不外发任何远程地址(grep daemon source 无外部 URL)
- [ ] 卸载脚本绝对不动 Reqable 数据 / 配置文件

## Phase 2 入场前检查

MVP 全部 ✅ 后再启动 P2:
- [ ] 真实日常使用 ≥ 1 周,无 daemon 崩溃 / 数据不一致
- [ ] 收集对 Tier 2/3 反向标记/改包 的真实需求场景(至少 3 个具体用例)
- [ ] Phase 2 设计文档单独走一轮 spec 流程

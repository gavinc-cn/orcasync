# orcasync 优化实施方案

本文是把 [sync-improvements.md](sync-improvements.md) 中的修复方向、[comparison-syncthing-mutagen.md](comparison-syncthing-mutagen.md) 中两家成熟项目的做法,落到具体落地步骤上的完整改造计划。**重点关注:每一处行为变化都必须可观测**——文件变更、扫描、冲突、传输、重试、回弹拒绝,全部要有结构化日志。

---

## 一、目标与原则

### 1.1 目标

1. **正确性优先**:消除 mtime-only 冲突解决、消除无校验的数据传输,任何时候不丢用户数据。
2. **可靠性兜底**:事件机制必有 scan 兜底,做到"事件丢了/系统休眠/inotify 溢出"都不影响最终一致性。
3. **大规模可用**:大文件不爆内存,大目录冷启动秒级完成,长时间运行不退化。
4. **全程可观测**:任何一次同步动作都能从日志重建出"发生了什么、为什么这么决定"。

### 1.2 原则

- **结构化优先**:所有日志带 `event=`、`path=`、`size=`、`reason=` 等键值,便于 grep/jq 分析。
- **状态机要持久**:manifest、staging、版本号都落盘,重启后能续上而不是从零开始。
- **改造可分期**:每个 milestone 单独可发布,不引入"一改全改"的耦合。
- **学 Syncthing 的工程化**(version vector、temp file、index db),学 Mutagen 的清晰边界(stage→transition、scan-and-reconcile)。

---

## 二、路线图(三个 Milestone)

### M1 — 数据安全与可观测(P0,约 1 周)

聚焦"不丢数据 + 看得见":统一日志、流式写盘、端到端 hash 校验、周期 rescan、冲突保留。

### M2 — 性能与规模(P1,约 1~2 周)

聚焦"扛得住大目录大文件":持久化 manifest、流水线请求、分块流式 manifest 传输、watcher 防抖与事件聚合调优。

### M3 — 正确性进阶(P2/P3,按需)

聚焦"成熟项目的正确性高地":snapshot/version 化的冲突检测、staging 目录与 echo 结构性消除、可选的 rsync 风格弱-强双 hash。

---

## 三、统一日志规范(横切要求)

> 用户特别强调:**程序运行日志要记录,例如文件变更、扫描等**。本节为所有后续改动的公共约束。

### 3.1 日志库与级别

- 继续使用 stdlib `logging`,初始化时强制 **结构化 formatter**:`%(asctime)s %(levelname)s [%(name)s] %(message)s`,业务字段通过 `extra=` 传入,以 `key=value` 形式追加到 message 末尾。
- 级别约定:
  - `DEBUG`:逐块传输细节、watcher 原始事件、scan 中的每个 stat 调用。
  - `INFO`:文件级动作(create/modify/delete 同步完成)、scan 摘要、rescan 触发、连接生命周期。
  - `WARNING`:hash 不匹配重试、冲突检测、watcher 溢出降级、超时。
  - `ERROR`:连接断开、写盘失败、不可恢复的协议错。
- 新增 CLI `--log-level` 和 `--log-format=text|json`,`json` 走 `python-json-logger` 直接吐 JSON line。

### 3.2 必须打点的事件清单

| 事件 | 级别 | 关键字段 |
|------|------|----------|
| watcher 原始事件 | DEBUG | `event_type, path, is_dir, source=watchdog` |
| watcher 防抖触发(实际派发) | INFO | `event_type, path, debounced_count, age_ms` |
| scan 启动 | INFO | `scan_type=full|incremental, root, trigger=startup|timer|overflow|manual` |
| scan 结束 | INFO | `scan_type, files, dirs, bytes, duration_ms, ignored` |
| 单文件 hash 计算 | DEBUG | `path, size, blocks, duration_ms, cache_hit=bool` |
| manifest diff 完成 | INFO | `peer, files_need_pull, files_need_push, bytes_to_transfer` |
| request_blocks 发出 | DEBUG | `path, indices, request_id` |
| block 接收 + 校验 | DEBUG | `path, index, size, hash_ok=bool` |
| block 校验失败 + 重试 | WARNING | `path, index, expected, got, retry=N` |
| 文件同步完成 | INFO | `direction=pull|push, path, size, blocks_transferred, bytes_transferred, duration_ms` |
| 冲突检测 | WARNING | `path, local_mtime, remote_mtime, local_hash, remote_hash, resolution=keep_local|keep_remote|both` |
| 冲突文件生成 | WARNING | `original_path, conflict_path, loser=local|remote` |
| 回弹被抑制 | DEBUG | `path, reason=fingerprint_match|in_apply_window` |
| inotify 溢出 / watch 失败 | WARNING | `error, action=trigger_full_rescan` |
| 周期 rescan 触发 | INFO | `interval_s, next_at` |
| 连接事件 | INFO | `event=connect|disconnect, peer, duration_s` |

### 3.3 日志的代码形态

新增 `orcasync/logging_util.py`:

```python
def log_event(logger, level, message, **fields):
    """统一带 kv 字段的结构化日志:'sync.done path=a/b.txt size=1024 duration_ms=42'"""
    if fields:
        suffix = " " + " ".join(f"{k}={v}" for k, v in fields.items())
    else:
        suffix = ""
    logger.log(level, message + suffix)
```

所有 `logger.info("Synced: %s", path)` 之类的旧写法**统一替换为**:

```python
log_event(logger, logging.INFO, "sync.done",
          direction="pull", path=path, size=size, duration_ms=elapsed_ms)
```

---

## 四、M1 详细方案(P0)

### 4.1 流式写盘 + atomic rename

**改造点**:[session.py:160-186](../orcasync/session.py#L160-L186)、[sync_engine.py:142-152](../orcasync/sync_engine.py#L142-L152)。

- 接收侧不再使用 `_pending_blocks` 累积。引入 `class StagingFile`:
  - 写入路径:`<root>/.orcasync/staging/<sha256-of-path>.partial`(放 `.orcasync/` 子目录,scan 与 ignore 默认跳过)。
  - 收到第一个 block:`open("wb")` + `os.posix_fallocate(fd, 0, expected_size)`(Linux);Windows 退化为 `SetFilePointerEx + SetEndOfFile`。
  - 收到每个 block:`f.seek(idx*BLOCK_SIZE); f.write(payload)`,**立即 SHA-256 校验**(见 4.2)。
  - 收齐 + `transfer_done`:`f.truncate(expected_size); f.close(); os.replace(partial, target)`。
- 状态机:`pending → receiving → verifying → applied`,每次跃迁打 DEBUG 日志。
- 内存占用从 `O(file_size)` 降到 `O(block_count)` 的位图。

**日志**:
- `transfer.staging_open path=… expected_size=…`
- `transfer.block_written path=… index=… hash_ok=true`
- `transfer.applied path=… size=… duration_ms=… blocks=…`

### 4.2 端到端 block 校验 + 重试

**改造点**:[protocol.py](../orcasync/protocol.py)、[session.py:136-186](../orcasync/session.py#L136-L186)。

- `block_data` header 增加 `hash` 字段(发送侧已经有 manifest 里的 hash,直接复用)。
- 接收侧写盘前对 payload 重算 SHA-256:
  - 一致 → 写入。
  - 不一致 → 丢弃 + 重新 `request_blocks(path, [idx], retry=N+1)`,header 带上 `retry` 计数。
- 连续 3 次失败 → 标记此文件为 `failed`,从 `_pending_transfers` 移除并打 `ERROR`;**整个会话继续**,不因单文件挂掉。

**日志**:`block.hash_mismatch path=… index=… expected=… got=… retry=2`、`block.gave_up path=…`。

### 4.3 周期 rescan + 触发式 rescan

**改造点**:新增 `orcasync/rescanner.py`,被 `SyncSession` 和 `LocalSyncSession` 共用。

- 三种触发源:
  1. **定时**:`asyncio.create_task` 跑 `while True: await asyncio.sleep(interval); await rescan()`。默认 `full_rescan_s=3600`、`incremental_rescan_s=600`,可在 CLI 设。
  2. **watcher 失败**:`FileWatcher` 抛 `IN_Q_OVERFLOW` 等异常时,catch 后 `await session.request_rescan(reason="watcher_overflow")`。
  3. **手动**:CLI 增加 `orcasync resync` 子命令,或通过本地 socket / signal 触发。
- 两种扫描深度:
  - **incremental**:只 `os.stat`,与上次 manifest 比 `(size, mtime)`,变了再算 hash。
  - **full**:不信任 cache,对每个文件重算所有块 hash,用来对抗 bit rot。

**日志**:
- `scan.start type=incremental trigger=timer`
- `scan.done type=incremental files=12431 changed=7 hashed=7 ignored=89 duration_ms=320`
- `scan.full_triggered reason=watcher_overflow`

### 4.4 冲突保留(.sync-conflict-*)

**改造点**:[sync_engine.py:88-118](../orcasync/sync_engine.py#L88-L118) 的 `diff_manifests`,以及 session/local_sync 的写盘前判断。

- 检测条件(过渡方案,M3 升级为 version vector):
  - 双方 hash 都与 ancestor(manifest 缓存中的上次值)不同,且 mtime 差 < 5 秒。
  - 或者双方在上次 sync 之后**都有过本地 watcher 事件**。
- 处理:
  - 落败方(mtime 较旧者,完全相等则按 hostname 字典序)在本地保留为 `<original>.sync-conflict-<YYYYMMDD-HHMMSS>-<hostname>.<ext>`。
  - 胜方正常应用。
  - **不静默选择**:发出 `WARNING` 级日志,并在下一版加入 CLI 状态查询 `orcasync status` 列出所有 conflict 文件。

**日志**:
- `conflict.detected path=… local_mtime=… remote_mtime=… local_hash=ab12 remote_hash=cd34`
- `conflict.kept_both original=a/b.txt loser=local conflict_file=a/b.sync-conflict-20260515-103045-hostA.txt`

---

## 五、M2 详细方案(P1)

### 5.1 持久化 manifest 缓存

#### 5.1.0 已实现：内存级 mtime+size 缓存（M1.5，2026-05-16）

**改造点**:[sync_engine.py `scan_directory()`](../orcasync/sync_engine.py)，[rescanner.py `run_once()`](../orcasync/rescanner.py)，[local_sync.py `run_initial_sync()`](../orcasync/local_sync.py)。

在进入 SQLite 持久化之前，先做了运行时内存缓存：

- `scan_directory()` 新增可选参数 `known_manifest`。扫描每个文件时先 `os.stat`，若 `(size, mtime)` 与 `known_manifest` 中的缓存项完全一致，**直接复用旧的 block 列表，不读文件内容**。
- `PeriodicRescanner.run_once()` 把 `self._known`（上次扫描结果）传入作为缓存。初始 seed 来自 `run_initial_sync()` 后的 baseline manifest。
- `run_initial_sync()` 完成后的两次 baseline 扫描也传入初始 manifest 作为缓存，大量未变更文件直接跳过 hash。
- `scan_directory()` 在有 `known_manifest` 时以 DEBUG 级别打印缓存命中统计：`scan.cache_stats hits=… misses=… hit_rate=…%`。

**效果**：对 5000 文件、大多数未变更的目录（典型情况）：
- 定时 rescan：从 ~14 分钟（全量 hash）→ 几秒（纯 stat）
- 初始同步后 baseline 扫描：大幅加速

**局限**：缓存在进程内存中，重启后初始扫描仍需全量 hash（首次启动无法受益）。跨重启的加速需要 5.1.1 的 SQLite 持久化。

#### 5.1.1 持久化 manifest 缓存（待实现）

**新增**:`<root>/.orcasync/manifest.db`(SQLite，单文件，跨平台)。

- Schema:
  ```sql
  CREATE TABLE files (
    path TEXT PRIMARY KEY,
    is_dir INTEGER NOT NULL,
    size INTEGER,
    mtime REAL,
    blocks_json TEXT,       -- JSON 数组，[{index,size,hash}]
    version_counter INTEGER DEFAULT 0,   -- 预留 M3 用
    updated_at REAL
  );
  CREATE INDEX idx_mtime ON files(mtime);
  ```
- scan 时:`SELECT size, mtime, blocks_json FROM files WHERE path=?` → 若 `(size, mtime)` 不变则**跳过 hash 计算**直接复用。
- 同步完成后:`INSERT OR REPLACE`。
- 启动时:用 db 当作初始 manifest,只补差异。**大目录冷启动从分钟级降到秒级**（解决首次启动问题）。

**日志**:`manifest.cache_hit path=… files_reused=12000 files_rehashed=43`。

### 5.2 流水线 request_blocks

**改造点**:[session.py:92-156](../orcasync/session.py#L92-L156),[protocol.py](../orcasync/protocol.py)。

- header 增加 `request_id`(单调递增 uint64)。
- 发送侧维护 `inflight: dict[request_id, RequestState]`,用 `asyncio.Semaphore(N=16)` 控制并发。
- 接收 `block_data` / `transfer_done` 按 `request_id` 路由到对应 future。
- 去掉每次 `send` 中的强制 `drain()`;改为**飞行字节数超过阈值**(默认 16 MB)才 `await drain()`,实现背压。

**日志**:
- `transfer.inflight count=8 bytes=4MB`(每秒抽样一次)
- `transfer.backpressure waited_ms=42`

### 5.3 流式 manifest 传输

**改造点**:[session.py:55-86](../orcasync/session.py#L55-L86)。

- 新增消息 `manifest_chunk` + `manifest_done`。
- 扫描时分批,每 500 条目一个 chunk 立即发送。
- 接收端边收边构 dict,收到 `manifest_done` 才 set `_manifest_event`,但 diff 可以在 done 前就对已到部分 partial 启动(优化项)。

**日志**:`manifest.send chunks=42 entries=21000 bytes=3.2MB duration_ms=512`。

### 5.4 watcher 优化

**改造点**:[watcher.py](../orcasync/watcher.py)。

- 防抖窗口从 0.5s → 10s(可配置 `--watch-delay-s`)。
- 合并键从 `(event_type, rel_path)` → `rel_path`,事件类型保留**最后到达的**(类型优先级:`delete > modify > create`)。
- `on_moved`:不再拆 delete+create,直接产出 `move` 事件,session 层处理为"按 path 移动";若拆不开就保留为 `modify(dest)`,**不再 delete src**(避免数据丢失窗口)。
- 临时文件名过滤:遇到 `*.tmp`、`*.swp`、`~*` 等编辑器临时文件不立即派发,等它们 rename 成真名后再处理(参考 Syncthing 的 `defaultIgnorePatterns`)。

**日志**:`watcher.fired event=modify path=… debounced=4 age_ms=8200`。

---

## 六、M3 详细方案(P2/P3)

### 6.1 结构性回弹避免(去掉 2s 时间窗)

依赖 5.1 的 manifest db:

- 每次写盘完成后立即更新 db。
- watcher 触发时先比对 db:`(size, mtime, hash)` 与 db 一致 → 没真实变化 → 直接吞掉。
- staging 目录 `.orcasync/staging/` 默认加入 ignore,从源头不会触发回弹。

**结果**:`_synced_files` 字典和 `2.0` 常量彻底删除。日志中 `echo.suppressed reason=fingerprint_match` 出现频率应明显升高,说明这条路径在工作。

### 6.2 Version vector 化的冲突检测(可选)

按 Syncthing 思路:每个文件维护 `{node_id_64: counter}`,本地修改递增本节点 counter,sync 时交换。

- `node_id` 在第一次启动生成(`uuid4().int & ((1<<64)-1)`),持久化到 `.orcasync/node_id`。
- diff 逻辑改为向量比较:`A dominates B` / `concurrent` / `equal`。**只有 concurrent 才是冲突**,不再用 mtime 猜。
- mtime + hostname 退化为 concurrent 时的 tiebreaker。

成本较高(协议、db schema 都要改),仅在多节点场景才必要,**双节点优先做 M1 的 4.4**。

### 6.3 块匹配可选弱 hash(远期)

只在 rsync 风格的"内容部分变化但偏移漂移"场景有意义。orcasync 现在固定块边界,几乎不会遇到这种情况,**优先级最低**,记为远期备选。

---

## 七、协议变更与兼容

新增/变更的消息字段:

| 消息 | 新增字段 | 兼容性 |
|------|----------|--------|
| `init` / `init_ack` | `version: int = 2` | 双方都 ≥2 才启用新特性;否则降级到 v1 |
| `block_data` | `hash`, `request_id`, `retry` | v1 端可忽略,接收端若是 v2 但发来 v1 视为退化模式 |
| `request_blocks` | `request_id` | 同上 |
| `manifest_chunk` / `manifest_done` | 新类型 | v1 端不识别 → 退化为整发 `manifest` |
| `file_event` | `version_vector`(M3) | M3 启用 |

实施时所有新字段都先**可选**,渐进 rollout,避免一次性把客户端、服务端都改坏。

---

## 八、测试与验证

### 8.1 单元测试新增

- `test_staging.py`:写盘中途崩溃(`os._exit` 模拟)→ 重启后 `.partial` 文件清理且 target 未损坏。
- `test_block_verify.py`:故意发坏 payload → 接收端必须重请求 + 计数 + 放弃。
- `test_rescan.py`:伪造 watcher 丢失事件 → 周期 rescan 必须发现差异。
- `test_conflict.py`:双侧并发改同一文件 → 生成 conflict 文件 + 双方都保留。
- `test_manifest_cache.py`:`(size, mtime)` 不变跳过 hash;变化触发重算。

### 8.2 集成测试场景

| 场景 | 验收标准 |
|------|----------|
| 同步 5GB 大文件 | RSS 增长 < 200MB,完成后日志显示 `bytes_transferred ≈ 5GB`,中途断网恢复后能续 |
| 同步 50 万小文件目录 | 冷启动 < 30s(有 cache),首启 < 5min,内存峰值 < 1GB |
| 单侧 `rm -rf` 子目录 | 对端收到 delete 事件 + 日志一一对应;若 inotify 溢出,周期 rescan 在 1 个 interval 内发现 |
| 双侧并发编辑同一文件 | 生成 1 个 conflict 文件,无静默覆盖,WARNING 日志可定位 |
| `kill -9` 后重启 | manifest cache 有效,无需重新全量扫描;staging 残留被清理 |
| NFS / SMB 挂载点 | `--force-poll` 模式可工作(M3 引入) |

### 8.3 日志可消费性验证

- `cat orcasync.log | grep 'conflict.detected'` 能列出所有冲突。
- `cat orcasync.log | jq 'select(.event=="sync.done")'`(json 模式)能算总吞吐。
- 每条 ERROR 日志必须能反向定位到至少一条 INFO/WARNING 上下文。

---

## 九、风险与回滚

| 风险 | 缓解 |
|------|------|
| SQLite 写并发(多线程 scan + 同步) | 单写连接 + WAL 模式;高频写走 batch transaction |
| `.orcasync/` 子目录污染用户根 | 默认 ignore;CLI 提供 `--state-dir` 把它放到 `~/.local/share/orcasync/<sha-of-root>` |
| 协议字段渐进可选时的回归 | 每个 milestone 跑一次"新版 ↔ 旧版"互通测试 |
| 周期 rescan 影响实时同步性能 | rescanner 跑在低优先级 task,锁粒度细化到单文件 |
| 日志吞吐过大 | DEBUG 默认关闭;INFO 已足够日常运维 |

回滚策略:每个 milestone 都是单独 PR + feature flag(`OrcaConfig` 增 `enable_staging`、`enable_manifest_cache` 等),线上有问题可单独关。

---

## 十、实施顺序速查

```
M1 (P0,数据安全 + 可观测)
  └─ 3 统一日志规范           ← 先做,后面所有改动都依赖
     4.1 流式写盘
     4.2 端到端 hash 校验
     4.3 周期 rescan
     4.4 冲突保留(mtime + hash 启发式)

M2 (P1,规模与性能)
  ├─ 5.1 持久化 manifest 缓存  ← M3 的基础
  ├─ 5.2 流水线 request_blocks
  ├─ 5.3 流式 manifest
  └─ 5.4 watcher 优化

M3 (P2/P3,正确性高地)
  ├─ 6.1 结构性回弹消除(依赖 5.1)
  ├─ 6.2 version vector 冲突检测
  └─ 6.3 弱-强双 hash(远期)
```

---

## 十一、参考

- 上游分析:[sync-mechanism.md](sync-mechanism.md)
- 改进方向:[sync-improvements.md](sync-improvements.md)
- 同类对比:[comparison-syncthing-mutagen.md](comparison-syncthing-mutagen.md)

# orcasync Enhancement Design

Date: 2026-05-07

## Requirements

1. **空文件夹同步** — 当前 `scan_directory` 只扫描文件，空目录不会被同步到对端
2. **本地模式** — 两个本地文件夹之间直接同步，无需启动 TCP server/client
3. **Windows 兼容** — 确保实时同步在 Windows 下正常工作

---

## 1. 空文件夹同步

### 问题分析

当前 `scan_directory` 使用 `os.walk`，只遍历文件。空目录（没有子文件/子目录）不会被加入 manifest。

`diff_manifests` 在遇到 `is_dir=True` 的条目时直接跳过，目录从未被同步。

虽然 `_handle_file_event` 中有 `create` + `is_dir` 的处理逻辑，但那只适用于实时同步阶段，初始同步阶段完全不处理目录。

### 设计方案

**manifest 结构扩展：**
目录条目格式：`{"path": "foo/bar", "is_dir": True, "mtime": <dir_mtime>}`
不需要 `size` 和 `blocks` 字段。

**sync_engine.py 改动：**

- `scan_directory`：在 `os.walk` 循环中，对每个 `dirnames` 中的子目录也加入 manifest
- `diff_manifests`：处理目录条目 — 本地缺少该目录时，标记为需要同步
- 新增 `ensure_dir(root, rel_path)`：递归创建目录

**session.py 改动：**

- `_request_needed`：区分文件和目录需求
  - 文件需求 → 发送 `request_blocks`（现有逻辑）
  - 目录需求 → 发送 `mkdir` 消息
- 新增 `_handle_mkdir`：接收 mkdir 消息，调用 `os.makedirs`
- 发送方在收到 `sync_done` 前需要确保目录已创建完毕（目录不需要 block 传输）

---

## 2. 本地模式 (Local Sync)

### 问题分析

当前必须启动 server + client 两个进程，通过 TCP 通信。对于两个本地文件夹，TCP 是多余的。

### 设计方案

**新增子命令：**
```bash
python -m orcasync local-sync --src A --dst B
```

**架构：**
- 复用现有的 `sync_engine`（manifest 计算、diff、block 读写）
- 复用 `watcher.FileWatcher`（两端各一个 watcher）
- 不经过网络，直接在同一个进程中双向同步

**核心逻辑：**

创建 `LocalSyncSession` 类：

```
class LocalSyncSession:
    def __init__(self, src_path, dst_path, loop):
        self.src_path = src_path
        self.dst_path = dst_path
        # 初始同步：计算 src/dst 的 manifest，互相补齐
        # 启动两个 FileWatcher
        # 事件处理：
        #   src 有变化 → 同步到 dst
        #   dst 有变化 → 同步到 src
```

初始同步流程（类似网络版，但跳过 TCP 和序列化）：
1. 扫描 src_manifest 和 dst_manifest
2. 计算 src 需要 pull 的 → 直接从 dst 读取写入 src
3. 计算 dst 需要 pull 的 → 直接从 src 读取写入 dst
4. 启动两个 watcher

实时同步：
- src watcher 触发 → 调用 `_sync_to_dst(path, event_type)`
- dst watcher 触发 → 调用 `_sync_to_src(path, event_type)`

**与网络版代码复用：**
- `sync_engine` 的所有函数直接复用
- 避免重复实现 block 级 diff、读写逻辑
- `LocalSyncSession` 内部直接调用 `read_block` / `write_blocks` / `compute_file_blocks`

---

## 3. Windows 兼容

### 问题分析

用户反馈 Windows 下同步后文件没有实际同步。潜在问题：

1. **路径分隔符**：manifest 中的路径使用 `os.path.join`，在 Windows 上是 `\`。但 `_handle_file_event` 中的 `rel_path` 可能混合使用 `/` 和 `\`。
2. **watchdog 事件路径**：watchdog 在 Windows 上返回的路径可能包含 `\\?\` 前缀（长路径）。
3. **文件锁定**：Windows 下文件在写入时可能被锁定，导致 `write_blocks` 失败。
4. **watchdog observer 类型**：Windows 默认使用 `ReadDirectoryChangesW`，对网络驱动器支持不好。

### 设计方案

**路径标准化：**
- 所有 manifest 中的路径统一使用 `/` 作为分隔符
- `scan_directory` 中：`rel_path.replace(os.sep, "/")`
- `watcher._rel` 中：返回的路径统一替换为 `/`
- `write_blocks` / `read_block` 中：使用时再转回 `os.sep`

**watchdog 路径处理：**
- 在 `_Handler._rel` 中处理 `\\?\` 前缀
- `path = path.replace("\\\\?\\", "")`

**文件锁定处理：**
- `write_blocks` 中添加重试机制（最多 3 次，间隔 100ms）
- Windows 下使用 `os.replace` 原子替换而非直接写入

**CLI 增加 `--use-polling` 选项：**
- Windows 网络驱动器场景下使用 `PollingObserver`
- 默认仍用 `Observer`（基于 `ReadDirectoryChangesW`）

---

## 文件改动清单

### 修改
- `orcasync/sync_engine.py`
  - `scan_directory`：加入目录扫描
  - `diff_manifests`：处理目录条目
  - 新增 `ensure_dir`
- `orcasync/session.py`
  - `_request_needed`：区分文件/目录
  - 新增 `_handle_mkdir`
  - 路径标准化（`/` 分隔符）
- `orcasync/watcher.py`
  - `_rel`：路径标准化，处理 Windows 前缀
- `orcasync/cli.py`
  - 新增 `local-sync` 子命令
  - 新增 `--use-polling` 选项

### 新增
- `orcasync/local_sync.py` — 本地同步核心逻辑
- `tests/test_local_sync.py` — 本地模式测试
- `tests/test_empty_folder.py` — 空文件夹同步测试

### 测试覆盖
1. 空文件夹初始同步（src 有空目录 → dst 也创建）
2. 空文件夹实时同步（新建空目录 → 同步到对端）
3. 本地模式初始同步（双向文件+目录）
4. 本地模式实时同步（新建/修改/删除文件和目录）
5. Windows 路径标准化（`\\` → `/`）

---

## 方案对比

| 维度 | 当前 TCP 模式 | 本地模式 |
|------|--------------|---------|
| 进程数 | 2 (server + client) | 1 |
| 通信方式 | TCP + JSON 序列化 | 直接函数调用 |
| 适用场景 | 跨机器 / 跨网络 | 同一台机器 |
| 性能 | 有网络开销 | 零网络开销 |
| 代码复用 | sync_engine, watcher | sync_engine, watcher |

本地模式是现有架构的自然扩展，不破坏现有 TCP 模式。

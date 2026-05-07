# GitIgnore Support Design

Date: 2026-05-08

## Requirements

1. **递归读取 `.gitignore`** — 像 Git 一样，每个子目录可以有独立的 `.gitignore` 文件，规则只影响该目录及子目录
2. **规则行为一致** — 支持所有 Git 标准规则：通配符 `*`、`**`、否定 `!`、目录后缀 `/` 等
3. **双向同步时各自应用** — 同步两端各自读取并应用自己的 `.gitignore`，`.gitignore` 文件本身正常同步
4. **默认忽略 `.git` 目录** — 像 Git 一样，`.git/` 默认被忽略
5. **CLI 开关** — 提供 `--no-gitignore` 参数禁用此功能（默认启用）
6. **覆盖完整同步生命周期** — 初始扫描和实时监听阶段都要过滤

---

## 方案对比

| 方案 | 额外依赖 | 开发成本 | 正确性 | 性能 | 维护成本 |
|------|----------|----------|--------|------|----------|
| 1. `pathspec` 库 | `pathspec` | 低 | 高 | 高 | 低 |
| 2. 自研 `fnmatch` 解析器 | 无 | 高 | 中 | 高 | 高 |
| 3. 调用 `git check-ignore` | `git` CLI | 低 | 高 | 低 | 低 |

**选择：方案 1（`pathspec`）**

理由：`pathspec` 是 Python 生态中处理 Git ignore 模式的标准选择，成熟可靠，能正确支持所有边缘规则（否定规则、递归通配符等）。自研方案开发复杂度高，容易遗漏边缘情况。调用 Git 命令方案性能极差且依赖外部 Git 安装。

---

## 架构设计

### 1. 新增模块：`orcasync/gitignore.py`

核心类：`GitIgnoreMatcher`

```python
class GitIgnoreMatcher:
    def __init__(self, root_path):
        """
        root_path: 同步目录的绝对路径
        遍历目录，递归读取所有 .gitignore 文件
        """
    
    def is_ignored(self, rel_path, is_dir=False) -> bool:
        """
        判断相对于 root_path 的路径是否被忽略
        
        逻辑：
        1. 找到 rel_path 所在目录的所有祖先目录的 .gitignore 规则
        2. 按从根到子目录的顺序合并规则（先遇到的规则优先级更高）
        3. 调用 pathspec 匹配
        
        注意：
        - pathspec 使用 "gitwildmatch" 模式，支持所有 Git 规则
        - 目录后缀 / 规则只对目录生效，对文件不生效
        """
```

**加载逻辑：**

1. 从 `root_path` 开始，递归遍历所有子目录
2. 在每个目录发现 `.gitignore` 文件时，读取其内容
3. 用 `pathspec.GitIgnoreSpec.from_lines("gitwildmatch", lines)` 解析
4. 将解析结果与父目录的 spec 合并（子目录规则在父目录规则之后添加，Git 的标准行为是后定义的规则优先级更高）
5. 为每个目录缓存一个合并后的 `GitIgnoreSpec`

**默认规则：**

始终在最前面添加 `.git/` 规则，确保 `.git` 目录默认被忽略（除非整个功能被禁用）。

**缓存优化：**

目录结构通常不会频繁变化，初次加载后缓存所有 spec。`.gitignore` 文件修改时不需要实时重新加载（因为 `.gitignore` 文件本身参与同步，修改后会通过正常同步流程传播，两端各自重新扫描目录时会重新加载）。

### 2. `sync_engine.py` 改动 — 扫描阶段过滤

`scan_directory` 函数签名变更：

```python
def scan_directory(root_path, gitignore_matcher=None):
```

**改动细节：**

- `os.walk` 遍历中，对每个目录先检查是否被忽略：
  - 如果被忽略，从 `dirnames` 列表中**删除**该目录名（阻止 `os.walk` 继续深入，与 Git 行为一致）
  - 同时跳过将该目录加入 manifest
- 对每个文件，检查是否被忽略，如果被忽略则跳过，不加入 manifest

**为什么要从 `dirnames` 删除被忽略的目录？**

Git 的忽略规则中，如果一个目录被忽略，那么该目录下的所有内容都被忽略（即使子目录有 `!xxx` 否定规则也不生效）。这与 `os.walk` 的机制匹配：从 `dirnames` 中删除目录名会阻止 Python 继续遍历该目录。

### 3. `watcher.py` 改动 — 实时事件过滤

`FileWatcher` 类变更：

```python
class FileWatcher:
    def __init__(self, root_path, callback, loop, gitignore_matcher=None):
```

**改动细节：**

在事件处理链中插入过滤层：

1. `_on_event` 收到事件时，先调用 `gitignore_matcher.is_ignored(rel_path, is_dir)`
2. 如果被忽略，直接丢弃，不进入 debounce 队列，不触发同步 callback
3. `move` 事件拆分为 `delete + create`，两端分别独立过滤

**特殊处理：**

- `.gitignore` 文件本身的修改事件**不过滤**（因为它本身需要被同步）
- 被忽略目录内部发生的事件也自动被过滤（因为 watcher 的 `_rel` 会计算出被忽略目录内的相对路径，而 `is_ignored` 会匹配该路径）

### 4. CLI 改动

三个子命令（`server`、`client`、`local-sync`）都添加 `--no-gitignore` 参数：

```
python -m orcasync server --host 0.0.0.0 --port 8384
python -m orcasync client --local /path/to/local/dir --remote /path/to/remote/dir --host <server-ip> --port 8384
python -m orcasync local-sync --src A --dst B
```

以上命令默认启用 .gitignore 支持。添加 `--no-gitignore` 后：

```
python -m orcasync client --local ... --remote ... --host ... --port 8384 --no-gitignore
```

禁用 .gitignore 支持，同步所有文件（包括 `.git` 目录）。

**参数传递：**

- CLI 解析出 `--no-gitignore` 标志
- 传递给 `FileWatcher` 和 `scan_directory` 调用点
- 如果启用，为每个 sync 目录创建 `GitIgnoreMatcher` 实例

### 5. TCP 同步模式下的行为

**Server 端：**
- 创建 `GitIgnoreMatcher` 实例（对应 server 端的 sync 目录）
- 初始扫描时过滤 server 端被忽略的文件
- 实时监听时过滤 server 端被忽略的文件事件

**Client 端：**
- 创建 `GitIgnoreMatcher` 实例（对应 client 端的 sync 目录）
- 初始扫描时过滤 client 端被忽略的文件
- 实时监听时过滤 client 端被忽略的文件事件

**两端独立工作**，各自的 `.gitignore` 文件独立生效。`.gitignore` 文件本身正常同步，所以两端规则会自然趋同。

### 6. 本地同步模式下的行为

`local_sync.py`：
- 为 `src` 和 `dst` 各创建一个 `GitIgnoreMatcher` 实例
- 初始扫描：src 用 src 的 matcher 过滤，dst 用 dst 的 matcher 过滤
- 实时监听：src 的 watcher 用 src 的 matcher 过滤，dst 的 watcher 用 dst 的 matcher 过滤

---

## 数据流

### 初始同步阶段

```
scan_directory(root_path, matcher)
  ├── os.walk(root_path)
  │   ├── 对每个目录：matcher.is_ignored(rel_dir, True)
  │   │   ├── 被忽略 → 从 dirnames 删除，跳过
  │   │   └── 未被忽略 → 加入 manifest
  │   └── 对每个文件：matcher.is_ignored(rel_file, False)
  │       ├── 被忽略 → 跳过
  │       └── 未被忽略 → 计算 blocks，加入 manifest
  └── 返回 manifest（不含被忽略的文件和目录）
```

### 实时同步阶段

```
watcher 收到事件 (type, rel_path, is_dir)
  ├── matcher.is_ignored(rel_path, is_dir)
  │   ├── 被忽略 → 丢弃事件
  │   └── 未被忽略 → 进入 debounce 队列 → 触发 sync callback
```

---

## 边界情况处理

### 1. `.gitignore` 文件本身

- `.gitignore` 文件**不被忽略**（即使它在某条规则中被匹配，也强制放行）
- 这样确保 `.gitignore` 修改能正常同步到对端

### 2. 被忽略目录的创建/删除

- 创建被忽略目录：`watcher` 收到 `create` 事件，`is_ignored` 返回 True，直接丢弃
- 这与 Git 行为一致：被忽略的目录不会被跟踪

### 3. 从忽略到不忽略（删除 `.gitignore` 中的规则）

- 用户修改 `.gitignore`，删除某条规则
- `.gitignore` 文件同步到对端
- 两端各自重新扫描时，新规则生效，之前被忽略的文件现在会出现在 manifest 中
- 正常同步流程会处理这些"新发现"的文件

### 4. 从不忽略到忽略（添加 `.gitignore` 规则）

- 用户修改 `.gitignore`，添加新规则
- `.gitignore` 文件同步到对端
- 两端各自重新扫描时，新规则生效
- 已存在的被忽略文件不会自动从对端删除（这与 Git 行为不同，Git 也不会自动删除已跟踪的文件）
- 如果用户希望删除对端文件，需要手动操作

### 5. 空目录与 `.gitignore`

- 如果空目录被 `.gitignore` 匹配（如 `empty_dir/`），则不会被同步
- 这是预期行为：被忽略的内容不应出现在对端

### 6. 嵌套 `.gitignore` 的优先级

遵循 Git 标准优先级：
1. 越靠近文件所在目录的 `.gitignore` 规则优先级越高
2. 同一文件内，后定义的规则优先级更高
3. `pathspec` 库已经正确处理这些优先级

---

## 文件改动清单

### 新增
- `orcasync/gitignore.py` — GitIgnoreMatcher 核心类
- `tests/test_gitignore.py` — 单元测试

### 修改
- `orcasync/sync_engine.py`
  - `scan_directory`：新增 `gitignore_matcher` 参数，集成过滤逻辑
- `orcasync/watcher.py`
  - `FileWatcher.__init__`：新增 `gitignore_matcher` 参数
  - `_on_event` 或事件处理链中插入过滤逻辑
- `orcasync/cli.py`
  - 三个子命令都添加 `--no-gitignore` 参数
- `orcasync/session.py`（TCP 模式）
  - 创建 `GitIgnoreMatcher` 实例并传递给 `scan_directory` 和 `FileWatcher`
- `orcasync/local_sync.py`（本地模式）
  - 为两端各创建 `GitIgnoreMatcher` 实例并传递
- `requirements.txt`
  - 添加 `pathspec>=0.12.0`

### 测试覆盖
1. **基础匹配**：`*`、`**`、否定 `!`、目录后缀 `/`
2. **递归 `.gitignore`**：子目录规则优先于父目录
3. **扫描阶段过滤**：`scan_directory` 正确跳过被忽略文件和目录
4. **监听阶段过滤**：`watcher` 正确丢弃被忽略事件
5. **默认忽略 `.git`**：`.git` 目录默认被忽略
6. **`.gitignore` 文件不过滤**：`.gitignore` 本身正常同步
7. **CLI 参数**：`--no-gitignore` 正确禁用过滤
8. **性能**：大目录下的匹配性能（`pathspec` 使用底层 C 优化，性能已足够）

---

## 兼容性

- **Python 版本**：`pathspec>=0.12.0` 支持 Python 3.8+，与项目要求（3.10+）兼容
- **操作系统**：pathspec 纯 Python，跨平台，无额外依赖
- **向后兼容**：默认启用，添加 `--no-gitignore` 可恢复旧行为

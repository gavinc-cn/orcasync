# orcasync 同步机制分析

## 一、整体架构

orcasync 是一个**双向文件同步工具**,支持两种传输模式:

- **TCP 模式** ([session.py](../orcasync/session.py)):跨网络/进程同步
- **本地模式** ([local_sync.py](../orcasync/local_sync.py)):同进程内两个目录直接同步,不走网络

核心机制围绕三件事:**清单交换** → **块级差异传输** → **实时事件广播**。

---

## 二、核心数据单元:Manifest(清单)

[sync_engine.py:34-85](../orcasync/sync_engine.py#L34-L85) 的 `scan_directory()` 扫描目录,产出一个 dict,key 是相对路径(强制 `/` 分隔符),value 是:

- **目录条目**: `{path, is_dir: True, mtime}`
- **文件条目**: `{path, size, mtime, is_dir: False, blocks: [...]}`
  - `blocks` 是把文件切成 **128KB** 一块,每块算 SHA-256,存 `{index, size, hash}`

所有路径在内部统一为 `/`,落盘 I/O 时再在 [sync_engine.py:121-153](../orcasync/sync_engine.py#L121-L153) 转回 `os.sep`,这是跨平台一致性的关键。

---

## 三、差异计算:diff_manifests

[sync_engine.py:88-118](../orcasync/sync_engine.py#L88-L118) 比对 `local` 与 `remote`,返回**本地需要从远端拉取**的清单:

1. 远端是目录而本地没有 → 需要建目录
2. 远端是文件而本地没有(或本地是目录)→ 需要整个文件(`block_indices: None`)
3. 块哈希完全一致 → 跳过
4. **远端 mtime ≤ 本地 mtime → 跳过**(这是 last-write-wins 冲突策略)
5. 否则,只挑出 hash 不同的块索引列表

注意第 4 条:它只看 mtime 决定是否覆盖,这意味着如果两边都修改了同一文件,**修改时间更晚的一方会胜出**,中间没有合并。

---

## 四、TCP 同步流程

### 初始同步(三次交互)

```
Client                                Server
  │                                     │
  ├─ init(remote_path) ────────────────►│
  │◄──────────────────── init_ack ──────┤
  │                                     │
  ├─ manifest(local files) ────────────►│   (server scan_directory)
  │◄────────────────── manifest(files) ─┤
  │                                     │
  │ diff_manifests()                    │ diff_manifests()
  ├─ request_blocks(path, indices) ────►│
  │◄────────────── block_data ──────────┤   (一块一条消息)
  │◄────────────── block_data ──────────┤
  │◄──────────── transfer_done ─────────┤
  ├─ sync_done ────────────────────────►│
  │◄──────────────────── sync_done ─────┤
  │                                     │
  ├── 启动 FileWatcher,进入实时模式 ───►│
```

两端**同时**进行 diff 并互发 `request_blocks`,所以是真双向。客户端先发清单,服务端拿到后才扫描自己的目录并回送清单([session.py:72-86](../orcasync/session.py#L72-L86)),避免服务端做无用扫描。

### 线协议

[protocol.py](../orcasync/protocol.py) 非常简洁:`[4 字节大端长度][JSON header][raw payload]`。header 里带 `type` 和 `payload_len`,payload 用于 `block_data` 的二进制块内容,其它消息 payload 为空。

---

## 五、实时同步阶段

初始同步完成后,两端各启一个 `FileWatcher`([watcher.py](../orcasync/watcher.py)):

1. **watchdog** 触发原始事件
2. **0.5 秒防抖**([watcher.py:30-42](../orcasync/watcher.py#L30-L42)):同一个 `(event_type, rel_path)` 在窗口内的多次事件被合并
3. 用 `call_soon_threadsafe` 把回调从 watchdog 线程切到 asyncio 事件循环
4. 回调过 gitignore 过滤后,推给 `_on_file_change`

变更通过 `file_event` 消息广播([session.py:269-294](../orcasync/session.py#L269-L294)):

- 删除/建目录:只发事件,不带数据
- 文件创建/修改:**只发块哈希列表**(不发数据)

对端收到([session.py:198-248](../orcasync/session.py#L198-L248)):

- 比对本地块哈希,**只 `request_blocks` 真正变化的块** → 这就是 delta 同步的核心价值
- 如果哈希全一致(可能只是 truncate),只调整文件大小
- mtime 反向更新时跳过(再次 last-write-wins)

---

## 六、回声避免(Echo Avoidance)

[session.py:43](../orcasync/session.py#L43) 维护 `_synced_files: {path: timestamp}`。每次**远端推过来的变更**应用到本地后会触发 watchdog 事件,如果不处理就会反弹回去无限循环。

策略:在 `_handle_block_data` / `_handle_file_event` / `_handle_transfer_done` 写盘前,先把路径加入 `_synced_files`;watcher 回调时检查,**2 秒**内的本地事件直接吞掉([session.py:258-262](../orcasync/session.py#L258-L262))。

这个时间窗口是个隐患:慢盘或大文件写盘超过 2 秒就可能误判;反过来,如果用户在 2 秒内主动改同一个文件,这次修改会被吞掉。

---

## 七、本地模式

[local_sync.py](../orcasync/local_sync.py) 去掉所有协议层,直接调 `sync_engine` 的函数,但保留同样的语义:

1. 双向 `diff_manifests` 各跑一次
2. 启两个 `FileWatcher`,一个监听 src,一个监听 dst
3. 一把 `asyncio.Lock` 串行化两个方向的事件,避免并发写竞争([local_sync.py:101-107](../orcasync/local_sync.py#L101-L107))
4. 同样的 2 秒回声窗口

但 **`_handle_change` 实时阶段是整文件复制**([local_sync.py:121-127](../orcasync/local_sync.py#L121-L127)),不像 TCP 模式那样走块级 delta —— 因为同进程下没有传输带宽压力,read+write 整文件更简单。

---

## 八、值得注意的设计点与潜在问题

### 优点

- **块级 delta**:大文件改动一小段时,只重传变化的 128KB 块,这是 rsync 风格的优化
- **协议极简**:JSON header + 二进制 payload,易扩展
- **路径规范化彻底**:内部全是 `/`,只在 I/O 边界换回 `os.sep`,跨 Win/Linux 友好
- **gitignore/syncignore 自动识别**:`.syncignore` 优先,否则递归 `.gitignore`,目录在 `os.walk` 时就被剪枝([sync_engine.py:45-52](../orcasync/sync_engine.py#L45-L52))

### 值得警惕的地方

1. **冲突解决=mtime 单调比较**([sync_engine.py:102](../orcasync/sync_engine.py#L102)):两端同时编辑同一文件,后改的覆盖先改的,没有合并或冲突提示。如果两机时钟漂移,谁覆盖谁会出乎意料。

2. **回声窗口是固定 2 秒**:既不够稳健也不够灵活。慢盘大文件、网络延迟、用户快速重复编辑都会踩坑。

3. **`request_blocks` 一块一条消息,每条 await drain**([session.py:151-156](../orcasync/session.py#L151-L156)):对小块多文件场景,RTT × 块数 会成为瓶颈,没有 pipeline/batch。

4. **`_handle_block_data` 把数据全部缓存到内存里**(`_pending_blocks[path]`,[session.py:160-165](../orcasync/session.py#L160-L165)),`transfer_done` 才落盘。同步**几个 GB 的大文件会撑爆内存**。

5. **初始同步是阻塞串行**:两端各自把所有 needs 全 `request_blocks` 出去再 `await self._sync_event.wait()`。大目录冷启动时,内存压力会随文件数线性涨。

6. **没有校验和重试**:`block_data` 收到后直接信任 payload,不重新计算 hash 验证。底层 TCP 虽有 checksum,但应用层做一次会更稳。

7. **watcher 防抖 0.5 秒**:连续写入的临时文件(编辑器原子保存)很可能被分成多个事件多次同步,而不是合并成一次终态。

---

## 九、总结

orcasync ≈ 一个**用 asyncio 实现的双向 rsync + watchdog 实时通知**:清单交换确定差异,128KB 块哈希作为去重单位,mtime 比较解冲突,2 秒窗口防回声。架构很清晰,但在大文件、强冲突、高并发场景下还有比较明显的优化空间。

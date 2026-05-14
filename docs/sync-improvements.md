# orcasync 改进方案

承接 [sync-mechanism.md](sync-mechanism.md) 中提出的"值得警惕的地方",本文逐条给出根因与推荐解法,并单独讨论事件监测的可靠性与扫描机制。

---

## 一、冲突解决:mtime 单调比较

**根因**:`diff_manifests` 仅靠 `remote.mtime > local.mtime` 决定方向 ([sync_engine.py:102](../orcasync/sync_engine.py#L102)),受时钟漂移、`touch`、`git checkout` 影响。

**推荐做法**(从轻到重):

1. **加 NTP 校时 + tolerance**:接受 2~3 秒时钟差,差距过大时拒绝同步并报警。
2. **引入 version vector / Lamport clock**:每个文件维护 `{node_id: counter}`,真正能检测出"两边并发修改"的冲突,而不是用墙钟近似。Syncthing 用的就是 vector clock。
3. **冲突保留策略**:检测到冲突时不覆盖,把落败方重命名为 `xxx.sync-conflict-20260514-<host>.ext`(Syncthing 的做法),让用户决定。
4. **简单兜底**:在 `diff_manifests` 里加一条:**`mtime` 相近(<5s)但 hash 不同 → 标记为冲突,默认不覆盖,日志告警**。这是最小改动方案。

---

## 二、回声窗口固定 2 秒

**根因**:[session.py:258-262](../orcasync/session.py#L258-L262) 用 `time.time() - synced < 2.0` 兜底,本质是"用时间猜事件来源"。

**推荐做法**:

- **把"时间"换成"事件指纹"**:写盘前记下 `(path, expected_size, expected_blocks_hash)`,watcher 触发时算当前文件指纹,与 expected 一致就吞掉。这是**确定性**判断,不依赖时间窗。
- **若一定要保留时间窗**,至少:
  - 按文件大小动态调:`max(2s, size / write_speed * 2)`
  - 写盘完成后再打时间戳,而不是写盘前
- **更彻底**:在 watcher 层支持"暂停某路径的事件"。`_handle_block_data` 开始时调用 `watcher.pause(path)`,`write_blocks` 完成后再 `resume`。事件还是会触发,但被 pause 标记吞掉。

---

## 三、`request_blocks` 单块单消息 + 每条 drain

**根因**:[session.py:151-156](../orcasync/session.py#L151-L156) 每个 `block` 一条 `send`,而 `send` 里 `await writer.drain()` ([protocol.py:17](../orcasync/protocol.py#L17))。每块都是一次 RTT-sensitive 的握手。

**推荐做法**:

1. **批量打包**:把同一文件的多个块拼到一个 `block_data` 消息里,header 描述 `[{index, offset, len}, ...]`,payload 是拼接的二进制流。一次 `drain` 把整批冲出去。
2. **限流的并发管道**:用 `asyncio.Semaphore(N=8)` 控制飞行中的 `request_blocks` 数量,而不是发一个等一个。
3. **去掉每条 drain**:`drain()` 只在缓冲区高水位才真阻塞。可以**只在批次末尾 drain**,中间用 `writer.write()` 直接写。
4. **小文件特例**:< 一块的文件直接随 manifest 内联传输(像 git pack-objects 那样把"小对象"内联),省一轮 round-trip。

---

## 四、`_pending_blocks` 全内存缓存

**根因**:[session.py:160-165](../orcasync/session.py#L160-L165) 把整个文件的所有块在内存里攒齐,`transfer_done` 才一次性 `write_blocks` 落盘。一个 5GB 文件 = 5GB RSS。

**推荐做法**:

- **流式写盘**:`_handle_block_data` 收到一块就立刻 seek+write 到目标文件(用 `.partial` 后缀避免读到半成品),`transfer_done` 时 `rename` 原子替换 + 截断到 `expected_size`。
- 配合 `os.posix_fallocate` 在第一块到达时预分配空间,避免文件碎片。
- 仍需要在内存里保留"哪些块已到/未到"的 bitmap,但占用是 `O(块数)` 而不是 `O(文件大小)`。

---

## 五、初始同步阻塞串行 + needs 全量在内存

**根因**:[session.py:92-110](../orcasync/session.py#L92-L110) 把全部 needs 一次性算出来并全发 `request_blocks`,然后 `_sync_event.wait()` 等所有结束。

**推荐做法**:

1. **流式 manifest**:不要 `data={"files": local_manifest}` 一次发完,而是分批 chunked,边扫描边发。大目录(数十万文件)的 JSON 序列化也会成为瓶颈,可以换成 length-prefixed 多条 `manifest_chunk`。
2. **流式 diff**:对端边收 chunk 边 diff 边 request,而不是等收完。
3. **限流的请求队列**:`asyncio.Queue(maxsize=K)`,生产侧(diff)推 need,消费侧(N 个 worker)发 `request_blocks` 并等待回应。控制飞行中的字节数。
4. **断点续传**:每写完一个文件就持久化一条"已同步到此"的标记(SQLite/JSONL),崩溃重启不必从头扫。

---

## 六、没有校验和重试

**根因**:`_handle_block_data` 直接信任 payload,既不校验 hash 也不重传。TCP checksum 是 16 位且只在链路层,**经过 NAT/代理/SSH 隧道时不能保证端到端**。

**推荐做法**:

- `block_data` header 带上 `hash`,接收端写盘前比对,不匹配则 `request_blocks` 重传这一块(同时计数,3 次失败就中止会话并告警)。
- 整文件写完后,根据 `expected_size` 和 manifest 中的 `blocks` 列表,再做一次"全文件 hash 校验"。开销低、可靠性提升明显。
- 可选:`HMAC(shared_key, block)`,防中间人篡改(目前协议明文,不抗 MITM)。

---

## 七、watcher 防抖 0.5 秒

**根因**:[watcher.py:30-42](../orcasync/watcher.py#L30-L42) 固定 0.5s 合并事件;编辑器"写临时文件→fsync→rename"模式会被切成多个独立事件。

**推荐做法**:

- **改为"静默期"语义**:每次新事件都把定时器重置(目前是 `cancel + 重新 call_later`,逻辑已经是这样,但 0.5s 太短),把窗口拉到 **1~2 秒静默无新事件才触发**。
- **合并 create+modify**:文件刚 create 紧接着 modify,只发一个 `modify` 即可。目前是按 `(event_type, rel_path)` 做 key,所以 create 和 modify 是两条独立计时器 ([watcher.py:30](../orcasync/watcher.py#L30)),应改成按 `rel_path` 聚合,事件类型保留最终态。
- **处理 rename/atomic-save**:Vim/IDE 的保存通常是 `write tmpfile → rename tmpfile target`,watchdog 触发 `on_moved` 已经在 [watcher.py:70-72](../orcasync/watcher.py#L70-L72) 拆成 delete+create,但应该合并成"目标文件 modify",避免对端先删后建造成数据丢失窗口。
- 文件被打开写入还没关闭时不要 sync(可用 `lsof` 或尝试拿独占锁判断,跨平台略麻烦,但能避免传半成品)。

---

## 八、事件监测够吗?要不要加扫描?

**结论:必须加周期性扫描,作为事件机制的可靠性兜底。** 这是几乎所有成熟同步工具(Syncthing、Dropbox、rsync daemon)的共识。

### 事件机制会漏的常见场景

| 场景 | 原因 |
|------|------|
| **inotify 队列溢出** | Linux 默认 `fs.inotify.max_queued_events=16384`,`rm -rf` / `git checkout` 大目录瞬间能打爆 |
| **inotify watch 上限** | `max_user_watches` 默认 8192~524288,大目录无法全部 watch,**watchdog 不会报错,只是悄悄漏事件** |
| **网络盘 / 虚拟文件系统** | NFS、SMB、SSHFS、`/proc`、容器 bind mount **完全不发 inotify** |
| **Windows ReadDirectoryChangesW 缓冲区溢出** | 默认 64KB,burst 写入会丢事件,且只通知"有事件丢了",不告诉是哪些 |
| **macOS FSEvents 粗粒度** | 默认只通知到目录级,文件粒度需要打开 `kFSEventStreamCreateFlagFileEvents` |
| **进程暂停期** | 进程被 OOM/挂起/SIGSTOP/笔记本休眠期间发生的变更全部丢失 |
| **观察启动前的变更** | watcher 启动到初始扫描完成之间的变更可能落在"扫描之后、watch 之前"的真空 |
| **符号链接、硬链接** | watchdog 不跟随,目标的变化不触发事件 |
| **时间不可靠场景** | 用户手动 `touch -d "yesterday"` 改 mtime;`git checkout` 把 mtime 改到过去;FAT32 只有 2 秒精度;不同 OS 的 mtime 分辨率不同(Linux ns,Windows 100ns,FAT 2s) |

### 推荐扫描策略(分层)

1. **完整 rescan(全量)**
   - 触发:启动时(已有)、watcher 重启后、定时(默认 1 小时,可配置)、收到对端 `rescan_request`
   - 跟初始同步同一套 `scan_directory + diff_manifests`,但只补差异
   - 单独的低优先级线程/任务,不阻塞实时 watch

2. **增量 rescan(中频)**
   - 每 5~10 分钟,只 `os.stat` 不算 hash;发现 `(size, mtime)` 与 manifest 不符再算 hash
   - 成本极低,能兜住绝大多数漏事件

3. **inotify 溢出检测**
   - watchdog 的 `Observer` 在 Linux 下可以监听 `IN_Q_OVERFLOW`;捕获到后**立即触发一次全量 rescan**
   - Windows 上 `ReadDirectoryChangesW` 返回 0 字节就是溢出信号

4. **持久化 manifest 缓存**
   - 把上次扫描结果存到 `.orcasync/manifest.db`(SQLite 或 msgpack),增量 rescan 时只对 `mtime/size` 变化的文件重算 hash → 大目录冷启动从分钟级降到秒级

5. **校验性 rescan(低频)**
   - 每天一次,**完整重算所有块 hash**,对抗静默数据损坏(bit rot)。可以和 `2.` 共享代码,只是不信任 mtime,强制 rehash

### 与事件机制的关系

- 事件机制 = **低延迟**(亚秒级)、不可靠
- 扫描机制 = **可靠**、延迟高(分钟~小时)
- 两者**互补**,不是替代:事件触发实时同步,扫描每隔一段确保最终一致

---

## 九、按"修复价值/工作量"排序的实施建议

| 优先级 | 改动 | 价值 | 工作量 |
|--------|------|------|--------|
| P0 | 块级 hash 校验 + 重试 | 数据正确性,刚需 | 半天 |
| P0 | 流式写盘(去掉 `_pending_blocks` 内存缓存) | 解掉大文件 OOM | 半天 |
| P0 | 周期性 stat-based rescan + inotify 溢出检测 | 解掉漏事件 | 1~2 天 |
| P1 | 冲突保留策略(`.sync-conflict-*`) | 防数据丢失 | 半天 |
| P1 | 流式 manifest + 限流并发 request | 大目录冷启动 | 1~2 天 |
| P1 | 持久化 manifest 缓存 | 重启加速 | 1 天 |
| P2 | watcher 静默期 + 按路径聚合事件 | 减少抖动 | 半天 |
| P2 | 指纹替代时间窗回声避免 | 提升健壮性 | 半天 |
| P3 | version vector 真冲突检测 | 远期正确性 | 1 周+ |

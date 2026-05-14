# Syncthing 与 Mutagen 对照参考

本文针对 [sync-improvements.md](sync-improvements.md) 提出的每一类问题,梳理 [Syncthing](https://github.com/syncthing/syncthing) 和 [Mutagen](https://github.com/mutagen-io/mutagen) 是怎么解决的,作为 orcasync 改进方向的参考。

> 调研基于:Syncthing 公开 BEP v1 规范、`syncing.html`、`lib/scanner` 源码,以及社区 wiki/forum;Mutagen 公开 `documentation/synchronization/*` 与 `pkg/synchronization/{rsync,core}` 源码。

---

## 一、冲突解决

**orcasync 现状**:`remote.mtime > local.mtime` 单调比较,丢冲突侧的修改。

### Syncthing

- 用 **version vector**:每个文件维护 `{device_id_short → counter}` 映射(`device_id_short` 是设备 ID 前 64 bit)。每次本地修改递增本设备的 counter。
- 真正的并发检测来自向量比较:`A dominates B` / `B dominates A` / `concurrent`。**只有 concurrent 才算冲突**,而不是用 mtime 猜。
- 检测到冲突后:**保留两份**,落败方重命名为 `<file>.sync-conflict-<date>-<time>-<modifiedBy>.<ext>`,作为普通文件继续在集群里传播(其他节点也会收到这个 conflict 文件,避免每台机器各自重做一遍冲突判断)。
- 落败方的选择依据(仅作 tiebreaker):
  1. version vector 比较(若不是 concurrent,直接采用 dominant 方)
  2. mtime 较旧者落败
  3. mtime 相等时,设备 ID 数值较大者落败(纯仲裁,无歧义)

### Mutagen

- 不用 version vector,**用 ancestor snapshot 三方合并**:每次同步会话维护"上次双方达成一致的内容"作为 ancestor。
- 一次 reconcile 周期:`scan(alpha) + scan(beta) + ancestor → diff_alpha = ancestor▷alpha, diff_beta = ancestor▷beta`,然后看两个 diff 是否冲突。
- 冲突由 `reconcile.go` 的 `handleDisagreement*` 一族函数处理,按用户选择的模式走不同策略:
  - `two-way-safe`(默认):**只在不丢数据时自动解决**;否则记录冲突,**双方都保留原状**,等用户手动处理(删掉一边)。
  - `two-way-resolved`:alpha 永远胜。
  - `one-way-safe`:alpha→beta,beta 端冲突修改被保留并报告。
  - `one-way-replica`:beta 变成 alpha 的精确镜像。

### 对 orcasync 的启示

- **完全不依赖 mtime 做冲突判定**,是两家共识。
- 最小成本路径:学 Mutagen 的 ancestor snapshot(orcasync 本来就要做持久化 manifest,正好可以当 ancestor)→ 三方合并 → 冲突时保留双方,落败方用 Syncthing 风格的 `.sync-conflict-*` 命名。
- Version vector 更精确,但需要为每个设备分配稳定 ID,改造量大;snapshot 模式对双节点场景已经够用。

---

## 二、回声避免(防止刚写完的文件触发本地事件回弹)

**orcasync 现状**:`_synced_files` 记录路径+时间戳,2 秒窗口内吞掉本地事件。

### Syncthing

- **没有显式时间窗**。它的回弹防护是结构性的:
  - watcher 触发后只更新本地 index(version vector + block hashes),不会自动反推回网络。
  - 真正的"应不应该传播"由 index 比较决定:刚写入的文件 version vector 跟对端 dominant 状态一致,diff 出来是空集,**自然不会再发**。
- 临时文件 `.syncthing.*.tmp` 在写入期间不会被扫描器纳入索引(扩展名匹配过滤),写完 rename 后才作为正式条目出现。

### Mutagen

- **同样没有时间窗**。三方合并模型天然防回弹:apply 完成后 reconciler 立刻更新 ancestor snapshot,下一轮 scan 时 `alpha == ancestor && beta == ancestor`,diff 为空。
- staging 文件放在 sync 根之外的独立目录(`~/.mutagen/.../staging-*`),scan 完全看不到中间态。

### 对 orcasync 的启示

- 当前 2 秒窗口是**因为缺乏稳定 index/snapshot 所做的近似**。补上"持久化 manifest 作为 ancestor"之后,echo 避免就从"时间猜"升级为"diff 自然为空",窗口可以删掉。
- 临时文件也应该改成隐藏前缀 + 写完 rename,而不是直接往目标路径覆盖。

---

## 三、`request_blocks` 单块单消息 + 每条 drain

**orcasync 现状**:每块一条消息一次 drain,RTT × 块数 成本。

### Syncthing

- 协议层(BEP)允许 Request 流水线:**"多个 outstanding Request 可同时在途,响应顺序与请求顺序无关"**。每个 Request 带 `id`,Response 用同样的 `id` 回。
- 实现层:**Puller** 是默认 16 个 goroutine 的池,每个 puller 同时拉一个块,通过 `puller max pending kib` 控制飞行中的总字节数(背压)。
- 还有 **Copier**:发现远端某块的 hash 在本地其它文件已存在(典型场景:rename、复制),**直接本地拷贝,根本不走网络**。block-level 去重。

### Mutagen

- rsync 引擎用 **streaming Transmission**:发送侧把 operation(literal data 或 block reference)逐个 push 到 stream,接收侧逐个 apply,**没有"每块一次 round-trip"的握手**。
- `Transmission` 对象通过 `resetToZeroMaintainingCapacity()` 复用,避免每个块重新分配内存。
- 由于是 rsync 算法,大量重复区域会变成 **block reference**(几十字节),只有真正新增的内容才以 literal data 形式传输,**带宽用量本身就更低**。

### 对 orcasync 的启示

- **流水线**(Syncthing 风格)和**单连接流式 stream**(Mutagen 风格)是两条不同路径,但都消灭了"一块一 RTT"。
- 对 orcasync,简单的改造是 Syncthing 风格:加 `request_id`,允许多个 Request 在途,接收端按 `request_id` 路由。
- 块级去重(Syncthing Copier)对有大量 rename/复制操作的场景收益巨大,值得借鉴。

---

## 四、`_pending_blocks` 全内存缓存

**orcasync 现状**:整文件块攒齐才写盘,大文件爆内存。

### Syncthing

- **从不直接写目标文件**。所有块写入 `.syncthing.<filename>.tmp`(Windows: `~syncthing~<filename>.tmp`),收到一块就 verify SHA-256 + 立即 write,完成后 atomic rename 替换。
- 内存中只存"哪些块还没到"的位图,而非块数据本身。
- 块校验失败:**丢弃这块,从其它源(其它对端设备)重新请求**;一个块可以从多个 peer 拿,是分布式天然冗余。

### Mutagen

- **Stage → Transition 两阶段**:
  1. Staging:在 `~/.mutagen/.../staging-<session>/` 下逐块构建完整文件(可能是 rsync 应用之后的产物)。
  2. Transition:`os.Rename` 原子替换目标(同设备)或临时文件 + rename(跨设备)。
- staging 文件按 SHA-1 命名内容寻址,**多个目标路径如果内容相同,共享同一个 staged 文件**(隐式去重)。
- 中途崩溃:staging 目录可以清理重来,不污染目标。

### 对 orcasync 的启示

- 流式写盘 + 原子 rename 是行业标准,**两家都这么做**,orcasync 这块应优先改造。
- 顺手把 staging 与目标分离(orcasync 可以放 `.orcasync/staging/`),scan 时跳过这个目录,完美避开"回弹避免"问题。

---

## 五、初始同步阻塞串行 + needs 全量在内存

**orcasync 现状**:全部 needs 一次性算完再请求,大目录冷启动内存压力大。

### Syncthing

- **Index 分片传输**:大目录的 ClusterConfig + Index 都可以拆成多条 IndexUpdate 消息,边扫描边发,边收边 diff。
- **持久化数据库**(BoltDB → 现 LevelDB-like):每个 folder 的文件列表、version vector、block list 都持久化。**重启不重扫,只增量更新**,大目录冷启动从分钟级降到秒级。
- Hasher 池并发计算 SHA-256,CPU 多核场景显著加速。

### Mutagen

- **scan 是分代的**:scan 结果是 `Entry` 树,叶子节点的内容签名(`Digest`)只在文件 `(size, mtime)` 与上次相同时复用 cache,**不重新读文件**。
- staging 路径用 chunked stream,接收端边收边写,没有"先攒所有再开始"的瓶颈。

### 对 orcasync 的启示

- 持久化 manifest 缓存(`.orcasync/manifest.db`)是两家都做的优化,**收益最大,代价最低**。
- 把 manifest 序列化改成 chunked / streaming(可以用 length-prefixed msgpack 流),长远比 JSON 一发到底好得多。

---

## 六、没有校验和重试

**orcasync 现状**:`block_data` 收到直接信任,无端到端 hash 校验。

### Syncthing

- **每块必校验**:Request 里就携带 `hash`,Response 到达后接收端用 SHA-256 重新算并对比。**不匹配则丢弃 + 从其它源重请求**。
- 数据库里的每个文件条目都带完整 block hash 列表,scan 时 `(size, mtime)` 不变可复用缓存的 hash,变了才重算。
- BEP 还支持 `weak_hash`(可选 Adler-32 类),做"内容指纹"补足 mtime 不可靠场景。

### Mutagen

- rsync 引擎的天然机制:接收端按 weak hash 匹配滑动窗口,**匹配后必须用 SHA-1 强 hash 二次确认**,匹配失败就把那段当成 literal data 重传。整个传输过程内嵌校验。
- 完成后 Transition 阶段还会再次比对 staged 文件签名,确认 staging 没出错。

### 对 orcasync 的启示

- **校验是必做项,不是优化项**。Syncthing 的"每块带 hash + 失败重传"是最小改造路径,orcasync 直接照抄即可。
- 如果以后引入 rsync 风格(为支持小改动大文件),也要保留强 hash 二次确认。

---

## 七、watcher 防抖 0.5 秒

**orcasync 现状**:0.5 秒固定窗口,合并键是 `(event_type, path)`。

### Syncthing

- 默认防抖 **10 秒**(`fsWatcherDelayS`,可调),删除事件再延长 **1 分钟**。理由:典型编辑器原子保存的"写 tmp → fsync → rename"在 10 秒内能全部稳定下来。
- 防抖窗口按路径聚合,而非按 `(event_type, path)`,**最后一个事件的类型决定最终动作**。
- 完整 fs 事件流跑完后,内部会再做一次轻量 scan 确认,**事件 + 扫描双保险**。

### Mutagen

- 默认 polling **10 秒**;native watch 模式下事件聚合也以 10 秒为基准。
- 由于走 scan-and-reconcile 模型,**watcher 只是触发器,真正比较的还是 scan 结果**,事件抖动天然被吸收。

### 对 orcasync 的启示

- 0.5 秒太短,**10 秒是行业默认**。改一下常量收益立竿见影。
- 合并键改成 path-only,事件类型保留终态。
- 长远把"事件触发"和"实际比较"解耦:事件只是"喊一声该看看了",真正的 diff 还是过 scan/manifest,这样事件丢/重复/乱序都无所谓。

---

## 八、扫描机制兜底事件不可靠

**orcasync 现状**:只有事件,无定期扫描。

### Syncthing

- **强制周期性全量扫描**,默认 **3600s(1 小时)**,实际间隔随机化到 3/4~5/4 倍区间,避免多节点同步触发雷暴。
- 文档原文:**"即使启用 watcher 也建议保留定期 full scan,因为可能有事件没被捕捉到"**。
- watcher 与 scan 共享 index 数据库,扫描发现的新条目和 watcher 触发的条目走同一套 puller。
- inotify 队列溢出在 Linux 上由 watchdog/fsnotify 层捕获,触发立即 rescan。

### Mutagen

- **每次同步周期都做完整 scan**,polling 间隔默认 **10 秒**:即使 watch 失败,最长 10 秒内一定能发现变化。
- Linux 上策略最复杂:**polling + 对最近活跃文件做受限 native watch**。原因是 inotify 的 watch 数有上限(`fs.inotify.max_user_watches`),Mutagen 选择"不全 watch,只 watch 热点"以避免耗尽资源,polling 作为兜底。
- macOS/Windows 用 native recursive watch,**额外加一个 cheap polling** 探测 sync root 是否被删/重建(单层 root 检查,几乎零开销)。
- 三种模式可选:`portable`(默认,智能选)、`force-poll`(强制 polling)、`no-watch`(完全手动,需要 `mutagen sync flush`)。

### 对 orcasync 的启示

- **两家在"事件必须有 scan 兜底"上完全一致**。Syncthing 偏"事件优先,scan 兜底",Mutagen 直接是"scan 优先,watch 加速"。
- 对 orcasync,Syncthing 模式更接近现有架构,改造路径:
  1. 加 1 小时全量 rescan 定时任务(可配置)。
  2. 加 5~10 分钟增量 stat-only rescan(只看 size/mtime 是否变,变了再算 hash)。
  3. 检测 inotify 溢出并触发立即 rescan。
- 如果将来要支持 NFS/SMB 等不发 inotify 的场景,可参考 Mutagen 的 `force-poll` 模式,让用户能显式禁用 watch。

---

## 九、对照汇总

| 维度 | orcasync | Syncthing | Mutagen |
|------|----------|-----------|---------|
| 冲突检测依据 | mtime | version vector | ancestor snapshot 三方合并 |
| 冲突动作 | 覆盖 | 保留 + `.sync-conflict-*` 重命名 + 集群传播 | 看模式;默认 two-way-safe 双方保留并上报 |
| 回弹避免 | 2s 时间窗 | 结构性(index diff 自然为空) | 结构性(snapshot diff 自然为空) |
| 块大小 | 固定 128 KB | 128 KB ~ 16 MB,按文件大小自适应(<2000 块) | 默认 8 KB;最优 sqrt(24·N),1KB~64KB |
| 块匹配 | 仅 SHA-256 | SHA-256(可选 weak hash) | weak rolling + SHA-1 强 hash |
| 传输模型 | 一块一消息一 drain | 流水线 + 16 puller 并发 + Copier 本地块去重 | streaming Transmission,逐 op 流式 |
| 接收落盘 | 内存攒齐 → 一次写 | `.syncthing.*.tmp` 逐块流式写 → atomic rename | staging 目录构建 → rename(跨设备退化为 temp+rename) |
| 端到端校验 | 无 | 每块 SHA-256 必校验,失败丢弃 + 重请求 | weak match 后强 hash 二次确认;Transition 前再校验 |
| 持久化 index | 无 | LevelDB-like 数据库 | 内嵌 entry tree + digest cache |
| watcher 防抖 | 0.5s | 10s(可调),delete +60s | 10s polling-based |
| 周期 rescan | 无 | 默认 1h,随机 3/4~5/4 | 每周期(默认 10s) |
| 不支持 watch 的兜底 | 无 | rescan | poll 模式 / no-watch + flush |

---

## 十、参考链接

- Syncthing
  - [BEP v1 specification](https://docs.syncthing.net/specs/bep-v1.html)
  - [Understanding Synchronization](https://docs.syncthing.net/users/syncing.html)
  - [Block scanner source](https://github.com/syncthing/syncthing/blob/main/lib/scanner/blocks.go)
  - Forum: [copier / puller / hasher 差异](https://forum.syncthing.net/t/what-is-the-difference-between-copier-puller-and-hasher/2627)
- Mutagen
  - [Synchronization overview](https://mutagen.io/documentation/synchronization/)
  - [Watching mechanism](https://mutagen.io/documentation/synchronization/watching)
  - [rsync engine source](https://github.com/mutagen-io/mutagen/blob/master/pkg/synchronization/rsync/engine.go)
  - [Reconciliation source](https://github.com/mutagen-io/mutagen/blob/master/pkg/synchronization/core/reconcile.go)
  - [Transition / atomic apply source](https://github.com/mutagen-io/mutagen/blob/master/pkg/synchronization/core/transition.go)

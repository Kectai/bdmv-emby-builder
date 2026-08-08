# Beta 验证记录

适用版本：`0.4.0b1`

本文只记录项目级验证结论，不附带从真实媒体库导出的计划、结果、路径或逐盘映射。可重复验证的最小结构由单元测试使用匿名合成数据覆盖。

## 自动化回归

执行：

```bash
python3 -m unittest discover -s tests -v
```

当前共 156 项测试，覆盖：

- MPLS 长度、区段、PlayItem、Mark、连接条件、多角度、SubPath 和章节边界；
- movie、series、bonus 规划，普通及带短边缘版权卡的非无缝单 PlayItem 分集、多 PlayItem 分集、单 M2TS 多集、CLI/逐盘季号优先级、默认季号复核字段、集号及 edition 隔离；
- 菜单循环、短交互导航、重复媒体和缺失 M2TS 的显式审计；
- 低置信度短 extras 的全部音轨聚合、无音轨、正常音频短路、静态抽样证据和只提示不排除行为；
- planner/builder 共用的完整分集证明、伪造时长提示/孤立 concat 拒绝及异常大边界数量保护；
- UTF-8/UTF-16 文件名限制、标准 BDMV/PLAYLIST/STREAM 关系、目标路径逃逸和 Windows ffconcat/进程检查；
- `copy_remux`、`hardlink_remux`、`hardlink_only` 的操作解析、整盘阻断和现有文件身份约束；
- 剩余空间预检、目标锁、随机 partial、原子替换和 Ctrl-C 审计恢复；
- 审计产物与源/媒体/内部路径冲突、符号链接、Windows junction/reparse point、FIFO 等特殊文件、控制文件及工作目录越界防护；
- FFprobe 时长、轨道、PID、TrueHD/AC-3、视频/音频/字幕包存在性、DTS 连续性及音视频轨道首尾覆盖校验；
- 状态 schema 迁移、完整内容哈希、计划指纹、人工搬移、内容变化和 CLI 非零退出码。

测试只在系统临时目录或项目指定的 `.test-tmp` 中创建小文件，不写源 BDMV。

## 真实 BDMV 验证范围

真实数据验证在 macOS 上使用 Python 3.11+ 与带 libbluray 的 FFmpeg/FFprobe 8.1.2 完成，覆盖：

- 单段电影正片与 1080p/4K 多版本；
- 同一电影跨多个 M2TS 的 seamless playlist；
- 多张盘连续收录多集的剧集 Play-All，包括一集一个 M2TS 和一集跨多个 M2TS；
- 纯特典盘、花絮 Play-All 和大量超短交互导航；
- 直接复制、硬链接和 libbluray stream-copy 重封装。

这些结果证明构建链路可用，不代表程序能执行每张盘的 BD-J 菜单。每次执行仍应复核 `warnings`、主标题 playlist、episode 数量和 dry-run 操作。

## 多盘剧集案例

一套两盘剧集的每张盘都包含一个约两小时的 Play-All，内部是 6 个约 24 分钟、完整覆盖各自 M2TS 且带起点 Entry Mark 的 PlayItem。配置为 `disc_type = "series"` 后：

- 上盘输出 `S01E01` 至 `S01E06`；
- 下盘输出 `S01E07` 至 `S01E12`；
- 每集是一对一完整 M2TS，因此 `copy_remux` 选择复制，硬链接模式选择硬链接；
- `connection_condition=6`、局部 clip、多角度或内容型 SubPath 会阻止拆集。

若只有多个时长相近的独立 playlist、但不存在 Play-All 顺序旁证，规划器不会凭 playlist 编号猜测它们都是 episode：libbluray 选中的标题作为单集，其余保留为 extras 候选并产生边界警告。

另一套多盘剧集的前半部分是一集一个完整 M2TS，后半部分则每集由 3–5 个完整 M2TS 组成。规划器从前半部分得到约 24 分钟的稳定 episode 中位时长，再结合后半部分的非无缝 Entry Mark 分组为正确集数。代表性分集由 5 个 M2TS 使用 concat stream-copy 生成：计划时长与成品相差小于 0.02 秒，H.264 1080p 视频和 PCM 音频轨道保持一致，逐包时间线校验通过。

单个完整 M2TS 包含两集或多集的结构由合成回归覆盖：程序优先采用连续覆盖父范围的原盘独立单集 playlist；仅在没有此类旁证、且章节重复结构或同行盘时长轮廓能完整分区时，才生成章节派生分集。等时长均匀章节不会单独构成多集证据。

## 多段电影验证

验证样本的电影主 playlist 由两个连续 PlayItem 组成。直接取首个 M2TS 会缺失后半段；二进制 `cat` 也不会处理各 clip 独立的时间戳域。

该样本曾暴露第二段 DTS 相对前一段倒退约 5.9 秒的问题：FFmpeg 默认 discontinuity 阈值没有修正，错误成品会比 MPLS 逻辑时长更长。当前 bluray 输入使用 `-dts_delta_threshold 1`，修正后的成品与 MPLS 累计时长相差小于 0.02 秒。

成品还通过以下检查：

- 视频、全部有效音轨和 PGS 字幕的结构与预期一致；
- 重封装允许 MPEG-TS muxer 合法重分配 PID，但保留媒体类型内轨道顺序；
- 音视频逐包 DTS 没有超过阈值的空洞或倒退；
- 每条音视频轨道都在节目首尾容差内持续存在。

## 操作与状态验证

完整单段的三种策略行为如下：

| 策略 | 同文件系统 | 跨文件系统或链接失败 | 分段/裁切内容 |
|---|---|---|---|
| `copy_remux` | copy | copy | remux |
| `hardlink_remux` | hardlink | copy | remux |
| `hardlink_only` | hardlink | 整盘阻止 | 整盘阻止 |

`hardlink_only` 还会在任一源 M2TS 缺失、已有目标不再与源共享文件身份时阻止整张盘。`copy_remux` 不接受已有目标仍是源文件硬链接；只有显式 `--overwrite` 才会用独立目标目录项替换它。

状态文件使用 schema version 7。copy/remux 成品记录完整文件 SHA-256，remux 另记录带版本的逻辑计划指纹及实际 operation/backend；`status` 同时检查大小和完整哈希，只有内容匹配时才把原路径或唯一且不是原盘源路径的搬移候选标记为 `verified`。旧 schema 4/5/6 仍可读取；缺少完整哈希的非硬链接记录标记为 `unverified`，缺少当前指纹版本的旧 remux 状态不能用于自动复核已有成品。任何 `missing`、`modified`、`broken-hardlink` 或 `unverified` 都使 `status` 返回非零退出码。

## 隐私与源盘保护

- scan、plan 和 dry-run 不写媒体；
- build 只写计划声明的目标根目录；
- 所有真实 BDMV 在验证期间保持只读；
- 完整库 plan、results、state 和任务 TOML 不纳入版本控制；
- 仓库不保留从真实媒体库导出的计划、结果、逐盘映射或专用 ffconcat。

更完整的安全边界见 [隐私与安全边界](../docs/privacy-and-safety.md)。

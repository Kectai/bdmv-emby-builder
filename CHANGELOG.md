# 版本变更

本项目使用 PEP 440 版本号。Beta 版本表示功能和数据格式已经可用，但自动识别结果及跨平台行为仍需要更广泛验证。

## 0.4.0b1 - 2026-08-06

- 将项目状态定为 Beta；统一以“先识别逻辑视频，再选择直接复制、硬链接或重封装”为核心模型。
- 加入电影、剧集和纯特典盘分类，多盘剧集按季连续编号，并支持显式季号、起始集号和 edition。
- 加入 Play-All 拆分、交互导航排除和完全离线的主标题降级链路；跨盘 playlist 规律仅作为提示，不覆盖本盘结果。
- 加入 `copy_remux`、`hardlink_remux`、`hardlink_only`，以及硬链接能力、路径和整批空间预检。
- M2TS 重封装默认通过 libbluray 读取 MPLS；收紧 seamless branching 时间戳归一化，并加入时长、轨道和逐包时间线校验。
- 加入原子结果/状态写入、目标目录锁、中断与陈旧 partial 恢复、现有成品复核和状态查询。
- 状态 schema 7 为 copy/remux 成品加入完整 SHA-256，remux 另绑定带版本的逻辑计划指纹及实际 operation/backend；旧 schema 4/5/6 保持可读，状态查询会识别内容变化并对异常返回非零退出码。
- 收紧 episode 拆分、`connection_condition=6`、缺失源、现有文件身份、跨盘 extras 冲突、edition 编号、MPLS 区段和 Windows 路径/进程处理。
- 阻止审计 JSON 覆盖源或媒体，保守识别大小写/Unicode 路径别名，拒绝 BDMV 树内及媒体/work 的符号链接，并保护 state/lock/work 内部路径；以当前源大小校正容量估算，无可执行 job 的整盘阻断也会返回非零结果。
- 限制完整片段边界与成品时长容差，避免异常配置绕过必要的重封装或成品校验。
- 收紧 Windows 尾随点/空格及设备名路径，并以原子替换写入 ffconcat/ffmetadata 控制文件，避免叶子链接修改源内容。
- 统一规划器与构建器的文件名长度预算，扩展名计入 UTF-8/UTF-16 上限。
- 计划执行强制校验真实 BDMV/PLAYLIST/STREAM 结构并移除 libbluray 根目录回退，阻止伪造计划扩大源读取边界。
- BDMV 遍历拒绝 Windows junction/reparse point、FIFO、socket 和设备文件；MPLS/META/state/lock 只接受有界普通文件，避免越界披露或特殊文件阻塞。
- 显式 copy/remux operation 必须与 `.m2ts`/`.mkv` 输出扩展名一致，保证实际容器与结果、state、status 审计一致。
- 源目录必须选择 BDMV 的盘根或更上层，不再接受直接以 `BDMV` 本身作为任务根，确保 libbluray 输入不越出声明边界。
- POSIX 上工具新建的媒体目录使用 `0755`，copy/remux 成品在原子替换前统一设为 `0644`；已有目录和 hardlink 源文件权限不变。
- 目标构建锁改为全程持有的 POSIX/Windows OS 文件锁；锚文件常驻且不再执行存在 TOCTOU 的陈旧 PID 删除。
- 构建锁锚点只保留固定标识，不持久化主机名、PID、时间或路径。
- state 与构建锁要求普通、非链接且链接计数为 1，拒绝硬链接控制路径修改或读取其他文件身份。
- 构建前重新解析 MPLS，并将 playlist、PlayItem clip/顺序/in-out、派生分段起点和总时长绑定到计划，拒绝错片或伪造 items。
- 派生分段会按与规划器共享的阈值重新证明 Entry Mark、连接条件、完整源、SubPath、重复 clip 和独立 playlist 旁证；状态重定位排除原盘源路径。
- 覆盖模式同样拒绝链接型媒体叶子；状态输出限制在目标根内，重定位遍历不跨 junction/reparse point。
- 长 job id 在媒体 partial 名中折叠为固定 16 位 token；陈旧 partial 只清理符合本工具固定 8 字符随机段的精确名称。
- Ctrl-C 结果保留已完成、中断和未运行任务；逐包校验覆盖视频、音频和字幕，并检查音视频轨道首尾覆盖。
- 加入 Ubuntu、macOS、Windows 的 Python 3.11 自动测试矩阵。
- 将人工配置迁移为 TOML；网络元数据查询不再属于规划流程。
- 完善项目文档、Beta 限制和隐私规范；不在仓库保留真实媒体库计划、结果、逐盘映射或专用 ffconcat。
- 项目采用 MIT 许可证发布。

## 0.3.0

- 建立直接复制与必要时 M2TS 无损重封装的完整工作流，并扩展真实 BDMV 验证。

## 0.1.0

- 初始 MPLS 扫描、规划和 MKV 无损封装原型。

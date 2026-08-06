# BDMV → Emby：直接复制与必要时 M2TS 无损重封装方案

本文是 `0.4.0b1` 的技术设计与实现约束。安装、配置和日常命令以项目 [README](../README.md) 为准；隐私与写入边界见[隐私与安全边界](privacy-and-safety.md)。

## 1. 最终结论

本项目采用两阶段自动策略。第一阶段判断一条 playlist 中包含几个逻辑视频：

```text
剧集盘中多个时长相近的长 PlayItem
    → 按 episode 拆分

多个完整、不同的源 M2TS，且存在非无缝边界，或 Entry Mark 得到独立 playlist/短 IG Play-All 佐证
    → 识别为 Play-All 合集，按边界拆成多个视频

大量不同但极短的 clip 组成交互导航/图库
    → 排除，不当作媒体视频

connection_condition=6、局部切点、重复 clip、内容型 SubPath 或无可靠边界
    → 保持为一个逻辑视频
```

第二阶段才决定每个逻辑视频的物化方式：完整单 M2TS 复制或硬链接；单 PlayItem 局部范围、以及仍由多个连续 PlayItem 组成的视频使用 libbluray 无损重封装。视频、音频和字幕不重新编码。

输出目录是可以删除后重新生成的 Emby 派生库；原始 BDMV 始终只读。

硬链接不作为默认输出：`copy_remux` 的完整单段采用普通复制，使 Emby 文件拥有独立文件身份，不会因为成品被原地修改而影响源 BDMV。需要节省空间时，可为单张盘显式选择 `hardlink_only` 或 `hardlink_remux`。

三种目录级策略如下：

- `hardlink_only`：所有内容都必须是一对一完整 M2TS、位于同一文件系统且通过实际硬链接能力测试；否则整张盘阻止处理。
- `hardlink_remux`：满足条件的内容硬链接，真正需要组合或裁切的内容无损重封装；完整单段在跨文件系统或运行时链接失败时降级为逐字节复制，不做没有必要且兼容性更差的重封装。
- `copy_remux`：满足条件的内容复制，其余内容无损重封装，是默认值。

硬链接和重封装使用相同的 Emby 媒体命名；区别写入 dry-run、构建结果及 `.bdmv-emby-state.json`。`bdmv-emby-builder status DESTINATION` 会验证所有成品是否存在，并在保留原文件名时有限重定位人工移动的成品：copy/remux 使用文件大小与完整 SHA-256，hardlink 使用与源共享的文件身份。

## 2. 为什么一个 playlist 可以引用多个 M2TS

BDMV 不是“一个电影文件夹”，而是一套播放结构：

| 组成 | 职责 |
|---|---|
| `BDMV/STREAM/*.m2ts` | 物理音视频 clip |
| `BDMV/CLIPINF/*.clpi` | 时间、入口点、数据包和连接信息 |
| `BDMV/PLAYLIST/*.mpls` | PlayItem 顺序、in/out、章节和逻辑标题 |
| `index.bdmv`、MovieObject、BD-J | 标题入口、菜单和导航 |

一个 MPLS 可以引用多个 M2TS，一个 M2TS 也可以被多个 MPLS 复用。原盘播放器读取的是逻辑标题和播放列表，而不是把某个 `STREAM/00000.m2ts` 直接当成完整电影。

实体播放器通过 `index.bdmv` 进入 HDMV/BD-J 导航，再由菜单和用户操作选中播放标题。[Blu-ray Disc Association 的应用规范](https://us.blu-raydisc.com/wp-content/uploads/sites/2/2019/09/bdj_gem_application_definition-15496.pdf)说明 Index Table 指向 First Playback、Top Menu 和各个 Title，Movie Object/BD-J 再执行导航命令；它不是一个通用的“正片 MPLS”字段。libbluray 开发者也明确指出，不经过菜单交互寻找主电影始终是一种猜测：[VideoLAN libbluray-devel 讨论](https://mailman.videolan.org/pipermail/libbluray-devel/2014-July/001503.html)。

无菜单自动处理时，项目先调用 [FFmpeg `bluray:` 协议](https://ffmpeg.org/doxygen/8.0/bluray_8c_source.html)，采用 FFmpeg/libbluray 返回的默认相关 playlist；该实现以 `TITLES_RELEVANT` 获取相关标题、排除短于 180 秒的候选，再选择最长 playlist。本项目只解析 FFmpeg 的选择结果，不复制其源码。程序还会排除明显菜单循环，以包含 PlayItem、连接条件、多角度、STC、章节、SubPath 和 MPLS 文件摘要的语义签名合并完全相同的 playlist。整个规划过程只读取本地 BDMV，不访问网络。

若一张电影盘存在长标题歧义，规划器会检查同一 META 系列中其他无歧义盘的选择规律，但只把结果写为审核提示。playlist ID 是光盘内局部编号，即使多张盘都选择 `00000`，也不能证明另一张盘的 `00000` 是正片，因此同行盘投票永远不能覆盖本盘 libbluray 结果。每个计划都记录 `playlist_selection`，顶层 `recognition.main_selection_counts` 汇总选择方式，便于执行前复核。

用户只声明 `disc_type`：`movie` 表示单部电影正片盘；`series` 表示包含一集或多集正片的剧集盘；`bonus` 表示纯特典盘。三种盘都可能包含附加内容，因此单个输出任务再使用 `main`、`episode` 或 `extras` 表示内容性质。

`movie` 盘自动输出一个主正片；`series` 盘只在主 playlist 本身给出 Play-All 顺序，且各长 PlayItem 具有起点 Entry Mark、完整覆盖不同源 clip 时按 PlayItem 拆集。多个长度相近的独立 playlist 不足以证明 episode 成员关系和顺序，因此不会按 playlist 编号聚类；本盘主标题作为单集，其余进入 extras 候选并提示复核。同一标准化标题、同一季和同一 edition 下的多张盘按自然顺序连续编号，不同季或 edition 各自从 `E01` 开始。季号优先使用用户配置，其次读取 META 标题和目录中的明确 `Season N`、`Nth Season`、`第N期/季` 或 `シーズンN` 标记；没有可靠证据时默认第 1 季并给出警告，不从裸数字或 `S2` 猜测。用户可用 `episode_start` 处理非连续起始集号，规划阶段会阻止组内集号范围重叠。

电影和剧集盘的其余语义唯一、非导航且不少于默认 60 秒的 playlist 都作为 `extras` 候选。花絮 Play-All 只有在完整源覆盖、唯一 clip、无多角度及无内容型 SubPath 的前提下才拆分：非无缝连接是强边界；无缝 Entry Mark 还必须得到独立单视频 playlist 或短时 IG Play-All 结构佐证。拆分结果再按实际完整 clip 去重，并优先保留独立 playlist。`bonus` 盘使用相同逻辑。60 秒阈值作用于候选 playlist，不会二次删除已可靠识别出的短分段。若电影盘还存在不少于 20 分钟、且时长达到所选主标题 35% 的其他 playlist，计划会给出歧义警告。

例如，一条分段电影 playlist 可能依次使用：

```text
00000.m2ts → 00001.m2ts
```

即使两个 clip 的原始时间戳数值存在交叠，它们也可能属于彼此独立的 clip 时间域；不能据此删除画面，也不能使用 `cat` 二进制连接。逻辑顺序和有效范围必须以 MPLS 的 PlayItem 为准。

## 3. 为什么优先让 libbluray 读取 MPLS

FFmpeg 的 concat demuxer 支持 `inpoint/outpoint`，但对 H.264/HEVC 等非帧内编码，官方明确提示可能输出切点前后的额外数据。蓝光还可能包含 seamless branching、解码预滚、音频帧边界和字幕状态。

因此，M2TS 重封装默认使用带 libbluray 的 FFmpeg：

```bash
ffmpeg \
  -playlist 0 \
  -i "bluray:/path/to/disc-root" \
  -map "0:v?" -map "0:a?" -map "0:s?" \
  -c copy \
  -f mpegts -mpegts_m2ts_mode 1 \
  output.m2ts
```

FFmpeg 官方说明：<https://ffmpeg.org/ffmpeg-protocols.html#bluray>

`remux_backend=auto` 会优先并实际要求 `bluray` 协议。若当前 FFmpeg 不支持，构建会停止并给出明确错误，不会静默改用风险更高的方式。用户只有在明确接受通用 concat 边界限制时，才应设置：

```toml
[settings]
remux_backend = "concat"
```

## 4. 依赖与跨平台策略

项目代码只依赖 Python 3.11+ 标准库，不需要第三方 Python 包。如果未来增加 Python 依赖，必须在项目目录的 `.venv` 中安装。

实际构建需要：

- FFmpeg；
- FFprobe；
- 多段/切点重封装时，FFmpeg 最好并默认需要启用 libbluray。

不要在程序中硬编码 Homebrew、Linux 或 Windows 路径。工具解析顺序为：

1. 环境变量 `BDMV_EMBY_FFMPEG` / `BDMV_EMBY_FFPROBE`；
2. TOML `[settings].ffmpeg` / `[settings].ffprobe`；
3. 当前 `PATH` 中的 `ffmpeg` / `ffprobe`。

检查环境：

```bash
bdmv-emby-builder doctor
```

成功结果必须表明 FFmpeg 和 FFprobe 都具有该协议：

```json
{
"ffmpeg_bluray_protocol": true,
"ffprobe_bluray_protocol": true,
"bluray_protocol": true
}
```

### macOS / Homebrew

普通 `ffmpeg` formula 不含 libbluray，使用：

```bash
brew install ffmpeg-full
export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"
```

只有当前 shell 尚未优先解析到 `ffmpeg-full` 时才需要设置 `PATH`；`doctor` 已显示正确的 `ffmpeg-full` 路径和 `bluray_protocol: true` 时无需重复配置。

`brew --prefix` 会同时适配 Apple Silicon、Intel Homebrew 和自定义前缀：

```bash
brew --prefix ffmpeg-full
```

### Linux 与 Windows

安装所在发行版或 FFmpeg 供应方提供的 libbluray-enabled 构建，然后使用 `doctor` 检测。程序不依赖 shell 拼接、Unix inode 命令或 macOS 专用 API，因此 Python 业务逻辑可跨平台运行。

## 5. 逻辑视频边界与复制判定

META/DL 可以提供盘名，少数盘还提供 Title 目录名称，但 Title 与 playlist/PlayItem 并非普遍可直接映射，因此 META 只作为命名与审计证据。视频边界主要使用 MPLS 和实际 clip：

1. Entry Mark 是否位于 PlayItem 的 in-point，以及是否有独立 playlist 或短时 IG Play-All 结构佐证；
2. `connection_condition`：1 为非无缝，5 为无缝，6 为逻辑连续；
3. FFprobe 是否能证明 PlayItem 完整覆盖源 M2TS；
4. clip 是否唯一，避免把复用/循环结构拆开；
5. 是否存在多角度或内容型 SubPath；类型 3 的 Interactive Graphics 菜单可忽略，字幕、画中画、3D/Dolby Vision 等内容型 SubPath 不拆；
6. 时长分布是否符合 episode，或是否属于大量极短 clip 的交互导航结构。

只有证据充分时才拆分；无法可靠判断时保持 playlist 整体，避免破坏真正连续的视频。

拆分后的 episode 或 Play-All 分段必须已经对应一个经 FFprobe 证明的完整独立 M2TS，因此物化时只采用复制或硬链接。不同 PlayItem 可以拥有彼此独立的时间戳域，libbluray/FFmpeg 的按秒片段 seek 对这类 playlist 并不普遍可靠；执行器会拒绝把派生分段退化成 playlist seek 重封装。内容型 SubPath 或多角度存在时，规划器保持整个 playlist，不生成这种分段任务。

无缝分支的各 clip 可能使用彼此重叠或倒退的原始时间戳域，但 MPLS 定义的逻辑播放时长仍是各 PlayItem 有效范围之和。bluray 后端先以 1 秒阈值归一化 MPEG-TS discontinuity；成品只接受 MPLS 累计时长，不把原始 libbluray 时间轴或异常空洞的镜像时长作为候选。默认容差为 2 秒，也可显式设为 0。

只有同时满足以下条件才直接复制：

1. playlist 只有一个 PlayItem；
2. 输出容器是 M2TS；
3. FFprobe 能读取源文件 `start_time` 与 `duration`；
4. `MPLS in ≈ start_time`；
5. `MPLS out ≈ start_time + duration`；
6. 差值不超过 `copy_boundary_tolerance_seconds`，默认 0.1 秒且最多 0.5 秒，防止把实际裁切误判为完整源文件。

不能证明完整覆盖时，一律选择重封装，不猜测。

复制使用 Python `shutil.copyfile`，可以利用各平台提供的快速复制系统调用。目标先以不可预测的临时文件名写入同一输出目录，校验通过后再原子替换为正式文件。

## 6. 重封装判定与输出

完成逻辑边界判断后，以下情况必须重封装：

- 同一个逻辑视频仍包含两个及以上 PlayItem；
- 单 PlayItem 只使用 M2TS 的局部范围；
- FFprobe 无法证明该 PlayItem 覆盖完整文件。

输出结构保持 Emby 电影命名：

```text
Movies/
└── Movie Name/
    ├── Movie Name - 1080p.m2ts
    ├── Movie Name - 4K.m2ts
    └── extras/
```

Emby 文件名中不加入 `Copy`、`Remux` 等后缀，避免影响识别和多版本规则。实际构建方式记录在 `.bdmv-emby-state.json`。

## 7. 安全边界

程序执行前强制验证：

- `destination_root` 不能位于 `source_root` 内；
- 每个 job 必须显式指向 `source_root` 内的标准 BDMV 目录，MPLS 必须直属其 `PLAYLIST` 并与 playlist ID 一致，源文件必须是其 `STREAM` 下的五位编号 M2TS；BDMV 树拒绝链接型路径、FIFO、socket 与设备文件，MPLS/META 只做有界普通文件读取；构建前会重新解析 MPLS，核对 PlayItem clip/顺序/in/out、派生分段起点及总时长；
- `task.source` 必须是 BDMV 的盘根或更上层目录，不能直接指向 `BDMV` 本身，以保证 libbluray 盘根仍在声明的只读源边界内；
- 每个输出必须位于 `destination_root` 内；
- 显式 `copy`、`remux_m2ts`、`remux_mkv` 必须分别匹配其实际输出容器扩展名；
- 源和输出不能是同一路径；
- 默认不覆盖已有成品；
- 只有显式 `--overwrite` 才替换目标目录项；
- 已有成品不重写，但必须重新校验并补建/刷新状态；
- 已有 hardlink 必须仍与源共享文件身份；copy/remux 目标不得意外与源共享身份；
- 已有媒体成品和构建工作目录不得是符号链接；
- 新生成的目标媒体目录在 POSIX 上使用 `0755`，copy/remux 媒体使用 `0644`，便于与构建进程不同账号运行的 Emby 读取；已有目录权限不被修改，hardlink 保留源文件本身的权限与身份；
- scan/plan 的 `--out` 与 build 的 `--results` 必须使用 `.json` 文件名，不得覆盖源文件、整个目标媒体库、输入配置/计划、媒体成品或内部 state/lock/work 路径；媒体输出必须是目标根下的规范相对文件路径，并拒绝源 BDMV 组件通过符号链接越界；
- 同一目标根目录用构建期间持续持有的跨平台 OS 文件锁阻止并发构建；锁锚文件可常驻，只写固定标识且不记录主机名、PID 或路径，并必须是链接计数为 1 的普通非链接文件；程序不再通过 PID 检查后删除旧锁；
- 替换通过临时文件和 `os.replace` 完成，不会对源文件原地写入；
- Ctrl-C 清理当前 partial 并释放 OS 锁，将已完成、当前中断及其余未运行任务写入结果；下次执行也会清理与同一 job 精确匹配的陈旧 partial，包括正式目标已经存在的情况。进程退出会由操作系统自动释放锁，常驻锚文件无需删除。

工作文件位置：

```text
<destination_root>/.bdmv-emby-work/
<destination_root>/.bdmv-emby-state.json
<destination_root>/.bdmv-emby-build.lock
<output-dir>/.<name>.<job-id>.<random>.partial.m2ts
```

单元测试临时文件必须通过 `TMPDIR` 放在项目所在硬盘，例如：

```bash
mkdir -p .test-tmp
TMPDIR="$PWD/.test-tmp" python3 -m unittest discover -s tests -v
```

## 8. 容量保护

计划为每个 job 记录 `estimated_output_bytes`。构建时会重新读取当前源文件大小，并取“计划估算值”和“当前全部源文件大小之和”中的较大值；局部裁切时可能高估，但计划被修改或源文件变大时不会直接信任过小值。

构建每个 job 前重新读取目标卷剩余空间，并保留：

```text
max(minimum_free_space_bytes, 文件系统总容量 × free_space_margin_ratio)
```

默认值：

- 固定最小余量 5 GiB；
- 文件系统容量余量 5%。

默认还会在批量执行前按目标文件系统分别汇总所有待写 job；任一目标卷空间不足时，在写入任何媒体前停止。`hardlink_remux` 会先实际测试链接能力，链接失败的任务先改为复制再参与整批估算。每个 job 开始前仍会再次检查，防止执行期间剩余空间发生变化。

## 9. 校验

每个成品必须通过：

1. FFprobe 能打开；
2. 输出时长与 MPLS 累计时长之差不超过 `duration_tolerance_seconds`，默认 2 秒且最多 5 秒；
3. 视频、音频、字幕的 codec、分辨率/声道、采样率、布局、位深和语言与预期一致；直接复制/硬链接还严格比较 PID，重封装则比较媒体类型内轨道顺序并允许 muxer 合法重分配 PID；
4. TrueHD 的无效 AC-3 占位流只有在同 PID 精确配对时才忽略，独立的有效 AC-3 Core 仍必须保留；
5. 多 PlayItem 在构建前具有一致的轨道布局；
6. 重封装成品逐包确认每条视频、音频和字幕流都存在并拒绝超过 0.05 秒的时间戳倒退；音视频流另外拒绝超过 0.25 秒的异常空洞，以及相对节目首尾超过 2 秒的轨道覆盖缺口；
7. 通过后才把 `.partial` 原子改名为正式文件；
8. 状态文件记录 operation、backend、MPLS、全部源文件、期望/实际时长、轨道和包时间线摘要；copy/remux 另记录完整文件 SHA-256，remux 同时记录逻辑计划指纹，用于识别内容变化和安全重定位。

当前状态 schema 为 7，计划指纹带独立版本。schema 4、5 和 6 仍可读取和升级，但缺少完整内容哈希的旧 copy/remux 记录只能标记为 `unverified`；缺少当前指纹版本的旧 remux 状态可供 `status` 核对内容，但不能自动复核已有 remux。已有 copy 只有与源文件逐字节一致时才可复核；已有 remux 只有同时匹配可信状态中的指纹版本、计划指纹、实际 operation/backend 和完整哈希时才可复核，否则必须人工确认后使用 `--overwrite` 重建。`status` 重定位会排除状态中记录的原盘源路径；发现 `missing`、`modified`、`broken-hardlink` 或 `unverified` 时返回非零退出码。

`M2TS` 不适合承载 Matroska 式章节元数据。MPLS 章节继续保存在 plan/state 中用于审计，但不能保证在 Emby 中显示原盘章节。

## 10. 自动元数据与配置

每个任务使用一个 TOML，完整保存本次任务参数：

```toml
[task]
source = "/absolute/path/to/MOVIE_DISC"
destination = "/absolute/path/to/EmbyMovies"

[[disc]]
disc_type = "movie"
```

```bash
bdmv-emby-builder plan --config task.toml --out plan.json
```

这里的 `--out plan.json` 是生成审核计划的输出位置，不是额外配置。

自动取得的信息包括：

- `META/DL/bdmt_jpn.xml` 中的标题，缺失时依次使用英文元数据和目录名；
- 正片 MPLS 候选、PlayItem 顺序、切点、时长与章节；
- 所选正片的视频分辨率，并据此生成 `1080p` / `4K` 版本名；
- 直接复制或无损重封装方式。

上映年份暂不推断，也不自动加入目录名。花絮没有可靠自然语言标题时，按“元数据盘名 + playlist 编号 + 时长”命名。

TOML 中的 `[task]` 只保存源路径和目标路径；规划时会统一解析为绝对路径写入 plan，确保换工作目录后仍可重复执行。`[[disc]]` 保存盘类别和可选影片归属。路径属于用户任务数据，不写入程序代码，因此不构成代码硬编码。TOML 支持注释，且 Python 3.11 标准库可以直接读取，不增加第三方依赖。旧 JSON 配置仅保留兼容读取。

当 `task.source` 只指向一张盘时，使用一个省略 `path` 的 `[[disc]]`。当它指向包含多张盘的根目录时，每个 `[[disc]].path` 描述相对于 `task.source` 的位置；盘类别、Bonus 归属和多版本归属属于对应的 disc。

```toml
[task]
source = "/absolute/path/to/BDMV_LIBRARY"
destination = "/absolute/path/to/EmbyMovies"

[[disc]]
path = "Release/BONUS_DISC"
disc_type = "bonus"
title = "Movie Name"
edition = "Director's Cut"
```

`edition` 用于同一作品、同一分辨率的多个剪辑版；它会与自动探测的 `1080p`/`4K` 一起进入 Emby 版本文件名，避免输出冲突。

TOML 不接受 playlist ID、`kind`、`folder` 或单个花絮名称。无法可靠识别语义的附加内容统一进入 `extras`，用户可在构建后自行移动或改名。

## 11. 审核与执行流程

```bash
# 1. 依赖检查
bdmv-emby-builder doctor

# 2. 扫描，只读
bdmv-emby-builder scan SOURCE --out scan.json

# 3. TOML 已包含 source、destination 和盘类别
bdmv-emby-builder plan --config task.toml --out plan.json

# 4. 解析 auto job，确认 copy/remux 和容量，不写影片
bdmv-emby-builder build plan.json --results dry-run-results.json

# 5. 人工检查 plan 和 dry-run

# 6. 先构建一个 job
bdmv-emby-builder build plan.json --execute --only JOB_ID \
  --results one-job-results.json

# 7. 播放检查接缝、音轨和字幕后，再决定是否执行其余 job
```

构建结果是一个稳定的 JSON 对象，包含 schema、时间、`mode`、`complete`、顶层 `error` 和固定字段的 `jobs`。实际执行遇到阻止、缺失、失败或未运行任务时返回非零。plan 与结果路径通过只读安全校验后，后续异常或 Ctrl-C 会用本轮错误文档原子替换旧结果；若 plan 本身不可信或结果路径不安全，程序拒绝写入结果文件。

## 12. 归档与备份

原始 BDMV 是源归档，Emby 目录是派生数据。两者位于同一块物理硬盘时不构成备份；硬盘损坏会同时丢失。真正备份必须位于另一块物理介质或独立存储系统。

在确认新目录播放正常前，不删除、移动或修改原始 BDMV。

计划、结果和状态包含绝对路径及媒体库清单，属于本地任务数据；公开前必须匿名化。

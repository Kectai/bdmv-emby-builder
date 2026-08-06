# BDMV → Emby Builder

BDMV → Emby Builder 只读分析 Blu-ray BDMV，识别电影、剧集和附加内容，并生成适合 Emby 扫描的媒体目录。项目以 MPLS 为播放关系来源：完整单 M2TS 直接复制或硬链接，确属同一逻辑视频的连续分段或局部切点才无损重封装为单个 M2TS。

当前版本为 **0.4.0 Beta 1**。核心扫描、规划、复制、硬链接、重封装和校验流程已具备真实原盘验证；自动识别仍是无菜单环境下的保守推断，执行前必须审核计划和警告。源 BDMV 不会被移动、重命名或修改。

本项目是非官方工具，与 Emby LLC 或 Blu-ray Disc Association 没有隶属、授权或背书关系。

## 功能范围

- 递归发现单张或多张 BDMV，读取 META、MPLS、PlayItem、Entry Mark、in/out、章节、连接条件、多角度、STC 和 SubPath；
- 离线识别电影主标题、剧集 episode 和附加内容，不查询网络元数据；
- 拆分剧集型 Play-All：支持一集对应一个 PlayItem、一集由多个完整 PlayItem 组成，以及一个 M2TS 由独立子 playlist 或重复章节结构划分为多集；
- 拆分边界可靠的花絮 Play-All，排除重复 playlist、菜单循环及高密度超短交互导航；
- 支持 `copy_remux`、`hardlink_remux` 和 `hardlink_only` 三种目录级策略；
- 默认输出 M2TS，视频、音频和 PGS 字幕均使用 stream copy，不重新编码；
- 生成可审核的扫描、计划、dry-run、构建结果和状态文件；
- 写入前校验路径、硬链接能力与剩余空间，写入后校验时长、轨道身份和重封装时间线；
- 使用目标目录锁、隐藏临时文件和原子替换处理并发、中断与失败恢复。

项目不执行完整 BD-J 菜单，也不能从 BDMV 稳定得知每段花絮的自然语言名称。无法充分证明的内容会保留警告或保持整体，不会依靠作品名、固定 playlist ID 等特例强行判断。

## 识别与处理原则

```text
发现 BDMV → 解析 META/MPLS → 去重并排除导航内容
    ↓
按 disc_type 识别 movie / series / bonus
    ↓
判断 playlist 中有几个逻辑视频
    ├─ 一集对应一个完整 PlayItem              → 按 PlayItem 拆分
    ├─ 一集由多个完整 PlayItem 组成            → 按可靠边界分组
    ├─ 一个 M2TS 包含多集                     → 优先独立子 playlist，其次重复章节
    ├─ 完整独立 M2TS 且具有可靠边界的 Play-All → 按视频拆分
    ├─ 菜单、图库和交互导航                    → 排除
    └─ 连续分段、局部切点或证据不足             → 保持整体
    ↓
完整单 M2TS                     → copy / hardlink
完整 playlist                   → libbluray stream-copy remux
已验证的派生分集片段组或章节区间 → concat stream-copy remux
```

逻辑边界综合使用源文件完整覆盖、clip 唯一性、Entry Mark、连接条件、多角度、SubPath、时长分布及其他 playlist 的旁证。`connection_condition=6`、重复 clip、多角度、内容型 SubPath 或不可靠边界不会被拆分。

电影主标题优先采用本盘 FFmpeg/libbluray 的相关标题结果；不可用时才降级为本盘最长有效候选并生成警告。同一 META 系列其他盘的选择规律只写入审核提示，绝不按跨盘 playlist 编号覆盖本盘结论，因为 MPLS 编号只在各自光盘内有意义。全部规则只读取本地数据。

更完整的设计、边界和实现依据见[直接复制与必要时重封装方案](docs/copy-and-m2ts-remux-strategy.md)。

## 系统要求

- Python 3.11 或更高版本；
- FFmpeg 和 FFprobe；
- 带 libbluray/`bluray` protocol 的 FFmpeg，用于相关标题识别和多段/切点重封装。

项目没有第三方 Python 依赖。建议安装到项目内虚拟环境；激活后统一使用 `bdmv-emby-builder` 命令：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
bdmv-emby-builder --help
```

Windows PowerShell 对应命令为：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
bdmv-emby-builder --help
```

下文命令均假设虚拟环境已经激活；不激活时可直接使用 `.venv/bin/bdmv-emby-builder`，Windows 使用 `.venv\Scripts\bdmv-emby-builder.exe`。

macOS Homebrew 可安装：

```bash
brew install ffmpeg-full
```

只有当前 shell 尚未找到该构建时才需要调整 `PATH`。工具按以下顺序解析：

1. `BDMV_EMBY_FFMPEG` / `BDMV_EMBY_FFPROBE` 环境变量；
2. TOML `[settings].ffmpeg` / `[settings].ffprobe`；
3. 当前 `PATH` 中的 `ffmpeg` / `ffprobe`。

Linux 和 Windows 应安装各自平台带 libbluray 的 FFmpeg 构建。安装后检查实际解析到的程序和协议能力：

```bash
bdmv-emby-builder doctor
```

需要重封装时，结果中的 `bluray_protocol` 应为 `true`。

## 快速开始

复制 [config.example.toml](config.example.toml) 为本地任务文件，例如 `task.toml`：

```toml
[task]
source = "/absolute/path/to/BDMV_LIBRARY"
destination = "/absolute/path/to/EmbyLibrary"

[[disc]]
path = "Movie/MAIN_DISC"
disc_type = "movie"
processing = "copy_remux"

[[disc]]
path = "Movie/BONUS_DISC"
disc_type = "bonus"
title = "Movie Name"
processing = "copy_remux"
```

按以下顺序执行：

```bash
# 1. 环境检查
bdmv-emby-builder doctor --config task.toml

# 2. 生成可审核计划；不写媒体
bdmv-emby-builder plan --config task.toml --out plan.json

# 3. dry-run；解析最终 copy / hardlink / remux 动作
bdmv-emby-builder build plan.json --results dry-run-results.json

# 4. 先构建并播放检查一个任务
bdmv-emby-builder build plan.json --execute \
  --only JOB_ID --results one-job-results.json

# 5. 确认后执行完整计划
bdmv-emby-builder build plan.json --execute \
  --results build-results.json

# 6. 核验目标库状态
bdmv-emby-builder status "/absolute/path/to/EmbyLibrary"
```

`scan` 是可选的更底层只读检查：

```bash
bdmv-emby-builder scan "/absolute/path/to/BDMV_LIBRARY" --out scan.json
```

`plan.json` 是程序生成的审核产物，不是第二份人工配置。重新规划后应重新执行 dry-run，不要手工修改旧计划继续构建。

## 配置

每个 TOML 文件描述一次任务。`[task]` 只放任务级输入输出；`[[disc]]` 只放每张盘需要人工表达的类别、归属和处理策略。

### `[task]`

| 字段 | 必填 | 说明 |
|---|---:|---|
| `source` | 是 | 单张蓝光盘根目录（其下包含 `BDMV`）或包含多张盘的源目录；不能直接指向 `BDMV` 本身，建议使用绝对路径 |
| `destination` | 是 | 新的 Emby 媒体根目录；建议使用绝对路径 |

命令行位置参数可临时覆盖这两个字段。

### `[[disc]]`

| 字段 | 必填 | 说明 |
|---|---:|---|
| `path` | 多盘任务需要 | 相对于 `task.source` 的盘目录；单盘任务可以省略 |
| `disc_type` | 是 | 盘的主要用途：`movie`、`series`、`bonus` 或 `ignore`；`movie`/`series` 盘中仍可识别附加内容 |
| `title` | 否 | 覆盖 META 标题，或将主盘、特典盘和多版本归入同一作品 |
| `edition` | 否 | 同作品、同分辨率存在多个剪辑时使用的版本标签 |
| `processing` | 否 | `copy_remux`、`hardlink_remux` 或 `hardlink_only` |
| `season` | 否 | 仅限 `series`；覆盖季号，`0` 表示 Specials |
| `episode_start` | 否 | 仅限 `series`；指定该盘第一集集号 |

配置不接受 playlist ID、单个花絮路由或输出文件名。技术细节由程序规划，用户在 Emby 派生目录中复核后可自行更名和移动。

季号按“用户配置 > META 明确季标 > 目录明确季标 > 默认第 1 季”确定。自动识别只接受 `Season 2`、`2nd Season`、`第2期`、`第2季`、`シーズン2` 等明确形式；缺少证据时会警告。相同作品、相同季和相同 edition 的多张盘跨盘连续编号；不同季或 edition 各自从 `E01` 开始，组内集号范围重叠会在规划阶段失败。

剧集自动拆分优先使用原盘明确的播放关系。若一个 episode 对应一个完整 PlayItem，则直接拆分；若一个 episode 横跨多个完整 PlayItem，则只在非无缝 Entry Mark 边界成立，并且同作品中其他明确分集提供了稳定时长轮廓，或本盘存在重复的片尾到片头重置结构时才分组。一条完整 M2TS 也可能收录两集或多集：程序先寻找能连续覆盖主标题的独立单集 playlist；没有这种旁证时，才在时长轮廓或重复章节节奏足够明确时按 Entry Mark 章节切分。以上推断都会生成警告并要求审核。

仅有多个时长相近的独立 playlist、但无法证明它们按顺序连续覆盖主标题时，不会按 playlist 编号猜测集数或顺序：本盘主标题作为单集，其余作为 extras 候选，等待用户在派生目录中确认。多角度、`connection_condition=6`、重复 clip 或内容型 SubPath 会阻止派生分集。

### 附加内容

`disc_type` 只描述盘的主要用途，不把内容强制限定为正片、剧集或花絮。`movie` 和 `series` 盘会先确定正片或 episode，再从同一张盘剩余的有效 playlist 中选择附加内容；候选必须与已选内容在语义上不同、不是导航或菜单循环、引用的源 M2TS 均存在，并达到 `extra_min_seconds`（默认 60 秒）。`bonus` 盘不选择正片，而是对全部有效 playlist 使用相同的附加内容筛选和去重规则。由于 BDMV 通常不提供可靠的自然语言花絮名称，extras 仍属于需要人工复核的保守分类；程序不会继续猜测它属于预告、访谈、删减片段或幕后花絮。

规划器会对不足 5 分钟、低置信度且完整覆盖单个 M2TS 的 extras 做轻量内容检查：先用 FFmpeg 扫描全部音轨，以最响音轨为准；只有所有音轨都接近静音或没有音轨时，才抽样检查三个短视频窗口。近乎静音且多数抽样画面静止的任务会增加 `content_review.status = "needs_review"`、证据和顶层警告；它仍保留在计划中，不会自动排除。正常带音乐的 OP/ED 和有语音的广播剧在音频阶段即结束，不做视频抽样。该检查不使用 OCR、不增加 Python 依赖；FFmpeg 不可用或候选不是完整单 M2TS 时安全跳过。

附加内容默认写入作品目录下的 `[settings].extras_folder`（默认 `extras`），而不是写入剧集的 `Season XX` 目录。文件名使用以下稳定格式：

```text
<盘标题> - PL<playlist编号> - <HHMMSS>.<容器>
```

盘标题优先取 BDMV META，缺失时使用盘目录名；配置了 `edition` 时会附加版本标签。若一个 Play-All 被可靠地拆为多个独立附加视频，编号会增加分段后缀，例如 `Bonus Disc - PL00004-P01 - 000042.m2ts`。不同盘生成同名 extras 时不会覆盖已有任务：规划器依次尝试附加盘目录名、盘路径哈希或任务 ID 片段来消除冲突，并在计划的 `output_disambiguation` 字段记录所用方式。

#### 人工整理与 Emby 目录规范

当前版本默认把所有已识别的附加内容统一输出到作品目录下的 `extras/`，不会自动分配到更细的语义目录。推荐的 TOML 配置可通过 `[settings].extras_folder` 把本次任务的统一输出目录改成另一个 Emby 支持的名称，但这只是整批换用一个目录，不会逐条判断内容类别。建议先播放确认内容，再在 Emby 派生目录中人工更名并移动。

Emby 官方支持以下附加内容子目录名：

- `extras`
- `specials`
- `shorts`
- `scenes`
- `featurettes`
- `behind the scenes`
- `deleted scenes`
- `interviews`
- `trailers`

电影附加内容目录必须直接位于电影目录下，不能嵌套。例如应使用 `Movie Name (Year)/behind the scenes/video.m2ts`，不能使用 `Movie Name (Year)/extras/behind the scenes/video.m2ts`。电影正片应先存在于该电影目录中，再加入附加内容，以降低误识别风险。上述不同类型最终都会显示在 Emby 详情页的 Extras 区域。参见 [Emby Movie Naming：Movie extras](https://emby.media/support/articles/Movie-Naming.html#movie-extras)。

剧集附加内容可以放在剧集、季或单集层级的上述目录中。若内容本身是需要作为特殊集参与刮削和播放顺序的正式 Special，应放入剧集下的 `Season 0`、`Season 00` 或 `Specials` 季目录，并命名为 `Series Name S00E01.ext` 等形式；这与普通 Extras 附件不同。参见 [Emby TV Naming：TV extras 与 Specials](https://emby.media/support/articles/TV-Naming.html#tv-extras)。

只移动文件且保留原文件名时，`status` 可以在目标库内有限重定位并复核成品；同时改名后，项目状态仍记录旧名称，`status` 会将旧路径报告为缺失。完成最终人工整理并让 Emby 重新扫描后，可把这部分文件视为由用户维护。硬链接成品可以移动或重命名目录项，但不能原地修改文件内容，否则原 BDMV 中共享的内容也会改变。

### 处理策略

`processing` 配置在 `[[disc]]` 中，对这张盘生成的正片、episode 和 extras 一并生效。程序会先判断每个输出属于哪种情况：

- **完整单 M2TS**：playlist 只使用一个源 M2TS，并完整覆盖其有效时间范围，不需要拼接或裁切；
- **需要重封装**：playlist 使用多个 M2TS、只取某个 M2TS 的局部范围，或包含需要按 playlist 处理的多角度、内容型 SubPath 等结构。普通完整标题由 libbluray 读取；经过规划器和执行器双重验证的分集片段组或章节区间使用 concat stream-copy，避免对父 playlist 做不可靠的中途 seek。

这里的“重封装”是把原有视频、音频和字幕流无损写入新容器，不重新编码画面或声音。它主要消耗磁盘读写，CPU 占用通常远低于转码。

| 模式 | 完整单 M2TS | 需要组合或裁切 | 适合场景 |
|---|---|---|---|
| `copy_remux` | 逐字节复制 | 无损重封装 | 默认且最稳妥；希望 Emby 成品与原盘互相独立 |
| `hardlink_remux` | 优先硬链接，不能链接时复制 | 无损重封装 | 原盘和 Emby 库通常位于同一文件系统，希望节省一对一文件占用的空间 |
| `hardlink_only` | 只允许硬链接 | 不允许处理 | 只接受完全一对一映射，发现任一不适合硬链接的内容就停止该盘 |

#### `copy_remux`：复制，必要时重封装

这是默认模式。完整单 M2TS 会逐字节复制到 Emby 目录；分段或带有效 in/out 裁切的内容才会重封装。所有成品都拥有独立的文件身份，修改或删除成品不会影响原 BDMV，代价是需要为复制和重封装文件准备完整的目标空间。不确定选哪个模式时使用此项。

#### `hardlink_remux`：硬链接，必要时重封装

完整单 M2TS 与目标目录位于同一文件系统时创建硬链接，几乎不额外占用媒体数据空间；不在同一文件系统或运行时无法创建硬链接时，自动降级为逐字节复制。需要组合或裁切的内容仍会生成独立的重封装文件，不会尝试把多个文件“硬链接成一个”。

硬链接成品与源 M2TS 指向同一份文件内容：重命名或删除其中一个目录项不会删除另一项，但对任一文件原地修改都会同时改变另一边看到的内容。应把硬链接成品视为只读媒体；需要完全隔离时使用 `copy_remux`。

#### `hardlink_only`：只接受硬链接

这是严格检查模式，不提供复制或重封装降级。只有确认完整覆盖单个源 M2TS、源与目标位于同一文件系统且硬链接预检成功的任务才能执行。只要同一张盘中有一个正片、episode 或 extras 需要拼接、裁切、重封装，或者源文件缺失、无法解析、无法硬链接，整张盘都会被标记为 `blocked`，不会只执行其中一部分。该模式适合明确要求“不能复制、不能重封装”的目录，不适合作为一般默认值。

文件名不体现 copy、hardlink 或 remux；最终动作、降级原因和校验结果记录在 dry-run/构建结果以及 `.bdmv-emby-state.json` 中。执行前可先检查 dry-run 的 `operation` 字段。

### `[settings]`

所有设置均可省略。

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `extra_min_seconds` | `60` | 附加内容候选最低时长 |
| `container` | `"m2ts"` | 输出容器；推荐 `m2ts`，兼容 `mkv` |
| `extras_folder` | `"extras"` | 本次任务统一使用的 Emby 附加内容目录；可选值见“人工整理与 Emby 目录规范” |
| `remux_backend` | `"auto"` | `auto`、`bluray` 或显式接受限制的 `concat` |
| `copy_boundary_tolerance_seconds` | `0.1` | 判断完整覆盖源 M2TS 的边界误差，可配置范围 0–0.5 秒 |
| `duration_tolerance_seconds` | `2.0` | 成品相对 MPLS 逻辑时长的允许误差，可配置范围 0–5 秒 |
| `minimum_free_space_bytes` | `5368709120` | 目标卷固定最小剩余空间 |
| `free_space_margin_ratio` | `0.05` | 目标文件系统容量预留比例 |
| `batch_space_check` | `true` | 执行前检查整批任务空间 |
| `ffmpeg` | `"ffmpeg"` | FFmpeg 命令或路径 |
| `ffprobe` | `"ffprobe"` | FFprobe 命令或路径 |

`auto` 对普通 playlist 重封装实际要求 FFmpeg 支持 `bluray` protocol，不会静默把完整标题降级为 concat。只有规划器生成且执行器重新验证的 `episode_playitem_group` / `episode_chapter_split` 会强制使用 concat；前者只组合完整源 M2TS，后者只采用 MPLS Entry Mark 章节边界，并仍需通过成品时长、轨道和逐包时间线校验。用户显式设置全局 `concat` 对其他蓝光精确切点和 seamless branching 仍有固有限制，只应在明确接受风险时使用。

## 计划审核与输出

执行前至少检查：

- 顶层 `warnings`、`recognition.main_selection_counts` 和 `recognition.extras_content_analysis`；
- 正片、episode、extras 数量及 `playlist_selection`；
- extras 的 `content_review`；它只表示需要人工确认，不改变 hardlink/copy/remux 操作；
- `playlist_segment`、源 M2TS、in/out 和目标路径；
- dry-run 的最终 `operation` 与预计空间；
- 多版本归属、剧集季号及跨盘集号。

电影与剧集输出示例：

```text
EmbyMovies/
└── Movie Name/
    ├── Movie Name - 1080p.m2ts
    ├── Movie Name - 4K.m2ts
    └── extras/
        └── Bonus Disc - PL00004-P01 - 000042.m2ts

EmbyTV/
└── Series Name/
    ├── Season 01/
    │   ├── Series Name - S01E01 - 1080p.m2ts
    │   └── Series Name - S01E02 - 1080p.m2ts
    └── extras/
        └── Bonus Disc - PL00002 - 000132.m2ts
```

1080p/4K 来自选中正片的实际视频流。无法可靠获取自然语言名称的附加内容保留盘名、playlist/分段编号和时长，构建后可在派生目录中整理。

| 产物 | 用途 |
|---|---|
| `scan.json` | BDMV 与 MPLS 只读扫描结果 |
| `plan.json` | 可审核、可重复执行的构建计划 |
| `*-results.json` | dry-run 或实际构建结果 |
| `.bdmv-emby-state.json` | 目标库中的操作、来源和校验状态 |
| `.bdmv-emby-build.lock` | 可常驻的 OS 独占锁锚点；文件存在不代表正在构建 |
| `.bdmv-emby-work/` | 构建工作目录 |

已有目标文件默认不会重写，而会重新校验并补建或刷新状态；只有 `--overwrite` 会替换同名目标目录项。已有 copy 必须与源 M2TS 的完整 SHA-256 一致；已有 remux 必须同时匹配当前版本的计划指纹、实际 operation/backend 与完整 SHA-256；已有 hardlink 必须仍与源共享文件身份。`status` 会检查缺失、内容变化和硬链接文件身份，并在保留原文件名时有限重定位人工移动的成品：copy/remux 使用文件大小与完整 SHA-256，hardlink 使用与源共享的文件身份；重定位不会把记录中的原盘源路径当作成品，存在异常时返回非零状态码。

## 安全与隐私

- 扫描和规划不写媒体；源 BDMV 仅作为输入打开；
- `destination` 不能位于 `source` 内，所有源和输出都必须留在各自计划根目录；
- `scan/plan --out` 与 `build --results` 必须使用 `.json` 文件名，并会拒绝源目录、整个目标媒体库、输入配置/计划、媒体成品及内部 state/lock/work 路径冲突；媒体输出也不能占用这些内部路径；
- 执行前按每个目标文件系统汇总整批空间，并在单任务开始前再次检查；`hardlink_only` 按整盘预检；
- 成品通过 FFprobe 校验后才从随机隐藏 partial 原子改名；
- 重封装成品还会逐包确认所有视频、音频和字幕轨道存在且时间戳不倒退，并检查音视频轨道的异常空洞和首尾覆盖；
- Ctrl-C 会清理当前 partial、释放构建锁，并在结果中保留已完成、被中断和未运行任务；
- 默认不覆盖现有文件，`--overwrite` 应仅用于已经复核过的目标路径。
- state 和构建锁只接受链接计数为 1 的普通非链接文件，拒绝符号链接、硬链接、junction、reparse point 与 FIFO。

TOML、scan、plan、results 和 state 通常包含绝对路径、目录名和媒体库清单。它们应视为本地隐私数据，不要未经检查上传到公开仓库、Issue 或日志服务。仓库不保留从真实媒体库导出的计划、结果、逐盘映射或专用 ffconcat；本地任务配置与全库结果已加入 `.gitignore`。详见[隐私与安全边界](docs/privacy-and-safety.md)。

硬链接派生库与原 BDMV 位于同一存储介质时不构成备份。确认成品前保留原盘；正式备份应位于独立介质。

## Beta 质量状态

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

当前 148 项测试覆盖 MPLS 边界与时间计算、内容去重与导航排除、低置信度 extras 的静音/静态画面复核提示、单 PlayItem 分集、多 PlayItem 分集、单 M2TS 多集、季号/集号/edition、发行目录标题清洗、跨平台路径、BDMV 结构与链接/特殊文件边界、审计产物冲突、空间与文件身份保护、重封装时长/轨道/时间线校验、锁、中断审计、完整内容哈希及重定位。GitHub Actions 已配置为在 Ubuntu、macOS 和 Windows 的 Python 3.11 上构建 wheel/sdist、安装 wheel 并运行同一测试集，其中 Windows 会实际创建目录 junction 验证 reparse point 防护。

真实数据验证覆盖多张 BDMV、电影主盘、纯特典盘、多盘剧集、一集一个 M2TS、一集跨多个 M2TS、1080p/4K、多段 seamless branching 正片及直接复制/硬链接/重封装。记录见[验证报告](examples/VALIDATION.md)。

Beta 阶段已知边界：

- 自动主标题选择仍需人工审核长候选歧义；
- 同盘多部长片、多剪辑版和复杂多角度/内容型 SubPath 可能需要人工处理；
- 花絮语义名称通常不能从 BDMV 可靠获得；
- 轻量 extras 内容检查只提供复核证据，不使用 OCR，也不会自动删除疑似版权警告或其他系统内容；
- M2TS 对章节和丰富轨道元数据的表达能力有限，章节主要保存在计划和状态中；
- 重新规划导致 job ID 或输出路由变化时，旧状态和旧派生文件不会自动清退；
- 自动 CI 覆盖 macOS/Linux/Windows 的纯代码与小文件回归；真实原盘和 libbluray 构建目前仍主要在 macOS 上验证。

## 项目结构与文档

遵循 Python 打包惯例：仓库名、分发名和命令使用 `bdmv-emby-builder`，代码中的导入包名使用 `bdmv_emby_builder`，源码置于 `src/` 下。

```text
src/
└── bdmv_emby_builder/
    ├── cli.py         # CLI、TOML 与命令调度
    ├── limits.py      # 配置与序列化计划共享的安全上限
    ├── path_safety.py # 跨平台路径身份与写入边界
    ├── scanner.py     # BDMV 发现、META 和 MPLS 扫描
    ├── mpls.py        # MPLS 二进制解析
    ├── planner.py     # 内容识别、边界、命名与计划
    └── builder.py     # copy、hardlink、remux、校验与状态

config.example.toml                  # 人工配置示例
docs/                                # 技术设计与隐私安全
examples/                            # 项目级验证记录
tests/                               # 自动化测试
```

- [技术方案：直接复制与必要时 M2TS 重封装](docs/copy-and-m2ts-remux-strategy.md)
- [隐私与安全边界](docs/privacy-and-safety.md)
- [验证记录](examples/VALIDATION.md)
- [版本变更](CHANGELOG.md)

## 许可证

本项目采用 [MIT License](LICENSE)。

# OurNotes 娱乐插件

OurNotes 三合一娱乐插件：**猜歌、猜卡面、猜曲绘**

## 致谢

本插件的整体架构参考了 [nichinichisou0609/astrbot_plugin_pjsk_guess_song](https://github.com/nichinichisou0609/astrbot_plugin_pjsk_guess_song)

## 功能

- **猜歌**：随机截取歌曲片段，展示 12 个曲绘选项，玩家发送编号作答。支持普通、2 倍速、倒放、随机组合四种模式。
- **猜卡面**：成员卡与支援卡统一卡池（仅 SR 及以上稀有度），对卡面施加模糊、分块、切割、长条等效果，猜出卡面上的角色。支持官方中文名、短名、英文名、罗马音缩写与常用别名。
- **猜曲绘**：对歌曲曲绘施加模糊/裁切/分块效果，猜出歌名。
- **绑定系统**：QQ 官方机器人可绑定到普通 QQ 号，三个游戏的历史分数一起迁移；每个 QQ 最多绑定两个官机。
- **排行榜**：猜歌、猜卡面、猜曲绘三个排行榜相互独立，各自单独查看，沿用横向表格 UI。
- **官方机按钮**：结算消息带 `qqbot-cmd-input` 快捷指令按钮，按钮文字不带 `on` 前缀；只显示当前游戏的查分与排行榜，绑定与解绑固定位于最底部。
- **资源缓存**：资源只下载一次，后台预热曲绘与完整卡面，减少开局等待。
- **不提供自动模式**。

## 指令

所有指令都支持 `/` 前缀（如 `/猜歌`、`/on猜歌`），答题同样支持（如 `/1`、`/高松灯`、`/确认`）。
`/`、`／`、`!`、`！` 四种前缀都会被自动忽略，带不带前缀效果一致。

游戏进行中可发送 `退出本局` 提前结束。

### 开始游戏

| 指令 | 说明 |
| --- | --- |
| `on猜歌` / `猜歌` | 普通猜歌 |
| `on猜歌 1` / `2倍速猜歌` | 2 倍速猜歌 |
| `on猜歌 2` / `倒放猜歌` | 倒放猜歌 |
| `随机猜歌` | 随机效果组合 |
| `on猜卡面` / `猜卡面` / `猜卡` | 猜卡面（卡牌 + 支援卡） |
| `on猜曲绘` / `猜曲绘` / `猜封面` | 猜曲绘 |

### 账号与数据

| 指令 | 说明 |
| --- | --- |
| `on分数` | 查看总分与三个游戏的详细数据 |
| `猜歌分数` / `猜卡面分数` / `猜曲绘分数` | 查看单个游戏的分数 |
| `猜歌排行榜` | 猜歌排行榜 |
| `猜卡面排行榜` | 猜卡面排行榜 |
| `猜曲绘排行榜` | 猜曲绘排行榜 |
| `on绑定 QQ号` | 官机绑定到普通 QQ 号（需发送“确认”） |
| `on解绑` | 解除官机绑定（需发送“确认”） |
| `on自定义名称 [名称]` | 设置排行榜显示名（不带参数则清除） |

### 管理员

| 指令 | 说明 |
| --- | --- |
| `on刷新题库` | 立即检查 master 数据更新 |
| `on题库状态` | 查看题库数量与资源源 |
| `on重置次数 [QQ号]` | 重置今日游戏次数 |

## 数据与资源

### master 数据

数据源为 `StarMoe-org/moenotes-masterdata` 的 `hk-tw-mo` 快照，仅拉取游玩所需的最小集合：

`MasterLiveMusic`、`MasterMemberCard`、`MasterSupportCard`、`MasterCharacter`、`MasterBand`、`MasterSound`、`MasterSoundCueSheet`、`MasterText`

- 默认每 24 小时检查一次（`update_interval_hours` 可改）；先读很小的 `current_version.json`，版本未变不会重新下载大文件。
- 下载优先 jsDelivr CDN，失败回退 GitHub raw / gitmirror。
- 原始文件与派生题库都会落盘，游戏运行时只读派生数据；派生规则变化时自动用本地原始文件离线重建。

### 资源

资源源默认 `https://assets.bdon.moe/zh-Hans`（`asset_base_url` + `asset_language` 可改）：

| 类型 | 路径 |
| --- | --- |
| 成员卡完整图（题目与结算） | `MemberCard/{assetId}/member_full/member_full.webp` |
| 支援卡完整图（题目与结算） | `SupportCard/{assetId}/snap_full/snap_full.webp` |
| 曲绘 | `Image/Jacket/{jacketAsset}/{jacketAsset}.webp` |
| 音频 | `Cri/Sound/{cueSheet}/{cue}.m4a` |

资源站使用 Cloudflare，首次回源可能较慢。插件采用两层缓解：启动后后台低并发预热全部曲绘与完整卡面、磁盘缓存 + 连接池复用。音频截取使用 ffmpeg（优先系统安装，其次 `imageio-ffmpeg` 自带二进制），不可用时回退 pydub。

## 猜卡面卡池

- 成员卡与支援卡统一进入猜卡面池。
- 只保留 **SR 及以上**稀有度（SR / SSR / EX / BD），R 卡被排除。
- 一张支援卡上有多位角色时，答出其中任意一位即可；结算时会列出全部正确角色名。

## 答案判定

### 歌曲标题

- **只使用 master 数据中的官方本地化标题**。某个地区没有官方中文名时，保留原歌名，不自行翻译、不编造中文译名。
- master 数据中没有独立的歌曲别名库，因此插件**不内置自编歌名别名**。可接受的答案来自：
  - master 官方简中、繁中、日文、英文标题；
  - 英文标题按空格/连字符拆分后的单词（4 个字符以上，如 `Mayoiuta`、`Charles`）；
  - master 的假名读音；
  - 官方中文标题的拼音全拼与首字母（`pypinyin`，仅用于匹配，不显示）；
  - 对以上名称的模糊匹配（错字、漏字、顺序差异等）。
- 判定先做精确匹配；精确命中其他歌曲时，不会被模糊匹配算到当前歌曲。

### 角色名

可接受的答案来自：

- master 官方名称、短名、繁中名、日文名；
- 官方英文名，以及 `Doloris / 三角初华` 这类官方组合名按 `/` 拆分后的两部分；
- 官方英文“姓 名”再拆成姓/名，只接受 3 个字符以上的部分（如 `Tomori`、`Uika`）；
- `res/character_aliases.json` 中的人工维护别名（见下）。

## 角色别名库

`res/character_aliases.json` 是一份面向中文玩家的人工维护别名表，键为角色 ID，值为可接受的答案列表。收录三类：

1. **中文圈常用写法**，例如长崎爽世也常写作「长崎素世」，因此 `长崎素世` 与 `素世` 都算对；
2. **官方英文名的罗马音形式**，包括全拼（`soyo`）与更短的缩写（`tmr`、`drls`、`mtsm`、`knt`）；
3. **社区常用昵称**，例如 `soyorin`。

规则与边界：

- 主要面向中文用户，**不收录日文写法**——日文名由 master 数据直接提供。
- 英文别名至少 3 个字符，过短的缩写（如 2 个字母）会被忽略，避免误判。
- 中文/日文短名可以是 1–2 个字（如「灯」「睦」）。
- 文件缺失或解析失败时插件仍可正常运行，只是少一层别名匹配。

新增角色时，在 `characters` 下按角色 ID 增加一行即可，无需改动代码。

## 猜歌选项图

猜歌的 12 个选项使用两列卡片布局：

- 顶部：`请发送正确答案 (1-12)`；
- 左侧：编号（主题色）、官方标题（黑色）、乐队名（灰色小字）；
- 右侧：圆角曲绘封面。

小字统一显示**乐队名**，并与黑色标题左对齐（即与标题同一 x 坐标，而不是与编号对齐）。

## 配置

配置文件由 AstrBot 的 `_conf_schema.json` 生成，界面为中文。常用项：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `update_interval_hours` | 24 | 题库更新检查间隔（小时） |
| `request_timeout` | 60 | 单次请求超时（秒） |
| `download_concurrency` | 4 | 同时下载连接数，建议 3–6 |
| `download_retries` | 3 | 超时/429/5xx 的重试次数（指数退避） |
| `asset_base_url` | `https://assets.bdon.moe` | 资源站根地址 |
| `asset_language` | `zh-Hans` | 卡面/曲绘文字语言 |
| `prefetch_assets` | `true` | 启动后后台预热曲绘与完整卡面 |
| `prefetch_concurrency` | 3 | 预热并发数 |
| `asset_cache_max_mb` | 1024 | 资源缓存上限，0 为不限制 |
| `connect_link_template` | qqbot 模板 | 官机按钮模板 |
| `quick_entries` | 猜歌/猜卡面/猜曲绘 | 结算消息快捷入口（官机按钮无需加 `on`） |
| `answer_timeout` | 30 | 每轮答题时间（秒） |
| `daily_play_limit` | 10 | 每日次数，`-1` 为无限制 |
| `daily_limit_per_game` | `false` | 三个游戏是否各自计算次数 |
| `game_cooldown_seconds` | 10 | 同一会话两局之间的冷却 |
| `max_guess_attempts` | 10 | 一轮内所有玩家的总猜测上限，0 为不限制 |
| `song_guesses_per_user` | 2 | 猜歌中每位玩家的作答次数 |
| `reward_valid_time` | 5 | 第一个答对后的额外得分窗口 |
| `clip_duration_seconds` | 10 | 歌曲片段长度 |
| `clip_start_ratio_min` / `max` | 0.08 / 0.72 | 片段起始位置范围 |
| `song_2x_score` | 1 | 2 倍速模式得分 |
| `song_reverse_score` | 1 | 倒放模式得分 |
| `random_mode_decay_factor` | 0.75 | 随机模式权重衰减系数 |
| `ranking_display_count` | 10 | 排行榜显示人数 |
| `card_effects` / `jacket_effects` | `{}` | 图片效果的启用、分数与参数 |
| `super_users` | `[]` | 管理员 ID |
| `blacklist` | `[]` | 拉黑用户 |
| `group_whitelist` | `[]` | 群白名单，留空允许所有群 |
| `whitelist_reject_message` | 提示文案 | 白名单拒绝提示 |
| `debug_mode` | `false` | 跳过冷却与次数，开局后立即公布答案 |

## 依赖

```
Pillow>=9.0.0
pilmoji
aiohttp>=3.8
pydub
imageio-ffmpeg
pypinyin
```

`imageio-ffmpeg` 会提供可用的 ffmpeg 二进制；系统已安装 ffmpeg/ffprobe 时优先使用系统版本。
`pypinyin` 用于拼音匹配，未安装时插件仍可运行，只是不支持拼音输入。

## 测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests -q
```

测试覆盖 master 数据解析、资源下载与缓存、图片效果、选项图/排行榜渲染、音频截取（普通/倍速/倒放）、数据库统计与绑定合并、角色别名库，以及三款游戏的完整流程（含正确答案计分与答案图发送）。

## 注意

- 如果服务器里仍安装着其他猜歌/猜卡面/猜曲绘插件，请先禁用或移除，避免指令冲突。
- 猜卡面卡池只含 SR 以上卡牌；R 卡不会出现。
- 首次未缓存的资源需要从 Cloudflare 回源，可能比之后慢；插件会在后台预热曲绘与完整卡面。

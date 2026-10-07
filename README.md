# 明日方舟剧情速查（arknights-lore）

MaiBot 第三方插件。检索《明日方舟》剧情原文，交由 LLM 整理成自然回答。

## 关联上游项目

| 项目 | 说明 |
|---|---|
| [ArknightsSearch/ArknightsSearch-backend](https://github.com/ArknightsSearch/ArknightsSearch-backend) | 剧情检索后端（本插件的远程模式接入它） |
| [ArknightsSearch/ArknightsSearch-resource](https://github.com/ArknightsSearch/ArknightsSearch-resource) | 语料构建产物（8 个 JSON，47 MB，国服/简中） |
| [Kengxxiao/ArknightsGameData](https://github.com/Kengxxiao/ArknightsGameData) | 游戏原始数据（剧情文本来源） |
| [Arkfans/ArknightsAlias](https://github.com/Arkfans/ArknightsAlias) | 角色/地区别名库（jieba 预筛用） |

后端与语料仓库均以 MIT 许可证发布；游戏文本版权归 Hypergryph 所有。

剧情库有两种接法，二选一：

- **接入 ArkSearch 服务**（默认）：另起一个 ArkSearch 后端进程，插件走 HTTP 调用
- **插件自建**：插件自己下载语料并在进程内检索，不依赖任何外部服务

## 前置

选「接入 ArkSearch 服务」时需要 ArkSearch 后端**单独运行**（不依赖 MaiBot 启停）：

```powershell
<arksearch-dir>\start-arksearch.bat
```

检查是否存活：

```powershell
<arksearch-dir>\check-arksearch.bat
```

守护（掉了自动拉起，在独立窗口跑，或用计划任务开机启动）：

```powershell
powershell -ExecutionPolicy Bypass -File <arksearch-dir>\watchdog-arksearch.ps1
```

插件在 ArkSearch 不可用时会返回「剧情检索服务不可用」并记日志，不会拖垮麦麦。

### 数据升级

ArkSearch 启动时会静默比对本地数据与上游（`ArknightsSearch/ArknightsSearch-resource`，只有国服/简中一套产物）。
**无差异或网络不通一律不出声**；只有检测到过期才在控制台提示。

升级（自动识别本机代理：环境变量 → Windows 系统代理 → 常见端口扫描）：

```powershell
cd <arksearch-dir>\backend
python upgrade_data.py            # 比对 -> 备份 -> 下载 -> 校验 -> 写入
python upgrade_data.py --check    # 只比对不下载
python upgrade_data.py --no-proxy # 强制直连
```

下载走 codeload 整仓快照（约 13 MB），不消耗 GitHub API 限额；写入前会校验 JSON 并自动备份到 `data\story.bak-<时间戳>`。**升级后需重启 ArkSearch 才生效。**

## 安装

把本目录放进 MaiBot 的 `plugins\` 下，然后 WebUI → 插件 → 加载，或重启 MaiBot Core。

## 配置

WebUI → 插件 → 明日方舟剧情速查，四块：

### 模型来源 `model.source`

| 值 | 行为 |
|---|---|
| `maibot replyer` / `planner` / `utils` / `vlm` / `embedding` | 复用 MaiBot「模型管理」里**同名**分类已配好的模型 |
| 自定义端点 | 用下面自填的端点，插件自己管超时/重试/降级 |
| 不总结（只返原文） | 不调用模型，直接返回检索到的原文 |

> 前五项**故意保留 MaiBot 的原名不翻译**——你在模型管理页那边看到的也是 `replyer` / `utils`，
> 译成「回复器」「工具模型」反而对不上号。只有插件自己的概念（自定义端点、不总结）用中文。

> **优先选 `maibot utils`。** 剧情总结是「概括整理小任务」，`planner` 是规划模型又大又慢，实测能把一次问答拖到 15 秒。
> `vlm` / `embedding` 是视觉/向量模型，不适合文本生成。
> 界面上是 `maibot utils` 这种带前缀的写法，插件调用 MaiBot 时会剥掉前缀传 `utils`，
> 宿主的 API 只认英文。
> 旧配置的 `source="maibot"` + `maibot_task="xxx"`、单选字符串、以及英文枚举值，
> 加载时都会自动迁移成新的多选列表。

> ⚠️ **别把来源指向 `openrouter/free` 这类免费轮盘。** `/free` 是「随便扔个免费模型过来」的路由，
> 随时可能路由到推理模型——实测命中过 `nvidia/nemotron-3.5-lightning`，
> 它会先写一整段英文思维链，`max_tokens` 不够就在思维链中间被截断，
> **正文一个字都没输出**，插件拿到的「答案」其实是半截思考过程。
>
> 插件已做输出净化兜底（见下），但固定指定一个显式关掉 `thinking` 的模型更快也更稳。
> 你配置里现成就有：`deepseek-v4-pro-nonthink`、`sensenova-6.8-flash-lite`、`ds4flash`、`mimo-v2.6-flash`。

### 检索服务 `retrieval`

「剧情库来源」和「模型来源」都是\*\*可多选的有序回退链\*\*：按列表顺序（可拖动调整）依次尝试，
前一个不可用（连不上、没数据、模型失败）就自动降级到下一个。两个都勾上最稳。

#### 剧情库来源 `retrieval.provider`

| 选项 | 行为 |
|---|---|
| 接入 ArkSearch 服务 | 用「服务地址」指向本机或远程已部署的后端（`http://127.0.0.1:48910`）。只是 URL 的区别 |
| 插件自建 | 用插件目录下自带的语料在进程内检索，不依赖外部服务 |

> 默认只勾「接入 ArkSearch 服务」且**服务地址留空**，表示「还没配置」。此时插件会明确提示去填。
> 想要开箱即用，就**同时勾上「插件自建」**——服务没起或地址没填时会自动走自带语料。

> **为什么枚举值是中文？** MaiBot 当前的 WebUI 渲染选项时写的是 `String(枚举值)`
> （单选下拉和多选复选框都是），把值直接当标签显示；它的翻译函数只对
> `label` / `placeholder` / `hint` / `title` / `description` / `name` 生效，
> **从来没有 `choices` 这个键**。SDK 会生成 `i18n.choices`，但前端丢弃不用。
>
> 所以想让界面上显示中文，值本身就得是中文。代价是 `config.toml` 里会出现中文。
> 早期英文写法（`remote` / `builtin` / `utils` / `auto` 等）加载时会自动迁移，不用手改。

#### 语料存放位置 `retrieval.data_dir`

留空则用**插件目录下的 `data` 子文件夹**（推荐：语料跟着插件走，整包迁移、备份、
重装都不会丢）。填相对路径按插件目录解析，填绝对路径则直接用该路径。

#### 语料维护

| 配置项 | 默认 | 说明 |
|---|---|---|
| `repo` | `ArknightsSearch/ArknightsSearch-resource` | 语料仓库（源项目 CI 产出的构建成品） |
| `branch` | `main` | 分支 |
| `proxy` | 自动探测 | 先试直连、不通再找系统代理；也可强制直连 |
| `auto_update` | `false` | 开启后每次加载插件检查一次语料版本（6 小时冷却），有新版后台自动下载 |

| 指令 | 作用 |
|---|---|
| `/arkcheck` | 检查语料是否有新版本，不下载。只读一个几 KB 的 commit feed，**不消耗 GitHub API 的 60 次/小时配额** |
| `/arkupdate` | 下载并部署新语料，完成后立即生效，不需要重启 |

勾上「插件自建」并保存配置时，如果本地还没有语料，会**自动开始构建**，不需要再发一次指令。

语料共 8 个 JSON、47 MB。下载走 `codeload.github.com` 的 zip 快照（13.6 MB），
**先解到临时目录、校验通过再切换目录**，中途失败不会破坏已有的可用数据。

#### 内置引擎

`arkdata.py` 是从 ArknightsSearch 后端搬运的检索引擎（MIT），去掉了 FastAPI 与 `core.*` 依赖，
产出的结构与 `/story` 接口（`require=PC`）**逐字节一致**，所以远程与自建两条路共用同一套降级与拼装逻辑。

和原后端的两点刻意差异：

- **惰性加载**。原后端在 `import` 时就把 47 MB JSON 全载入并建好倒排索引；插件不能这么干——Runner 进程是 MaiBot 的热重载目标，import 就吃掉 75 MB、拖慢每次改配置。这里改成首次检索时才加载，约 0.7 秒。
- **数据缺失不抛异常**。缺文件时 `ensure_loaded()` 返回 `False` 并把原因写进 `detail`，而不是让整个插件崩掉。

已用 60 组随机参数（含 char / text / regex / zone 及组合）与后端接口做过逐字段回归，**不一致 0**。

### 模型来源 `model.source`

同样是多选回退链，按列表顺序（可拖动调整）依次尝试。

| 选项 | 行为 |
|---|---|
| `maibot replyer` / `planner` / `utils` / `vlm` / `embedding` | 复用 MaiBot「模型管理」里**同名**分类已配好的模型 |
| 自定义端点 | 用「自定义端点」里自己填的端点，插件自己管超时/重试/降级 |
| 不总结（只返原文） | 不调用模型，直接返回检索到的原文 |

> 前五项**故意保留 MaiBot 的原名不翻译**——你在模型管理页那边看到的也是 `replyer` / `utils`，
> 译成「回复器」「工具模型」反而对不上号。只有插件自己的概念（自定义端点、不总结）用中文。

> **默认只勾 `maibot utils`。** 剧情总结是「概括整理小任务」，`planner` 是规划模型又大又慢，实测能把一次问答拖到 15 秒。
> `vlm` / `embedding` 不适合文本生成。
> 界面上是 `maibot utils` 这种带前缀的写法，插件调用 MaiBot 时会剥掉前缀传 `utils`，
> 宿主的 API 只认英文。
> 旧配置的 `source="maibot"` + `maibot_task="xxx"`、单选字符串、以及英文枚举值，
> 加载时都会自动迁移成新的多选列表。

#### 回退链是怎么跑的

**顺序调用，不是随机。** WebUI 里那个多选控件不是复选框网格：勾选的项会变成一排**可拖动的标签**，
点选按先后追加到末尾，也能**直接拖动调整顺序**。插件严格按这个顺序逐个尝试：

1. 调第一个来源；
2. 结果**不可用就换下一个**——不可用包括两种：没输出，或者输出被净化器判为无效
   （一整段英文思维链、`User Safety: safe` 这类安全判定串）；
3. 拿到可用输出就停，不会再调后面的；
4. 全部不可用才退回原文（`fail_open`）。

第 2 点是关键。免费轮盘抽到推理模型时，返回的恰恰是**非空的**思维链——
如果只看「有没有返回东西」就停下，链条会当场断掉，后面的来源永远轮不上，
而思维链污染正是这条链要解决的场景。

时间预算不够时会跳过剩下的来源，不让用户干等整条链依次超时。

> ⚠️ **别把来源指向 `openrouter/free` 这类免费轮盘。** `/free` 是「随便扔个免费模型过来」的路由，
> 随时可能路由到推理模型——实测命中过 `nvidia/nemotron-3.5-lightning`，
> 它会先写一整段英文思维链，`max_tokens` 不够就在思维链中间被截断，
> **正文一个字都没输出**，插件拿到的「答案」其实是半截思考过程。
>
> 推荐勾【maibot utils + 自定义端点】：前者挂了自动落到后者，不会因为轮盘抽到坏模型就整段失忆。
> 你配置里现成就有非推理的模型：`deepseek-v4-pro-nonthink`、`sensenova-6.8-flash-lite`、`ds4flash`、`mimo-v2.6-flash`。

### 自定义端点 `model.endpoints`

来源里勾了「自定义端点」时使用。可添加多个，每个字段：

- `name` — 标识，用于日志和熔断统计
- `base_url` — OpenAI 兼容根地址，如 `https://xxx.com/v1`
- `api_key` — 留空表示免鉴权
- `model` — 模型名
- `timeout` — 单次请求超时（秒）
- `cooldown` — 连续失败后的熔断冷却（秒）

填两个以上即获得自动轮转 + 故障降级。4xx（鉴权/模型名/额度）不重试，超时/连接错/5xx 才重试。

### 别名白名单预筛 `retrieval.use_jieba`

插件启动时会读取 **12078 条角色/地区别名**，构造一个**只含别名、不加载 jieba 默认词表**的分词器，用它做检索词预筛。

- 不用默认词表的原因：它是 2017 年通用语料，明日方舟 2019 年才上线，「凯尔希」会被切成「凯尔 / 希在」，切碎的实体反而成了噪音
- 白名单只认别名表里的实体词，滤掉「看着」「什么」这类功能词——功能词能命中 900~2000 篇，是纯噪音
- **台词类问题不受影响**：白名单为空时自动退回原有的盲拆词 + LLM 提炼逻辑

| 配置项 | 默认 | 说明 |
|---|---|---|
| `alias_data_dir` | 空 | 别名表目录。留空则依次尝试：`ARKSEARCH_DATA` 环境变量 → 语料目录 → 插件目录下的 `data` → 同级 `arksearch/backend/data/story` |
| `use_jieba` | `true` | 关掉就退回盲拆词 |
| `jieba_max_terms` | `4` | 一次最多取几个白名单词 |

> 默认会用**插件目录下 `data/` 里自带的语料**，所以纯自建模式下不需要额外配置任何路径。
> 显式填了路径却找不到时**不会**偷偷回退到猜测目录，只记一条 WARNING——避免「我明明指定了却读的是别处」。

实测（真实语料，同一问题）：

| 问题 | 关闭预筛 | 开启预筛 |
|---|---|---|
| 凯尔希在孤星里说了什么 | 684 字 / 484ms | **825 字 / 63ms** |
| 可露希尔的密录里有没有讲阿罗巴尼的事 | **0 字** | **522 字 / 78ms** |
| 魏彦吾那句我会一直看着你们是哪段剧情 | **0 字** | **617 字 / 63ms** |
| 今天天气不错我们吃什么 | 242 字 | 242 字（无实体，原样回退） |


### 自定义端点 `model.endpoints`

来源选 `plugin` 时使用。可添加多个，每个字段：

- `name` — 标识，用于日志和熔断统计
- `base_url` — OpenAI 兼容根地址，如 `https://xxx.com/v1`
- `api_key` — 留空表示免鉴权
- `model` — 模型名
- `timeout` — 单次请求超时（秒）
- `cooldown` — 连续失败后的熔断冷却（秒）

填两个以上即获得自动轮转 + 故障降级。4xx（鉴权/模型名/额度）不重试，超时/连接错/5xx 才重试。

### 模型输出净化

插件不信任任何模型输出，送进聊天窗口前一律过一遍净化：

| 拦截对象 | 例子 | 处理 |
|---|---|---|
| 成对思维链标签 | `<thinking>…</thinking>` | 剥掉标签内容 |
| 思维链口头禅 | `Here's a thinking process:`、`Let me think…`、`1. **Analyze**` | 有「最终答案 / Answer:」就从后面抢救正文，否则整段丢弃 |
| 安全判定串 | `User Safety: safe`、`Safety Categories: …` | 整段丢弃 |
| 无中文的英文长文本 | `Based on the provided context, …` | 整段丢弃（这是英文思维链的特征） |
| 超长输出 | — | 截断到上限 |

判定为不可用时返回空串，由 `fail_open` **退回检索到的原文**——原文对用户永远有用，
半截思考过程只会让人困惑。日志里会留一条 WARNING：

```
[WARNING] 模型输出被判定为不可用（多半是推理模型的思维链或安全判定串），退回原文。开头：'Here's a thinking process:\n\n1.  **Analyze User Input:**…'
```


### 各级超时

| 配置项 | 默认 | 作用范围 |
|---|---|---|
| `retrieval.timeout` | 10s | 调 ArkSearch 后端 |
| `endpoints[].timeout` | 25s | 单个模型端点单次请求 |
| `resilience.total_budget` | 45s | 整个工具调用的总预算（含所有重试），超时即放弃并走兜底 |
| `max_attempts` | 4 | 单次生成最多试几个端点 |

三级从外到内递减，最外层 `total_budget` 保证不会卡住 MaiBot 的事件循环。

## 用法

- **LLM 自主调用**：`arknights_lore` 工具，麦麦判断该查时会调
- **手动查询**：`/ark 凯尔希`，直接走插件自己的检索+总结，不经过麦麦的人格化层

工具参数：`query`（必填）、`char_name`（已知角色名时传，检索更准）、`question`（用户原话，传了回答更贴合）

## 日志

插件目录下 `arknights-lore.log`，只记录本插件，滚动保存最多 500 条，进程重启后保留尾部历史。

```
2026-10-04 13:52:58 [WARNING] 端点 xxx 连续失败 2 次，熔断 60s
2026-10-04 13:53:01 [WARNING] 端点池全部失败，最后一个错误：HTTP 500
2026-10-04 14:02:11 [INFO] 完成 query='凯尔希' char='' 原文900字 总结成功 用时2.31s
2026-10-04 14:28:42 [WARNING] 模型输出被判定为不可用（多半是推理模型的思维链或安全判定串），退回原文。开头：'Here's a thinking process:\n\n1.  **Analyze User Input:**…'
```

## 自测

```powershell
# 主运行时（MaiBot 的解释器，有 jieba）
& "$env:APPDATA\MaiBotOneKeyDesktop\af385031c204\python-env\python.exe" selftest.py

# 降级路径（无 jieba，应自动退回盲拆词且全部通过）
# 用任意无 jieba 的 Python 3.10+ 即可
python selftest.py
```

会起本地故障注入服务模拟 500/503/超时，验证降级链路。需要 ArkSearch 在跑。
两个解释器下都要全绿（T13/T14 会按有无 jieba 自动分支断言）。

当前覆盖：T1–T12 检索与命令链路，T13/T14 别名预筛与降级，T15 输出净化（用真实泄漏文本做端到端拦截），**T16 多选回退链**（选项汉化、两代旧配置迁移、schema 断言），T17 内置引擎（真实语料）、T18 数据来源切换与降级、T19 语料下载与版本探测。

## 已知行为

- 检索参数**从窄到宽逐级放宽**：角色+关键词交集 → 只按角色 → 别名白名单词 → 原句/拆词 → 只按关键词。因为 ArkSearch 多参数取交集，直接同时传经常归零（`char=凯尔希` 有 199 篇，`text=孤星` 只有 1 篇，交集为 0）
- **`text` 模式要求字面连续子串**，自然语言问题直接返回 0，所以主检索用 `regex`（纯字面匹配、不过滤说话人、结果还更全）
- **空检索结果不进缓存**，避免语料更新后新内容被旧缓存挡住
- 模型全挂时 `fail_open` 默认开启，把原文片段直接返回而不是报错
- 未知角色名会让 ArkSearch 直接 **HTTP 500**（`char_name2id` 抛 KeyError）。插件按级捕获并继续降级，不会误报成「服务不可用」
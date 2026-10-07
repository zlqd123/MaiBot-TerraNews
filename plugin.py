"""明日方舟剧情速查插件。

工作流：ArkSearch 本地后端检索剧情原文 -> 按预算裁剪上下文 -> LLM 整理成自然回答。

LLM 来源通过配置 ``model.source`` 选择——它是**多选的有序回退链**，按勾选顺序
依次尝试，前一个失败自动降级到下一个：

- ``replyer`` / ``planner`` / ``utils`` / ``vlm`` / ``embedding``：复用 MaiBot 模型管理页里
  对应预设分类已配好的模型，走 ``ctx.llm.generate``。剧情总结属于概括整理小任务，
  一般选 ``utils``。
- ``plugin``：使用插件自填的端点，插件自己控制超时、重试与端点降级。
- ``none``：只返回检索到的原文片段，不做总结。

剧情库来源 ``retrieval.provider`` 同理，也是多选回退链（``remote`` 接外部服务 /
``builtin`` 用插件自带的语料）。

免费模型池（尤其 OpenRouter 的 ``/free`` 轮盘）不稳定，可能路由到推理模型并把
思维链当正文输出，所以模型返回的文本一律先过 :func:`sanitize_model_output` 净化。

只依赖标准库与 pydantic，不引入 httpx 等第三方包，避免与主程序依赖冲突。
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Literal, Optional, Sequence, Set

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

try:
    from pydantic import model_validator
except ImportError:  # 极老的运行环境没有 pydantic v2，配置迁移降级为不迁移
    def model_validator(*_args: Any, **_kwargs: Any) -> Any:  # type: ignore[misc]
        def _noop(fn: Any) -> Any:
            return fn

        return _noop

# --------------------------------------------------------------------------- #
# 同级模块的导入
#
# MaiBot 加载插件时（plugin_runtime/runner/plugin_loader.py）只用
# spec_from_file_location 把 plugin.py 当成一个独立模块执行，并且临时加进
# sys.path 的是**插件的上级目录**（plugins/），插件目录本身不在里面。
# 所以同级的 arkdata / arkdownload 直接 import 会 ModuleNotFoundError——
# 文件明明躺在旁边也找不到。必须自己把插件目录补进 sys.path。
#
# 注意：只丢同目录的缓存。别的插件若也叫 arkdata，不该被我们连坐；
# 同目录的旧缓存则必须丢，否则改完 arkdata.py 热重载时读到的还是旧代码。
# --------------------------------------------------------------------------- #
_PLUGIN_DIR = Path(__file__).resolve().parent

for _helper in ("arkdata", "arkdownload"):
    _cached = sys.modules.get(_helper)
    _cached_file = getattr(_cached, "__file__", None)
    if _cached_file:
        try:
            if Path(_cached_file).resolve().parent == _PLUGIN_DIR:
                del sys.modules[_helper]
        except OSError:  # pragma: no cover - 路径拿不到就保守起见不清
            pass

if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from arkdata import ArkData, ParamRejected  # noqa: E402
from arkdownload import DEFAULT_BRANCH, DEFAULT_REPO, detect_proxy, download_story, fetch_latest_commit, plan_update, write_manifest  # noqa: E402


# ArkSearch 的 /story 返回中，第 5 个元素是命中片段（EXTRA 位）。
_EXTRA_INDEX = 4
# 插件日志最多保留的行数，超出后丢弃最旧的。
_LOG_MAX_LINES = 500
# 插件日志文件名，写在插件目录下。
_LOG_FILENAME = "arknights-lore.log"

# 插件自带语料的默认子目录名，放在插件目录下，便于整包迁移和备份
DEFAULT_DATA_SUBDIR = "data"


# --------------------------------------------------------------------------- #
# 模型输出净化
# --------------------------------------------------------------------------- #
# 免费模型池（尤其 OpenRouter 的 /free 轮盘）会路由到推理模型。推理模型拿到请求
# 先写一整段思维链，如果 max_tokens 太小，思维链就把预算吃光，正文根本没输出——
# 插件拿到的「答案」其实是半截思考过程，直接发到聊天窗口就是「Here's a thinking
# process: ...」。同一个轮盘也会路由到内容审核模型，吐出 `User Safety: safe`
# 这种安全判定串。两者都不是回答，必须拦下来退回原文。

# 成对的思维链标签：<thinking>...</thinking>、<think>...</think> 等
_COT_TAG_RE = re.compile(r"<(thinking|think|reasoning|scratchpad)>.*?</\1\s*>", re.S | re.I)
# 思维链的口头禅/结构标记，命中即认为模型在自言自语而不是在回答
_COT_PREAMBLE_RE = re.compile(
    r"(here'?s\s+(is\s+)?(a\s+)?thinking\s+process"
    # 「Let's think」和「Let me think」两种写法都要覆盖
    r"|let(?:'?s|\s+me)\s+think(\s+step\s+by\s+step)?"
    r"|i'?ll\s+(think|reason|analyz)"
    r"|(thinking|reasoning)\s+(process|steps?)"
    r"|^\s*step\s*\d+\s*[:.]"
    r"|^\s*\d+\.\s*\*?\*?(analyze|identify|check|consider|formulate|verify)\b)"
    ,
    re.I | re.M,
)
# 安全判定串：内容审核类模型的固定输出格式
_SAFETY_VERDICT_RE = re.compile(
    r"(user\s+safety\s*:|response\s+safety\s*:|safety\s+categories\s*:"
    r"|content\s+polic(y|ies)\s*:|i'?m\s+not\s+able\s+to\s+comply)",
    re.I,
)
# 思维链之后真正的答案可能被放在这些标记之后，能定位到就抢救出来。
# 标记把紧跟的冒号一并吃掉，避免分出来还带着个孤零零的「：」。
_ANSWER_TAIL_RE = re.compile(
    r"(最终答案|最终回答|最终输出|所以答案|回答|答案|final\s+answer|answer)"
    r"\s*[:：]?\s*",
    re.I,
)
# 中日韩统一表意文字，用来判断输出是不是中文
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
# 拉丁字母，用来判断是不是泄漏进来的英文思维链
_LATIN_RE = re.compile(r"[A-Za-z]")


def sanitize_model_output(text: str, *, max_chars: int = 400, require_chinese: bool = True) -> str:
    """过滤模型输出里混进来的思维链、安全判定串与超长内容。

    宁可返回空串（让调用方退回检索到的原文），也不要放思维链去聊天窗口——
    原文对用户永远有用，半截思考过程只会让人困惑。

    Args:
        text: 模型原始输出。
        max_chars: 保留的最大字符数，超出直接截断。
        require_chinese: 是否要求输出含中文。英文思维链正是这样被识别的。

    Returns:
        净化后的文本；判定为不可用时返回空串。
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return ""

    # 1. 安全判定串：内容审核模型的输出，一句话就能定性
    if _SAFETY_VERDICT_RE.search(cleaned):
        return ""

    # 2. 成对标签包起来的思维链：整体剥掉
    stripped = _COT_TAG_RE.sub("", cleaned).strip()
    if stripped:
        cleaned = stripped

    # 3. 口头禅式思维链：尝试从「最终答案」标记之后抢救正文，抢救不到就丢弃
    if _COT_PREAMBLE_RE.search(cleaned):
        tails = _ANSWER_TAIL_RE.split(cleaned)
        tail = tails[-1].strip().lstrip("：:，,。.、-— ") if len(tails) > 1 else ""
        # 尾部太短说明截断发生在思维链中间，正文压根没生成
        if len(tail) < 2:
            return ""
        cleaned = tail

    # 4. 语言检查：没有中文却有大量字母，几乎可以肯定是英文思维链
    if require_chinese and not _CJK_RE.search(cleaned) and len(_LATIN_RE.findall(cleaned)) > 20:
        return ""

    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip()
    return cleaned


class PluginFileLog:
    """插件目录下的滚动日志文件。

    只记录本插件自身的日志，最多保留 ``max_lines`` 条。启动时会把文件里已有的
    尾部读进来，这样进程重启后崩溃现场不会丢。
    """

    def __init__(self, path: Path, max_lines: int = _LOG_MAX_LINES) -> None:
        """初始化日志器。

        Args:
            path: 日志文件路径。
            max_lines: 最多保留的行数。
        """
        self._path = path
        self._lock = threading.Lock()
        self._lines: Deque[str] = deque(maxlen=max_lines)
        self._available = True
        self._load()

    def _load(self) -> None:
        """把文件里已有的尾部内容读进内存环形缓冲。"""
        try:
            if self._path.exists():
                existing = self._path.read_text(encoding="utf-8", errors="replace").splitlines()
                self._lines.extend(existing[-self._lines.maxlen:])
        except OSError:
            # 日志不可读不该影响插件功能
            self._available = False

    def write(self, level: str, message: str) -> None:
        """写入一条日志。

        Args:
            level: 日志级别，如 INFO / WARNING。
            message: 日志正文。
        """
        if not self._available:
            return
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {message}"
        with self._lock:
            self._lines.append(line)
            try:
                self._path.write_text("\n".join(self._lines) + "\n", encoding="utf-8")
            except OSError:
                # 磁盘满或文件被占用时停止写文件，但不影响主逻辑
                self._available = False
# 中文粗估：1 个汉字约等于 1.5 个 token，英文约 4 字符 1 个 token。
_CHARS_PER_TOKEN_ZH = 0.7


# --------------------------------------------------------------------------- #
# 配置模型
# --------------------------------------------------------------------------- #
def _lbl(text: str, **extra: Any) -> Dict[str, Any]:
    """生成 Field 的 WebUI 中文标签。

    SDK 的 ``generate_plugin_config_schema`` 写的是
    ``"label": str(json_extra.get("label") or field_name)``——label 必须是**纯字符串**，
    传 ``{"zh_CN": ...}`` 会被 str() 渲染成字典字面量。

    Args:
        text: 界面上显示的中文名。
        **extra: 其余 json_schema_extra 项，例如 ``i18n``。

    Returns:
        可直接传给 Field 的 json_schema_extra。
    """
    return {"label": text, **extra}


def _i18n_choices(mapping: Dict[str, str]) -> Dict[str, Any]:
    """给枚举字段配选项名。

    .. warning::
       **MaiBot 当前的 WebUI 不读这个。** 前端渲染选项时写的是 ``String(choice)``
       （单选下拉 ``s.choices?.map(f=>...children:String(f))`` 和多选复选框
       ``options:s.choices.map(E=>({label:String(E)}))`` 都是），把枚举值直接当标签；
       而它的翻译函数只对 ``label`` / ``placeholder`` / ``hint`` / ``title`` /
       ``description`` / ``name`` 生效，**从来没有 ``choices`` 这个键**。
       SDK 会把 ``i18n.choices`` 生成出来，前端丢弃不用。

       所以枚举值本身就得是中文，见下面 :data:`PROVIDER_REMOTE` 那组的写法。
       这里保留是为了将来 WebUI 支持了就能直接生效。

    Args:
        mapping: ``{选项值: 中文名}``。

    Returns:
        可直接展开进 json_schema_extra 的 i18n 结构。
    """
    return {
        "i18n": {
            "zh_CN": {"choices": dict(mapping)},
            "en_US": {"choices": {key: key for key in mapping}},
        }
    }


def _as_list(
    value: Any,
    allowed: Sequence[str],
    default: Sequence[str],
    legacy: Optional[Dict[str, str]] = None,
) -> List[str]:
    """把配置值归一成「合法且非空的有序列表」。

    兼容旧版的标量写法（当时是单选字符串）与早期英文枚举值，顺带剔除重复项和非法值。

    Args:
        value: 配置里的原始值，可能是标量或列表。
        allowed: 允许的取值。
        default: 归一后为空时使用的默认值。
        legacy: 旧英文值到当前值的映射。

    Returns:
        保持用户选择顺序的去重列表。
    """
    legacy = legacy or {}
    if value is None or value == "":
        return list(default)
    candidates: List[Any] = [value] if isinstance(value, str) else list(value)
    items: List[str] = []
    for item in candidates:
        text = legacy.get(str(item).strip(), str(item).strip())
        if text in allowed and text not in items:
            items.append(text)
    return items or list(default)


# --------------------------------------------------------------------------- #
# 枚举值
#
# 这些**必须直接写成中文**。MaiBot 的 WebUI 把选项渲染成 ``String(枚举值)``，
# 不做 i18n 查找，所以值是英文就一定显示英文。代价是 config.toml 里会出现中文，
# 换来的是用户在界面上真的看得懂。早期英文值由 _LEGACY_* 映射自动迁移。
# --------------------------------------------------------------------------- #
PROVIDER_REMOTE = "接入 ArkSearch 服务"
PROVIDER_BUILTIN = "插件自建"

# 前五个是 MaiBot 自己模型管理页里的术语，**不翻译**——用户要在那边对上号，
# 译成「回复器/规划器」反而对不上。只给插件自己的概念（自定义端点、不总结）用中文。
SOURCE_REPLYER = "maibot replyer"
SOURCE_PLANNER = "maibot planner"
SOURCE_UTILS = "maibot utils"
SOURCE_VLM = "maibot vlm"
SOURCE_EMBEDDING = "maibot embedding"
SOURCE_PLUGIN = "自定义端点"
SOURCE_NONE = "不总结（只返原文）"

PROXY_AUTO = "自动探测"
PROXY_NONE = "强制直连"

#: 剧情库来源的全部合法取值
DATA_PROVIDERS: tuple[str, ...] = (PROVIDER_REMOTE, PROVIDER_BUILTIN)

#: 模型来源的全部合法取值，顺序即界面展示顺序
MODEL_SOURCES: tuple[str, ...] = (
    SOURCE_REPLYER, SOURCE_PLANNER, SOURCE_UTILS, SOURCE_VLM,
    SOURCE_EMBEDDING, SOURCE_PLUGIN, SOURCE_NONE,
)

#: 其中走 MaiBot 预设分类的；其余是「自定义端点」和「不总结」
MAIBOT_MODEL_TASKS: tuple[str, ...] = (
    SOURCE_REPLYER, SOURCE_PLANNER, SOURCE_UTILS, SOURCE_VLM, SOURCE_EMBEDDING,
)

#: 传给 MaiBot API 的任务名——界面显示中文，宿主只认英文
MAIBOT_TASK_NAMES: Dict[str, str] = {
    SOURCE_REPLYER: "replyer",
    SOURCE_PLANNER: "planner",
    SOURCE_UTILS: "utils",
    SOURCE_VLM: "vlm",
    SOURCE_EMBEDDING: "embedding",
}

# 早期版本用的英文枚举值，加载时自动迁移
_LEGACY_PROVIDER = {"remote": PROVIDER_REMOTE, "builtin": PROVIDER_BUILTIN}
_LEGACY_SOURCE = {
    "replyer": SOURCE_REPLYER,
    "planner": SOURCE_PLANNER,
    "utils": SOURCE_UTILS,
    "vlm": SOURCE_VLM,
    "embedding": SOURCE_EMBEDDING,
    "plugin": SOURCE_PLUGIN,
    "none": SOURCE_NONE,
}
_LEGACY_PROXY = {"auto": PROXY_AUTO, "none": PROXY_NONE}


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__: str = "基础设置"
    config_version: str = Field(default="1.0.0", description="配置版本号", json_schema_extra=_lbl("配置版本号"))
    enabled: bool = Field(default=True, description="是否启用插件", json_schema_extra=_lbl("启用插件"))


class CommandConfig(PluginConfigBase):
    """``/ark`` 命令的行为。"""

    __ui_label__: str = "斜杠命令"
    prefix: str = Field(default="/ark", description="命令前缀，改后需要重载插件", json_schema_extra=_lbl("命令前缀"))
    intercept: bool = Field(
        default=True,
        description=(
            "是否消费掉这条消息。开启后麦麦不会再对「/ark 指令」本身发起人格化回复；"
            "关闭后麦麦会先看到指令、稍后又看到结果，容易自问自答"
        ),
        json_schema_extra=_lbl("消费消息（防重复回复）"),
    )
    store_message: bool = Field(
        default=False,
        description=(
            "是否把命令结果写进 MaiBot 消息库。关闭后结果只发到 QQ、不进入麦麦的上下文，"
            "麦麦就不会再评价自己刚发的那段内容"
        ),
        json_schema_extra=_lbl("结果写入消息库"),
    )
    show_empty_hint: bool = Field(
        default=True,
        description="没检索到内容时是否回复一句提示",
        json_schema_extra=_lbl("无结果时提示"),
    )
    extract_keyword: bool = Field(
        default=True,
        description=(
            "先用模型把整句自然语言提炼成检索词，再去检索。中文没有词边界，"
            "不提炼的话「那句台词出自哪」这类长句基本检索不到；关闭可省一次模型调用"
        ),
        json_schema_extra=_lbl("模型提炼关键词"),
    )
    keyword_threshold: int = Field(
        default=8,
        description="输入超过多少个字才需要提炼，短的直接当关键词用",
        ge=0,
        le=100,
        json_schema_extra=_lbl("提炼触发字数"),
    )


class EndpointConfig(PluginConfigBase):
    """插件内自带的单个模型端点。"""

    __ui_label__: str = "模型端点"
    name: str = Field(default="", description="端点名称，仅用于日志与冷却统计", json_schema_extra=_lbl("端点名称"))
    base_url: str = Field(
        default="",
        description="OpenAI 兼容接口根地址，例如 https://example.com/v1",
        json_schema_extra=_lbl("接口地址"),
    )
    api_key: str = Field(default="", description="API Key，留空表示无需鉴权", json_schema_extra=_lbl("API Key"))
    model: str = Field(
        default="",
        description="模型名，例如 qwen2.5-7b-instruct",
        json_schema_extra=_lbl("模型名"),
    )
    timeout: float = Field(
        default=25.0,
        description="单次请求超时（秒）",
        ge=1.0,
        le=300.0,
        json_schema_extra=_lbl("超时(秒)"),
    )
    cooldown: float = Field(
        default=120.0,
        description="连续失败后的熔断冷却（秒）",
        ge=0.0,
        le=3600.0,
        json_schema_extra=_lbl("熔断冷却(秒)"),
    )


class RetrievalConfig(PluginConfigBase):
    """剧情库来源：接入 ArkSearch 服务，或插件自建。"""

    __ui_label__: str = "检索服务"
    provider: List[Literal[PROVIDER_REMOTE, PROVIDER_BUILTIN]] = Field(
        default_factory=lambda: [PROVIDER_REMOTE],
        description=(
            "剧情库来源，可多选，**按勾选顺序依次尝试，前一个不可用就自动降级到下一个**。"
            "接入 ArkSearch 服务=填「服务地址」指向本机或远程已部署的后端（http://127.0.0.1:48910）；"
            "插件自建=用插件目录下自带的语料在进程内检索，不依赖外部服务。"
            "两个都勾上就是「优先用服务，服务挂了就用自建」，最稳"
        ),
        json_schema_extra=_lbl("数据来源", **_i18n_choices({p: p for p in DATA_PROVIDERS})),
    )
    base_url: str = Field(
        default="",
        description=(
            "ArkSearch 服务地址。勾选了「接入 ArkSearch 服务」时必填，例如 http://127.0.0.1:48910。"
            "留空表示还没配置；只要还勾着「插件自建」，插件就会自动走自建，不会反复尝试空地址"
        ),
        json_schema_extra=_lbl("服务地址"),
    )
    data_dir: str = Field(
        default="",
        description=(
            f"语料存放目录。留空则用插件目录下的 {DEFAULT_DATA_SUBDIR} 子文件夹"
            "（推荐，整包迁移时语料跟着走）。填相对路径按插件目录解析，填绝对路径则直接用该路径"
        ),
        json_schema_extra=_lbl("语料目录"),
    )
    repo: str = Field(
        default=DEFAULT_REPO,
        description="自建模式的语料仓库（owner/name）。默认是源项目 CI 产出的构建成品",
        json_schema_extra=_lbl("语料仓库"),
    )
    branch: str = Field(default=DEFAULT_BRANCH, description="语料仓库分支", json_schema_extra=_lbl("分支"))
    proxy: Literal[PROXY_AUTO, PROXY_NONE] = Field(
        default=PROXY_AUTO,
        description="下载语料时的代理：自动探测=先试直连、不通再找系统代理；强制直连=完全不用代理",
        json_schema_extra=_lbl("下载代理", **_i18n_choices({p: p for p in (PROXY_AUTO, PROXY_NONE)})),
    )
    auto_update: bool = Field(
        default=False,
        description=(
            "开启后每次加载插件时检查一次语料版本，有新版就在后台下载。"
            "检查只读一个几 KB 的 commit feed，不会消耗 GitHub API 配额"
        ),
        json_schema_extra=_lbl("自动检查更新"),
    )
    alias_data_dir: str = Field(
        default="",
        description=(
            "别名表目录绝对路径，用于 jieba 预筛。"
            "留空则依次尝试环境变量 ARKSEARCH_DATA、插件同级目录下的 arksearch、"
            "以及自建模式的数据目录。找不到时自动跳过预筛，功能不受影响"
        ),
        json_schema_extra=_lbl("别名表目录"),
    )
    use_jieba: bool = Field(
        default=True,
        description=(
            "开启后用别名库做 jieba 预筛：只把命中别名白名单的词当检索词，"
            "过滤掉「看着」「什么」这类噪音。要额外装 jieba（MaiBot 自带）"
        ),
        json_schema_extra=_lbl("启用 jieba 预筛"),
    )
    jieba_max_terms: int = Field(
        default=4,
        description="一次查询最多取几个白名单词送检，越靠前区分度越高",
        ge=1,
        le=10,
        json_schema_extra=_lbl("白名单词数量上限"),
    )
    timeout: float = Field(
        default=10.0,
        description="检索超时（秒）",
        ge=1.0,
        le=120.0,
        json_schema_extra=_lbl("超时(秒)"),
    )
    result_limit: int = Field(
        default=4,
        description="每次检索返回的篇目数上限",
        ge=1,
        le=20,
        json_schema_extra=_lbl("返回篇目上限"),
    )
    max_hits: int = Field(
        default=3,
        description="每篇最多取几个命中片段",
        ge=1,
        le=10,
        json_schema_extra=_lbl("每篇片段上限"),
    )
    max_candidates: int = Field(
        default=6,
        description="从自然语言问题里最多试几个候选检索词，搜索耗时随此线性增长",
        ge=1,
        le=15,
        json_schema_extra=_lbl("候选词数量上限"),
    )
    max_context_chars: int = Field(
        default=1200,
        description="送进模型的原文总字数上限，超出按相关度截断",
        ge=200,
        le=20000,
        json_schema_extra=_lbl("上下文字数上限"),
    )
    cache_ttl: float = Field(
        default=300.0,
        description="检索结果缓存时长（秒）",
        ge=0.0,
        le=3600.0,
        json_schema_extra=_lbl("缓存时长(秒)"),
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_provider(cls, data: Any) -> Any:
        """把单选的 ``provider="builtin"`` 和早期英文值统一迁成多选列表。

        Args:
            data: 校验前的原始配置字典。

        Returns:
            迁移后的配置字典。
        """
        if not isinstance(data, dict):
            return data
        raw = data.get("provider")
        if raw is None:
            return data
        if isinstance(raw, str):
            return {**data, "provider": [_LEGACY_PROVIDER.get(raw.strip(), raw.strip())]}
        if isinstance(raw, list):
            items = [_LEGACY_PROVIDER.get(str(x).strip(), str(x).strip()) for x in raw]
            return {**data, "provider": items}
        return data

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_proxy(cls, data: Any) -> Any:
        """把早期的 ``proxy="auto"`` 翻成中文枚举值。

        Args:
            data: 校验前的原始配置字典。

        Returns:
            迁移后的配置字典。
        """
        if isinstance(data, dict):
            raw = data.get("proxy")
            if isinstance(raw, str):
                text = raw.strip()
                if text in _LEGACY_PROXY:
                    return {**data, "proxy": _LEGACY_PROXY[text]}
        return data

    # ------------------------------------------------------------------ 便捷 -- #
    @property
    def providers(self) -> List[str]:
        """按用户勾选顺序返回可用的剧情库来源。"""
        return _as_list(self.provider, DATA_PROVIDERS, (PROVIDER_REMOTE,), _LEGACY_PROVIDER)

    @property
    def use_builtin(self) -> bool:
        """是否包含「插件自建」。"""
        return PROVIDER_BUILTIN in self.providers

    @property
    def use_remote(self) -> bool:
        """是否勾选了「接入 ArkSearch 服务」，且地址已填。"""
        return PROVIDER_REMOTE in self.providers and bool(self.base_url.strip())


class ModelConfig(PluginConfigBase):
    """模型来源与生成参数。"""

    __ui_label__: str = "模型来源"

    source: List[Literal[SOURCE_REPLYER, SOURCE_PLANNER, SOURCE_UTILS, SOURCE_VLM, SOURCE_EMBEDDING, SOURCE_PLUGIN, SOURCE_NONE]] = Field(
        default_factory=lambda: [SOURCE_UTILS],
        description=(
            "LLM 来源，可多选，**按列表顺序依次尝试，前一个不可用就自动降级到下一个**。"
            "maibot replyer/planner/utils/vlm/embedding = 复用 MaiBot「模型管理」里同名分类已配好的模型；"
            "自定义端点 = 用下面自己填的端点；不总结 = 只把检索到的原文返回。"
            "剧情总结属于「概括整理小任务」，首选 maibot utils——planner 是规划模型，又大又慢，"
            "实测能把一次问答拖到 15 秒。"
            "搭配建议：勾【maibot utils + 自定义端点】，前者挂了自动落到后者，"
            "不会因为免费轮盘抽到坏模型就整段失忆。"
            "注意：MaiBot 里若配的是 openrouter/free 这种免费轮盘，随时可能被路由到"
            "推理模型（曾实测命中 nvidia/nemotron，思维链被当正文输出）。插件已做输出净化兜底，"
            "但固定指定一个关掉 thinking 的模型会更快也更稳"
        ),
        json_schema_extra=_lbl("来源", **_i18n_choices({s: s for s in MODEL_SOURCES})),
    )
    temperature: float = Field(
        default=0.4,
        description="采样温度",
        ge=0.0,
        le=2.0,
        json_schema_extra=_lbl("温度"),
    )
    max_tokens: int = Field(
        default=600,
        description=(
            "单次生成上限。给推理模型时要留够它写思维链的空间，"
            "太小会导致只输出半截思考过程就被截断"
        ),
        ge=64,
        le=8192,
        json_schema_extra=_lbl("生成长度上限"),
    )
    endpoints: List[EndpointConfig] = Field(
        default_factory=list,
        description="来源选「自定义端点」时使用，按顺序轮转，失败自动跳过",
        json_schema_extra=_lbl("自定义端点"),
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_source(cls, data: Any) -> Any:
        """逐代迁移 ``source``：老英文枚举值 / 单选 / 最老的 maibot+task 写法。

        迁移的起因是枚举值从英文改成了中文（为了在 WebUI 里显示中文）。
        老配置不该因为升级就加载失败。

        Args:
            data: 校验前的原始配置字典。

        Returns:
            迁移后的配置字典。
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        raw = data.get("source")

        def _one(text: str) -> str:
            """把单个值翻成当前枚举值。"""
            text = str(text or "").strip()
            # 先原样试、再小写试。不能无脑 .lower()——那会把
            # "MaiBot 工具模型" 变成 "maibot 工具模型"，Literal 校验直接失败。
            if text in MODEL_SOURCES:
                return text
            if text == "maibot":
                return SOURCE_UTILS
            return _LEGACY_SOURCE.get(text.lower(), text)

        # 第一代：source="maibot" + maibot_task="xxx"
        if raw == "maibot":
            task = str(data.get("maibot_task") or "").strip()
            data["source"] = [task if task in MODEL_SOURCES else _LEGACY_SOURCE.get(task.lower(), SOURCE_UTILS)]
        # 第二代：单选字符串（英文或中文）
        elif isinstance(raw, str):
            data["source"] = [_one(raw)]
        # 第三代：列表，逐项翻
        elif isinstance(raw, list):
            data["source"] = [_one(item) for item in raw]

        data.pop("maibot_task", None)
        return data


class ResilienceConfig(PluginConfigBase):
    """容错策略——针对不稳定的免费小模型。"""

    __ui_label__: str = "容错策略"
    total_budget: float = Field(
        default=45.0,
        description="单次工具调用的总时间预算（秒），超时即放弃并返回已拿到的内容",
        ge=5.0,
        le=600.0,
        json_schema_extra=_lbl("总时间预算(秒)"),
    )
    max_attempts: int = Field(
        default=4,
        description="单次生成最多尝试几个端点",
        ge=1,
        le=20,
        json_schema_extra=_lbl("最大尝试端点数"),
    )
    base_backoff: float = Field(
        default=1.0,
        description="重试退避基数（秒）",
        ge=0.0,
        le=30.0,
        json_schema_extra=_lbl("退避基数(秒)"),
    )
    max_backoff: float = Field(
        default=8.0,
        description="重试退避上限（秒）",
        ge=0.0,
        le=120.0,
        json_schema_extra=_lbl("退避上限(秒)"),
    )
    error_threshold: int = Field(
        default=2,
        description="端点连续失败多少次后进入熔断冷却",
        ge=1,
        le=20,
        json_schema_extra=_lbl("熔断失败阈值"),
    )
    fail_open: bool = Field(
        default=True,
        description="模型全部失败时，是否把原文片段直接返回而不是报错",
        json_schema_extra=_lbl("失败时回退原文"),
    )


class _SourceList:
    """``model.source`` 是多选回退链，不是单个值。

    单独拆出来是为了让「这是有序列表」的语义在代码里显式化——直接对
    ``config.model.source`` 做字符串比较会静默出错（列表和非空都成立）。
    """

    #: 全部合法取值
    ALLOWED: tuple[str, ...] = MODEL_SOURCES
    #: 勾选「不总结」时，无论还勾了什么都不再调用模型
    STOP = SOURCE_NONE

    @staticmethod
    def resolve(value: Any) -> List[str]:
        """归一化配置值，顺带把早期英文值翻成中文。

        Args:
            value: 配置里的原始值。

        Returns:
            去重后的有序来源列表。
        """
        return _as_list(value, MODEL_SOURCES, (SOURCE_UTILS,), _LEGACY_SOURCE)

    @staticmethod
    def task_name(source: str) -> str:
        """把界面上的中文来源翻成 MaiBot API 认的英文任务名。

        Args:
            source: 来源枚举值。

        Returns:
            ``replyer`` / ``planner`` / ``utils`` / ``vlm`` / ``embedding`` 之一。
        """
        return MAIBOT_TASK_NAMES.get(source, "utils")


class ArkLoreConfig(PluginConfigBase):
    """插件配置总入口。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig, description="基础设置")
    command: CommandConfig = Field(default_factory=CommandConfig, description="斜杠命令")
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig, description="检索服务")
    model: ModelConfig = Field(default_factory=ModelConfig, description="模型来源")
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig, description="容错策略")


# --------------------------------------------------------------------------- #
# 阻塞式 HTTP（标准库），统一放线程池执行，绝不阻塞事件循环
# --------------------------------------------------------------------------- #
def _post_json_sync(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout: float) -> Dict[str, Any]:
    """发起一次 POST 请求并解析 JSON 响应。

    Args:
        url: 目标地址。
        payload: JSON 请求体。
        headers: 请求头。
        timeout: 底层 socket 超时。

    Returns:
        解析后的 JSON 字典。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


# --------------------------------------------------------------------------- #
# 别名表 + jieba 预筛
# --------------------------------------------------------------------------- #
class AliasIndex:
    """ArkSearch 别名表：白名单 + 只含别名库的 jieba 分词器。

    为什么不用 jieba 默认词表：它是 2017 年通用语料，明日方舟 2019 年才上线，
    「凯尔希」会被切成「凯尔 / 希在」，「可露希尔」被切成「可露 / 希尔」——
    切碎的实体反而成了噪音。这里构造一个**空** Tokenizer，把 ArkSearch 自己的
    12078 条别名全量注入，于是分词只可能沿着游戏内的实体边界走。

    为什么还要再做一次白名单：即使词典里只有别名，切词仍会吐出单字和
    句子里没收录的短语（单字没有区分度），必须用同一份别名集合过滤。
    """

    def __init__(self, names: Set[str]) -> None:
        self.names = names
        self._tokenizer: Any = None
        if not names:
            return
        try:
            import jieba
        except ImportError:
            return
        try:
            tokenizer = jieba.Tokenizer()
            # jieba 0.42.1 的 add_word() 会触发 initialize() 去读自带的 dict.txt。
            # 预置 initialized=True 让 initialize() 直接早退，从而彻底不加载默认词表。
            tokenizer.initialized = True
            tokenizer.FREQ = {}
            tokenizer.total = 0
            tokenizer.user_word_tag_tab = {}
            # 词频给足，保证最长匹配优先；实体词的切分边界由别名长度决定
            for name in sorted(names, key=len, reverse=True):
                tokenizer.add_word(name, freq=1_000_000, tag="nz")
            self._tokenizer = tokenizer
        except Exception:  # 分词器构造失败不影响主流程，退回原有拆词逻辑
            self._tokenizer = None

    @property
    def ready(self) -> bool:
        return self._tokenizer is not None and bool(self.names)

    def __len__(self) -> int:
        return len(self.names)

    def extract(self, text: str, limit: int) -> List[str]:
        """切词后按白名单过滤，按**区分度**排序返回。

        Args:
            text: 原始查询串。
            limit: 最多返回几个词。

        Returns:
            命中别名白名单的词，按出现顺序去重；没有命中时返回空列表。
        """
        if not self.ready or not text:
            return []
        try:
            tokens = self._tokenizer.cut(text)
        except Exception:
            return []
        result: List[str] = []
        seen: set[str] = set()
        for token in tokens:
            token = token.strip()
            if len(token) < 2 or token in seen:
                continue
            # 白名单：只认别名库里真实存在的实体
            if token not in self.names:
                continue
            seen.add(token)
            result.append(token)
            if len(result) >= limit:
                break
        return result


class AliasIndexFactory:
    """在插件启动时从本地 ArkSearch 提取别名表。"""

    @staticmethod
    def _candidate_dirs(configured: str, configured_data_dir: str = "") -> List[Path]:
        """列出可能的 data/story 目录，按优先级排列。

        用户**显式填了路径**就只用它——填错了要明确报错，不能偷偷回退到猜的
        目录，否则「我明明指定了却读的是别处」会很难排查。只有留空时才自动探测。

        Args:
            configured: 用户在「别名表目录」里填的路径，可为空。
            configured_data_dir: 用户在「语料目录」里填的路径，可为空。

        Returns:
            候选目录列表，第一个存在的就是它。
        """
        if configured:
            return [Path(configured)]

        candidates: List[Path] = []
        env = os.environ.get("ARKSEARCH_DATA", "").strip()
        if env:
            candidates.append(Path(env))
        plugin_dir = Path(__file__).resolve().parent
        # 自建模式的语料优先用——它跟着插件走，通常就是最新的那份
        configured_data = (configured_data_dir or "").strip()
        if configured_data:
            path = Path(configured_data)
            candidates.append(path if path.is_absolute() else plugin_dir / path)
        candidates.append(plugin_dir / DEFAULT_DATA_SUBDIR)
        # 插件部署目录同级常见布局：plugins/arknights-lore -> arksearch/backend/data/story
        for parent in list(plugin_dir.parents)[:4]:
            candidates.append(parent / "arksearch" / "backend" / "data" / "story")
        return candidates

    @staticmethod
    def _locate(configured: str, configured_data_dir: str = "") -> Optional[Path]:
        for path in AliasIndexFactory._candidate_dirs(configured, configured_data_dir):
            if (path / "seq_data.json").is_file():
                return path
        return None

    @classmethod
    def load(cls, configured: str, log: Callable[..., None], configured_data_dir: str = "") -> AliasIndex:
        """读取别名表并构建分词器。

        Args:
            configured: 「别名表目录」里填的路径。
            log: 插件日志函数，用于汇报成功或降级原因。
            configured_data_dir: 「语料目录」里填的路径，作为候选之一。

        Returns:
            别名索引；任何失败都返回空索引，功能自动退回原有拆词逻辑。
        """
        directory = cls._locate(configured, configured_data_dir)
        if directory is None:
            log(
                "WARNING",
                "未找到 ArkSearch 别名表目录，jieba 预筛已关闭（其余功能不受影响）。"
                "请在「检索服务 → 别名表目录」填 data/story 的绝对路径，"
                "或设置环境变量 ARKSEARCH_DATA",
            )
            return AliasIndex(set())

        names: Set[str] = set()
        started = time.monotonic()
        try:
            seq_path = directory / "seq_data.json"
            seq_data = json.loads(seq_path.read_text(encoding="utf-8"))
            # 实际形状是 [[char_id 列表, 别名列表], ...]，别名在每项的下标 1；
            # 旧版本曾用过 {"name": [...]} 的字典形状，这里一并兼容。
            for entry in seq_data:
                value: Any = None
                if isinstance(entry, dict):
                    value = entry.get("name")
                elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    value = entry[1]
                if isinstance(value, list):
                    names.update(item for item in value if isinstance(item, str))
            zone_path = directory / "zone_name.json"
            if zone_path.is_file():
                zone_data = json.loads(zone_path.read_text(encoding="utf-8"))
                for zone in zone_data.values():
                    if isinstance(zone, dict):
                        label = zone.get("zh_CN")
                        if isinstance(label, str):
                            names.add(label)
        except Exception as exc:
            log("WARNING", "别名表读取失败（%s），jieba 预筛已关闭：%s", directory, exc)
            return AliasIndex(set())

        # 单字没有区分度（「希」能命中 1438 篇），只保留两字及以上
        names = {name.strip() for name in names if name and len(name.strip()) >= 2}
        if not names:
            log("WARNING", "别名表为空，jieba 预筛已关闭")
            return AliasIndex(set())

        index = AliasIndex(names)
        if not index.ready:
            log("WARNING", "jieba 不可用或分词器构造失败，预筛退回原有拆词逻辑")
        else:
            log(
                "INFO",
                "已从 %s 提取 %d 条别名，jieba 预筛就绪（%.2fs）",
                directory,
                len(names),
                time.monotonic() - started,
            )
        return index


# --------------------------------------------------------------------------- #
# 插件主体
# --------------------------------------------------------------------------- #
class ArknightsLorePlugin(MaiBotPlugin):
    """明日方舟剧情速查。"""

    config_model = ArkLoreConfig

    def __init__(self) -> None:
        super().__init__()
        # 插件目录下的滚动日志（最多 500 条），独立于 MaiBot 主日志
        self._file_log = PluginFileLog(Path(__file__).resolve().parent / _LOG_FILENAME)
        # 端点名 -> 熔断恢复时间戳
        self._cooldown_until: Dict[str, float] = {}
        # 端点名 -> 连续失败次数
        self._fail_count: Dict[str, int] = {}
        # 检索缓存键 -> (过期时间戳, 上下文文本)
        self._cache: Dict[str, Any] = {}
        # 启动时从本地 ArkSearch 提取的别名表，驱动 jieba 预筛
        self._alias_index = AliasIndex(set())
        # 自建模式的本地检索引擎；用远程服务时为 None
        self._engine: Optional[ArkData] = None
        # 自建语料目录，默认在插件目录下的 data 子文件夹。
        # 这里**只能给默认值**，不能调 _resolve_data_dir()——加载器是
        # 先 create_plugin() 再注入配置的（plugin_loader.py:590），
        # 构造函数里碰 self.config 会抛「当前插件配置尚未完成注入」。
        # 真正的解析放到 on_load / on_config_update，那时配置已经注入。
        self._data_dir = Path(__file__).resolve().parent / DEFAULT_DATA_SUBDIR
        # 正在进行的构建/下载，防止重复触发
        self._building = False
        # 上次自动检查更新的时间戳，避免每次调用都去问远端
        self._last_update_check = 0.0

    def _log(self, level: str, message: str, *args: Any) -> None:
        """同时写入 MaiBot 主日志和插件自己的日志文件。

        Args:
            level: INFO / WARNING / ERROR。
            message: 日志模板。
            *args: 格式化参数。
        """
        rendered = message % args if args else message
        logger = self.ctx.logger
        if level == "INFO":
            logger.info(rendered)
        elif level == "WARNING":
            logger.warning(rendered)
        else:
            logger.error(rendered)
        self._file_log.write(level, rendered)

    # ------------------------------ 生命周期 ------------------------------ #
    def _resolve_data_dir(self) -> Path:
        """确定语料目录。

        留空时用插件目录下的 ``data`` 子文件夹——这样语料跟着插件走，整包迁移、
        备份、重装都不会丢。相对路径按插件目录解析，绝对路径直接用。

        配置还没注入时（构造函数的时机）退回插件目录下的 ``data``，而不是把
        异常抛出去——加载器是在注入配置**之前**构造实例的。

        Returns:
            语料目录。
        """
        plugin_dir = Path(__file__).resolve().parent
        try:
            retrieval = self.config.retrieval
            configured = (retrieval.data_dir or "").strip()
        except Exception:
            return plugin_dir / DEFAULT_DATA_SUBDIR
        if not configured:
            return plugin_dir / DEFAULT_DATA_SUBDIR
        path = Path(configured)
        return path if path.is_absolute() else (plugin_dir / path)

    async def on_load(self) -> None:
        """插件加载。"""
        retrieval = self.config.retrieval
        self._data_dir = self._resolve_data_dir()
        self._log(
            "INFO",
            "明日方舟剧情速查已加载，模型来源：%s，剧情库来源：%s",
            "/".join(_SourceList.resolve(self.config.model.source)),
            "/".join(retrieval.providers),
        )
        self._log("INFO", "语料目录：%s", self._data_dir)
        await self._reload_alias_index()
        if retrieval.use_builtin:
            # 语料不存在就自动构建——勾了自建就不该还得再手动跑一次命令
            if self._engine is None and self._data_dir.joinpath("story_data.json").is_file():
                self._engine = ArkData(self._data_dir)
            await self._maybe_auto_update()

    async def on_unload(self) -> None:
        """插件卸载。"""
        self._cache.clear()
        # 释放 47MB 剧情库，否则热重载会把它留在内存里不放
        if self._engine is not None:
            self._engine.unload()
            self._engine = None

    # --------------------------- 自建模式：构建 --------------------------- #
    def _engine_or_none(self) -> Optional[ArkData]:
        """取出本地引擎，未加载时按需构造。

        Returns:
            就绪的引擎；没勾「插件自建」时返回 ``None``。
        """
        if not self.config.retrieval.use_builtin:
            return None
        if self._engine is None:
            self._engine = ArkData(self._data_dir)
        return self._engine

    async def _build_builtin(self, force: bool = False, stream_id: str = "", notify: bool = True) -> str:
        """下载并部署自建语料。

        整个过程在线程池里跑：13.6MB 下载 + 47MB 解压 + 校验，几十秒的事不能
        占着事件循环。已有可用语料时除非 ``force`` 否则不重复下载。

        Args:
            force: 已有数据也强制重新下载。
            stream_id: 需要回执时用的会话 id。
            notify: 是否把结果发到聊天。

        Returns:
            一句给用户看的结果说明。
        """
        if self._building:
            return "正在处理上一次请求，请稍候。"
        if not force and self._data_dir.joinpath("story_data.json").is_file():
            return f"语料已就绪（{self._data_dir}），无需重建。"

        self._building = True
        retrieval = self.config.retrieval
        try:
            proxy = detect_proxy(retrieval.proxy)
            self._log("INFO", "开始构建语料，代理：%s，仓库：%s", proxy or "直连", retrieval.repo)
            result = await asyncio.to_thread(
                download_story,
                self._data_dir,
                retrieval.repo,
                retrieval.branch,
                proxy,
            )
            if not result.get("ok"):
                message = str(result.get("message") or "下载失败")
                self._log("WARNING", "构建语料失败：%s", message)
                return f"语料更新失败：{message}"

            files = list(result.get("files") or [])
            commit = fetch_latest_commit(retrieval.repo, retrieval.branch, proxy) or ""
            if commit:
                write_manifest(self._data_dir, commit, files)

            # 数据换了，旧内存里的那份必须丢掉，否则会一直查到过期剧情
            if self._engine is not None:
                self._engine.unload()
            self._engine = ArkData(self._data_dir)

            size_mb = float(result.get("bytes") or 0) / 1048576.0
            message = f"语料已更新：{len(files)} 个文件，{size_mb:.1f} MB。"
            self._log("INFO", "%s 目录：%s", message, self._data_dir)
            if notify and stream_id:
                await self.ctx.send.text(message, stream_id)
            # 顺带把别名预筛也换成新语料里的
            await self._reload_alias_index()
            return message
        except Exception as exc:
            self._log("ERROR", "构建语料异常：%s", exc)
            return f"语料更新失败：{exc}"
        finally:
            self._building = False

    async def _maybe_auto_update(self) -> None:
        """自建模式下按需检查语料更新。

        冷却 6 小时，避免每次热重载都去问远端。网络不通一律静默。
        """
        retrieval = self.config.retrieval
        if not retrieval.auto_update:
            return
        now = time.monotonic()
        if self._last_update_check and now - self._last_update_check < 21600:
            return
        self._last_update_check = now
        try:
            proxy = detect_proxy(retrieval.proxy)
            status, message = await asyncio.to_thread(
                plan_update, self._data_dir, retrieval.repo, retrieval.branch, proxy, 10.0
            )
            if status == "stale":
                self._log("INFO", "检测到语料新版本：%s，开始更新", message)
                await self._build_builtin()
            else:
                self._log("INFO", "语料检查：%s", message)
        except Exception as exc:
            self._log("WARNING", "语料检查失败（忽略）：%s", exc)

    async def _reload_alias_index(self) -> None:
        """从本地 ArkSearch 提取别名表，构建只含别名库的 jieba 分词器。

        读取约 0.4 MB JSON 并注入一万多条词，放线程池里做，不阻塞事件循环。
        失败不会阻断插件加载，只是预筛退回原有的盲拆词逻辑。
        """
        retrieval = self.config.retrieval
        if not retrieval.use_jieba:
            self._alias_index = AliasIndex(set())
            self._log("INFO", "jieba 预筛已按配置关闭")
            return
        self._alias_index = await asyncio.to_thread(
            AliasIndexFactory.load, retrieval.alias_data_dir, self._log, retrieval.data_dir
        )

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """配置热重载：清掉熔断状态和检索缓存，别名表相关的配置变更要重建预筛。"""
        self._cooldown_until.clear()
        self._fail_count.clear()
        self._cache.clear()
        self._log("INFO", "配置已更新（scope=%s, version=%s），缓存与熔断状态已清空", scope, version)
        retrieval_keys = [key for key in (config_data or {}) if key.startswith("retrieval.")]
        try:
            if retrieval_keys:
                await self._reload_alias_index()
        except Exception as exc:  # 配置热重载绝不能把插件带崩
            self._log("WARNING", "别名表重建失败，保留旧预筛：%s", exc)

        # 勾上「插件自建」且本地无语料时立刻开始构建，不用等用户再发一次指令
        try:
            retrieval = self.config.retrieval
            self._data_dir = self._resolve_data_dir()
            if retrieval.use_builtin and not self._data_dir.joinpath("story_data.json").is_file():
                self._log("INFO", "勾选了「插件自建」且本地无语料，开始构建：%s", self._data_dir)
                await self._build_builtin(force=True, notify=False)
            elif not retrieval.use_builtin and self._engine is not None:
                # 取消勾选就释放本地引擎，避免 47MB 白占着内存
                self._engine.unload()
                self._engine = None
        except Exception as exc:
            self._log("WARNING", "切换数据来源时处理失败：%s", exc)

    # ------------------------------ 工具组件 ------------------------------ #
    @Tool(
        "arknights_lore",
        brief_description="检索《明日方舟》游戏剧情原文并回答",
        detailed_description=(
            "在《明日方舟》全部剧情原文中检索，返回带有篇章名与原文的整理结果。"
            "当用户问起剧情内容、角色台词出处、活动故事、主线章节或干员密录时使用。"
            "参数说明：\n"
            "- query：string，必填。想查的关键词或片段。\n"
            "- char_name：string，可选。已知的干员/角色名，用于按出场检索，比纯关键词更准。\n"
            "- question：string，可选。用户的原始问题原文，传入后回答更贴合提问。"
        ),
        parameters=[
            ToolParameterInfo(
                name="query",
                param_type=ToolParamType.STRING,
                description="检索关键词或剧情片段",
                required=True,
            ),
            ToolParameterInfo(
                name="char_name",
                param_type=ToolParamType.STRING,
                description="已知的角色名，用于按出场检索",
                required=False,
                default="",
            ),
            ToolParameterInfo(
                name="question",
                param_type=ToolParamType.STRING,
                description="用户原始问题原文",
                required=False,
                default="",
            ),
        ],
    )
    async def arknights_lore(
        self,
        query: str,
        char_name: str = "",
        question: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """检索剧情并整理回答。

        Args:
            query: 检索关键词或剧情片段。
            char_name: 已知的角色名。
            question: 用户原始问题原文。
            **kwargs: Host 注入的额外参数，含 stream_id。

        Returns:
            含 content 的字典，content 是给 LLM 阅读的最终材料。
        """
        del kwargs
        resilience = self.config.resilience
        deadline = time.monotonic() + resilience.total_budget
        started = time.monotonic()

        try:
            context_text = await asyncio.wait_for(
                self._search(query, char_name),
                timeout=self.config.retrieval.timeout,
            )
        except asyncio.TimeoutError:
            self._log("WARNING", "检索超时 %.1fs query=%r char=%r", self.config.retrieval.timeout, query, char_name)
            return {"success": False, "content": "剧情检索超时了，换个说法再试试。"}
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            # 连都连不上，多半是 ArkSearch 后端没启动——这是运维问题，提示要明确
            self._log("WARNING", "ArkSearch 连不上（后端可能未启动）: %s", exc)
            return {"success": False, "content": "剧情检索服务当前不可用，请稍后再试。"}
        except Exception as exc:
            self._log("WARNING", "ArkSearch 检索异常: %s", exc)
            return {"success": False, "content": "剧情检索出错，请稍后再试。"}

        if not context_text:
            self._log("INFO", "无结果 query=%r char=%r 用时%.2fs", query, char_name, time.monotonic() - started)
            return {"success": True, "content": f"在《明日方舟》剧情里没找到与「{query}」相关的内容。"}

        answer = await self._summarize(query, question, context_text, deadline)
        self._log(
            "INFO",
            "完成 query=%r char=%r 原文%d字 总结%s 用时%.2fs",
            query,
            char_name,
            len(context_text),
            "成功" if answer else "失败",
            time.monotonic() - started,
        )
        if answer:
            return {"success": True, "content": answer}

        if resilience.fail_open:
            # 模型全挂时不吞掉已检索到的原文，让上层还有话可说
            return {"success": True, "content": f"总结模型暂时不可用，以下是检索到的原文：\n\n{context_text}"}
        return {"success": False, "content": "总结模型暂时不可用，且已关闭原文兜底。"}

    # ------------------------------ 命令组件 ------------------------------ #
    @Command("ark", pattern=r"^/ark\s+(?P<q>.+)$", help="检索明日方舟剧情：/ark 关键词或整句自然语言均可")
    async def handle_ark(self, matched_groups: Optional[Dict[str, str]] = None, **kwargs: Any) -> Any:
        """手动检索：``/ark 关键词``。

        ``/ark`` 后面接什么都行——角色名、一句台词、或者完整的自然语言问句，
        插件会自己拆词检索。整条路径不经过 MaiBot 的对话 LLM（planner/replyer），
        总结由 model.source 指定的来源完成。

        Args:
            matched_groups: 正则捕获组，含 ``q``。
            **kwargs: Host 注入参数，含 stream_id。

        Returns:
            三元组 ``(是否成功, 描述, 拦截等级)``。

            注意第三个返回值是**拦截等级**而不是「是否发送」：
            Host 侧 ``continue_process = not bool(intercept_message_level)``，
            返回 0 会让 MaiBot 继续把这条指令当普通消息走 planner，于是麦麦
            会对 ``/ark`` 本身再回复一次。返回 1 才是「消费掉，别再处理」。
        """
        stream_id = kwargs.get("stream_id", "")
        keyword = str((matched_groups or {}).get("q", "")).strip()
        if not keyword:
            return False, "关键词为空", 1

        command_cfg = self.config.command
        # storage_message=False：结果只发到 QQ，不进 MaiBot 消息库，
        # 麦麦的上下文里就不会出现「自己刚说的这段」，也就不会去评价它。
        send_kwargs: Dict[str, Any] = {"storage_message": command_cfg.store_message}

        async def _run() -> None:
            if not stream_id:
                return
            deadline = time.monotonic() + self.config.resilience.total_budget
            try:
                # 整句自然语言先让模型提炼成检索词；短词直接用，省一次模型调用
                search_query = keyword
                if len(keyword) >= command_cfg.keyword_threshold:
                    extracted = await self._extract_keywords(keyword, deadline)
                    if extracted:
                        search_query = extracted

                context_text = await asyncio.wait_for(
                    self._search(search_query, "", stream_id),
                    timeout=self.config.resilience.total_budget,
                )
            except asyncio.TimeoutError:
                await self.ctx.send.text("检索超时了。", stream_id, **send_kwargs)
                return
            except Exception as exc:
                await self.ctx.send.text(f"检索服务不可用：{exc}", stream_id, **send_kwargs)
                return

            if not context_text:
                if command_cfg.show_empty_hint:
                    await self.ctx.send.text(f"没找到与「{keyword}」相关的剧情。", stream_id, **send_kwargs)
                return

            # 按要求不发送「正在处理」之类的占位消息，只在拿到成品后发一次
            answer = await self._summarize(search_query, keyword, context_text, deadline)
            await self.ctx.send.text(answer or context_text, stream_id, **send_kwargs)

        asyncio.create_task(_run())
        return True, "", 1 if command_cfg.intercept else 0

    # --------------------------- 语料维护命令 --------------------------- #
    @Command("arkcheck", pattern=r"^/arkcheck$", help="检查明日方舟剧情语料是否有新版本（不下载）")
    async def handle_ark_check(self, **kwargs: Any) -> Any:
        """检查语料更新。

        只读一个几 KB 的 commit feed 判断有没有新版，不下载任何东西，也不消耗
        GitHub API 的 60 次/小时配额。

        Args:
            **kwargs: Host 注入参数，含 stream_id。

        Returns:
            三元组 ``(是否成功, 描述, 拦截等级)``。
        """
        stream_id = kwargs.get("stream_id", "")

        async def _run() -> None:
            if not stream_id:
                return
            retrieval = self.config.retrieval
            if not retrieval.use_builtin:
                await self.ctx.send.text(
                    "「剧情库来源」里没有勾选「插件自建」，插件这边不持有语料，没有可检查的版本。"
                    "如果语料由 ArkSearch 服务管，请到那套服务上做更新。",
                    stream_id,
                )
                return
            try:
                proxy = detect_proxy(retrieval.proxy)
                status, message = await asyncio.to_thread(
                    plan_update, self._data_dir, retrieval.repo, retrieval.branch, proxy, 10.0
                )
            except Exception as exc:
                await self.ctx.send.text(f"检查失败：{exc}", stream_id)
                return

            if status == "stale":
                await self.ctx.send.text(f"检测到新版本（{message}）。发 /arkupdate 开始下载。", stream_id)
            elif status == "current":
                await self.ctx.send.text(f"语料已是最新。{message}", stream_id)
            else:
                # 网络不通属于常态，别制造焦虑
                await self.ctx.send.text(f"没能确认版本：{message}。稍后再试。", stream_id)

        asyncio.create_task(_run())
        return True, "", 1

    @Command("arkupdate", pattern=r"^/arkupdate$", help="下载并部署新版本剧情语料")
    async def handle_ark_update(self, **kwargs: Any) -> Any:
        """下载并部署新语料。

        下载 13.6MB、解压成 47MB，耗时几十秒。完成后会立刻生效，不需要重启。

        Args:
            **kwargs: Host 注入参数，含 stream_id。

        Returns:
            三元组 ``(是否成功, 描述, 拦截等级)``。
        """
        stream_id = kwargs.get("stream_id", "")

        async def _run() -> None:
            if not stream_id:
                return
            retrieval = self.config.retrieval
            if not retrieval.use_builtin:
                await self.ctx.send.text(
                    "「剧情库来源」里没有勾选「插件自建」，插件不持有语料，所以无可更新。"
                    "想由插件自己管语料，请在设置里勾上「插件自建」。",
                    stream_id,
                )
                return
            await self._build_builtin(force=True, stream_id=stream_id, notify=True)

        asyncio.create_task(_run())
        return True, "", 1

    # ------------------------------ 检索与上下文 ------------------------------ #
    # 标点与空白，用来把自然语言问题切成候选词
    _SPLIT_RE = re.compile(r"[\s,，。、；：？！…—\-_/\\\"'“”‘’（）()《》〈〉【】\[\]{}!?.,:;]+")

    def _candidate_terms(self, query: str) -> List[str]:
        """把模型给的自然语言 query 拆成候选检索词，按优先级排序。

        ArkSearch 的 text 检索要求**字面连续子串**，所以「凯尔希的原名」「凯尔希
        罗德岛」这类问题会直接返回 0——但每个实体词单独查都有几百篇。这里把问题
        拆开并逐步缩短前缀，让检索能落到真正有结果的词上。

        三层来源，优先级从高到低：

        1. **jieba 白名单**：只认别名表里的实体词，区分度最高。「凯尔希在孤星里
           说了什么」直接得到「凯尔希 / 孤星」，不用试探十几轮。
        2. 原句与分隔符切出来的 token。
        3. 长词逐级缩短的前缀。

        第 1 层没产出时（非实体问题，如「我会一直看着你」这种台词）自动落到 2、3 层，
        所以台词场景不受影响。
        """
        query = query.strip()
        if not query:
            return []

        limit = self.config.retrieval.max_candidates
        ordered: List[str] = []
        seen: set[str] = set()

        def push(term: str) -> bool:
            """加入候选词，跳过过短和重复项。

            Args:
                term: 候选检索词。

            Returns:
                是否已加入（重复或过短返回 False）。
            """
            term = term.strip()
            # 单字没有区分度（「希」能命中 1438 篇），跳过
            if len(term) < 2 or term in seen:
                return False
            seen.add(term)
            ordered.append(term)
            return True

        # 1. 别名白名单优先，jieba 的最长匹配已经按区分度从高到低排好了
        if self._alias_index.ready:
            for term in self._alias_index.extract(query, self.config.retrieval.jieba_max_terms):
                push(term)
                if len(ordered) >= limit:
                    return ordered

        tokens = [t for t in self._SPLIT_RE.split(query) if t]

        push(query)
        for token in tokens:
            push(token)

        # 长词逐级缩短：「凯尔希的原名」-> 凯尔希的原 -> 凯尔希的 -> 凯尔希
        for token in tokens:
            if len(token) > 3:
                for size in range(len(token) - 1, 2, -1):
                    push(token[:size])

        return ordered[:limit]

    def _build_alternation_regex(self, query: str, min_len: int = 3, max_terms: int = 40) -> str:
        """把长句子的所有候选子串拼成**一条**正则，用一次请求覆盖全部候选。

        逐个试候选对中文长句很不划算——「凯尔希在孤星里说了什么」有 11 个字，
        靠前缀缩短要试到第 8 次才够得到「凯尔希」。改成 `A|B|C|...` 择一后，
        后端一次 ``re.search`` 就能覆盖所有候选，代价只是一次请求。
        """
        query = query.strip()
        if not query:
            return ""
        chars = self._SPLIT_RE.split(query)
        terms: List[str] = []
        seen: set[str] = set()
        # 先长后短：更长的片段区分度更高，命中结果更准
        for length in range(len(chars[0]), min_len - 1, -1):
            for token in chars:
                for start in range(0, len(token) - length + 1):
                    term = token[start : start + length]
                    if term in seen:
                        continue
                    seen.add(term)
                    terms.append(term)
                    if len(terms) >= max_terms:
                        return "|".join(re.escape(t) for t in terms)
        return "|".join(re.escape(t) for t in terms)

    async def _search(self, query: str, char_name: str, stream_id: str = "") -> str:
        """检索剧情并拼装上下文，带短 TTL 缓存。

        按「数据来源」走两条等价路径：自建模式用进程内引擎，远程模式调
        ``/story`` 接口。两条路产出的结构完全一致，所以降级策略与上下文拼装
        是共用的。

        两个后端特性决定了这里的策略：

        1. 多个检索参数之间取**交集**，同时传角色名和关键词很容易把召回打空
           （char=凯尔希 有 199 篇，text=孤星 只有 1 篇，交集为 0）。
        2. `text` 检索要求**字面连续子串**，自然语言问题直接返回 0。因此主检索
           改用 `regex`（纯字面匹配、不过滤说话人、结果还更全），并把问题拆成
           候选词逐个尝试。

        Args:
            query: 检索关键词或自然语言问题。
            char_name: 角色名，可为空。

        Returns:
            拼装好的上下文文本；无结果时返回空串。
        """
        query = query.strip()
        char_name = char_name.strip()

        cache_key = f"{query}|{char_name}"
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached is not None and cached[0] > now:
            return cached[1]

        attempts: List[List[Dict[str, str]]] = []
        # 1. 给了角色名就优先用 char 模式——它走别名表，召回最准
        if char_name:
            if query:
                attempts.append([{"type": "char", "param": char_name}, {"type": "regex", "param": query}])
            attempts.append([{"type": "char", "param": char_name}])
        # 2. 自然语言问题拆词后逐个试
        for term in self._candidate_terms(query):
            attempts.append([{"type": "regex", "param": term}])
        # 2b. 长句子再兜一次：把所有候选子串并成一条正则，一次请求覆盖
        alternation = self._build_alternation_regex(query)
        if alternation:
            attempts.append([{"type": "regex", "param": alternation}])
        # 3. 最后退回 text 模式，它会滤掉「该词被当作角色名使用」的篇目，噪音更少
        if query:
            attempts.append([{"type": "text", "param": query}])
        if not attempts:
            return ""

        retrieval = self.config.retrieval

        # 按勾选顺序依次尝试，前一个拿不到结果就降级到下一个。
        # 两个都勾上时，「服务优先、自建兜底」是最省心的组合。
        for provider in retrieval.providers:
            if provider == PROVIDER_REMOTE:
                if not retrieval.base_url.strip():
                    self._log(
                        "WARNING",
                        "勾选了「接入 ArkSearch 服务」但「服务地址」为空，请在插件设置里填上",
                    )
                    continue
                found = await self._search_remote(attempts, retrieval, cache_key, now)
            else:
                found = await self._search_builtin(attempts, retrieval, cache_key, now, stream_id)
            if found:
                return found
        return ""

    async def _search_builtin(
        self,
        attempts: List[List[Dict[str, str]]],
        retrieval: Any,
        cache_key: str,
        now: float,
        stream_id: str,
    ) -> str:
        """用进程内引擎检索。结果结构与 /story 接口逐字节一致。

        Args:
            attempts: 逐级放宽的检索参数组。
            retrieval: 检索配置。
            cache_key: 缓存键。
            now: 当前时间戳。
            stream_id: 会话 id，用于在缺语料时提示。

        Returns:
            拼装好的上下文；无结果时返回空串。
        """
        engine = self._engine_or_none()
        if engine is None:
            return ""
        if not engine.ready and engine.missing_files():
            self._log(
                "WARNING",
                "自建语料尚未就绪（缺 %s），已自动开始下载",
                "、".join(engine.missing_files()[:3]),
            )
            asyncio.create_task(self._build_builtin(force=True, stream_id=stream_id))
            return ""

        for index, params in enumerate(attempts):
            try:
                rows, _total = await asyncio.wait_for(
                    asyncio.to_thread(engine.search, params, retrieval.result_limit),
                    timeout=retrieval.timeout,
                )
            except asyncio.TimeoutError:
                self._log("WARNING", "本地检索超时 %.1fs params=%s", retrieval.timeout, params)
                continue
            except ParamRejected as exc:
                # 正则编错、角色名不存在——和后端 440/500 同义，放宽下一级即可
                self._log("WARNING", "本地检索第 %d 级参数被拒：%s", index + 1, exc)
                continue
            context_text = self._build_context(rows)
            if not context_text:
                continue
            ttl = retrieval.cache_ttl if index == 0 else min(retrieval.cache_ttl, 60.0)
            self._cache[cache_key] = (now + ttl, context_text)
            return context_text
        return ""

    async def _search_remote(
        self,
        attempts: List[List[Dict[str, str]]],
        retrieval: Any,
        cache_key: str,
        now: float,
    ) -> str:
        """调 ArkSearch 的 ``/story`` 接口检索。

        Args:
            attempts: 逐级放宽的检索参数组。
            retrieval: 检索配置。
            cache_key: 缓存键。
            now: 当前时间戳。

        Returns:
            拼装好的上下文；无结果时返回空串。
        """
        base = retrieval.base_url.rstrip("/")
        headers = {"Content-Type": "application/json; charset=utf-8"}

        for index, params in enumerate(attempts):
            payload = {"params": params, "limit": retrieval.result_limit}
            try:
                response = await asyncio.to_thread(
                    _post_json_sync, f"{base}/story", payload, headers, retrieval.timeout
                )
            except urllib.error.HTTPError as exc:
                # ArkSearch 对无法识别的角色名会直接 500（char_name2id 抛 KeyError），
                # 非法正则是 440。这类是**参数**问题而非服务故障，应该跳过本级继续放宽，
                # 否则一个编错的名字会让整次检索失败，还会被误报成「服务不可用」。
                self._log(
                    "WARNING",
                    "检索第 %d 级被拒 HTTP %s，params=%s，继续降级",
                    index + 1,
                    exc.code,
                    params,
                )
                continue
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                # 服务连不上。**必须在这里吃掉**——否则异常会一路抛出去，
                # 后面勾选的「插件自建」根本没机会接管，回退链就白搭了。
                self._log("WARNING", "连不上 ArkSearch（%s）：%s", base, exc)
                return ""
            except Exception as exc:
                self._log("WARNING", "远程检索异常：%s", exc)
                return ""
            context_text = self._build_context(response.get("data", []))
            if not context_text:
                continue
            # 放宽到第 2、3 级才命中的，按较短 TTL 缓存，避免长期缓存到低精度结果
            ttl = retrieval.cache_ttl if index == 0 else min(retrieval.cache_ttl, 60.0)
            self._cache[cache_key] = (now + ttl, context_text)
            if index > 0:
                self._log(
                    "INFO", "检索放宽到第 %d 级命中（%s），交集过严", index + 1, "char" if index == 1 else "text"
                )
            return context_text

        return ""

    def _build_context(self, rows: List[Any]) -> str:
        """把 /story 的结构化结果转成可读文本，并按字数预算截断。

        Args:
            rows: /story 返回的 data 数组。

        Returns:
            拼装后的上下文文本。
        """
        retrieval = self.config.retrieval
        blocks: List[str] = []
        used = 0

        for row in rows:
            if not isinstance(row, list) or len(row) <= _EXTRA_INDEX:
                continue
            story_id, story_type, title, zone = row[0], row[1], row[2], row[3]
            header = f"《{title}》（{zone}）"

            for extra in row[_EXTRA_INDEX] or []:
                for hit in (extra.get("data") or [])[: retrieval.max_hits]:
                    # ArkSearch 有两种片段形状：text/regex 模式是 [上文, 说话人, 命中, 下文, 次行]，
                    # char 模式是单条整行文本。两者都要能拼。
                    if isinstance(hit, str):
                        snippet = hit.strip()
                    elif isinstance(hit, list):
                        before, speaker, matched, after, following = (list(hit) + ["", "", "", "", ""])[:5]
                        snippet_lines = []
                        if before or speaker:
                            snippet_lines.append(f"{speaker}{before}{matched}{after}".strip())
                        elif following:
                            snippet_lines.append(following.strip())
                        snippet = "\n".join(line for line in snippet_lines if line)
                    else:
                        continue
                    if not snippet:
                        continue

                    block = f"{header}\n{snippet}"
                    # 预算耗尽就停止累积，保留先命中的（后端已按相关度排序）
                    if used + len(block) > retrieval.max_context_chars:
                        return "\n\n".join(blocks)
                    blocks.append(block)
                    used += len(block)

        return "\n\n".join(blocks)

    # ------------------------------ 模型调用 ------------------------------ #
    async def _extract_keywords(self, text: str, deadline: float) -> str:
        """把整句自然语言提炼成检索关键词。

        中文没有词边界，靠前缀缩短或子串猜测既慢又不准——「魏彦吾那句我会一直
        看着你们是哪段剧情」有 19 个字，逐个试要试到第 12 次才够得到「我会一直
        看着你们」。借模型做一次提炼是唯一可靠的办法。

        Args:
            text: 用户原话。
            deadline: 总时间预算截止时间戳。

        Returns:
            提炼出的关键词；跳过或失败时返回空串，调用方退回原查询。
        """
        if not self.config.command.extract_keyword or _SourceList.STOP in _SourceList.resolve(self.config.model.source):
            return ""

        messages = [
            {
                "role": "system",
                "content": (
                    "你在为《明日方舟》剧情检索系统生成检索词。规则：\n"
                    "1. 只输出检索词本身，用空格分隔多个词，不要任何解释、标点或引号；\n"
                    "2. 抽取人名、地名、组织名、活动名，以及原句中的完整台词；\n"
                    "3. 台词要保持原样、不要改写；\n"
                    "4. 去掉「那句」「是哪段」「有没有讲」这类口语填充词；\n"
                    "5. 最多输出 5 个词；\n"
                    "6. 不要输出思考过程或分析步骤。"
                ),
            },
            {"role": "user", "content": text},
        ]

        raw = ""
        # 提炼环节对思维链零容忍：截断的推理会变成一串垃圾检索词，把检索彻底带偏。
        # 所以净化要放进循环里做——只有净化后仍为空才算这个来源不可用，
        # 否则「非空的思维链」会直接截断链条，后面的来源永远轮不上。
        for source in _SourceList.resolve(self.config.model.source):
            if source == _SourceList.STOP:
                return ""
            if time.monotonic() >= deadline:
                break
            candidate = (await self._call_maibot(messages, deadline, task_name=source)
                         if source in MAIBOT_MODEL_TASKS
                         else await self._call_endpoint_pool(messages, deadline))
            raw = sanitize_model_output(candidate, max_chars=200, require_chinese=False)
            if raw:
                break
            self._log(
                "INFO",
                "关键词提炼：来源 %s 不可用（%s），降级到下一个",
                source,
                "无输出" if not candidate else "输出被判为无效",
            )
        if not raw:
            self._log("INFO", "关键词提炼失败或输出不可用，退回原查询：%r", text[:30])
            return ""

        # 模型可能带 markdown 或多余解释，逐词剥掉包裹符号再筛长度
        terms: List[str] = []
        for raw_term in re.split(r"[\s,，、;；]+", raw):
            term = re.sub(r"^[`*「」\"'《》【】\[\]()]+|[`*「」\"'《》【】\[\]()]+$", "", raw_term)
            if len(term) >= 2:
                terms.append(term)
        keywords = " ".join(terms[:5])
        self._log("INFO", "关键词提炼 %r -> %r", text[:30], keywords)
        return keywords

    async def _summarize(self, query: str, question: str, context_text: str, deadline: float) -> str:
        """把检索到的原文整理成自然回答。

        按「模型来源」勾选的顺序依次尝试，前一个拿不到可用输出就降级到下一个。

        Args:
            query: 检索关键词。
            question: 用户原始问题，可为空。
            context_text: 检索到的原文上下文。
            deadline: 总时间预算的截止时间戳。

        Returns:
            整理后的回答；模型不可用或输出被判定为无效时返回空串，
            由调用方按 ``fail_open`` 退回原文。
        """
        sources = _SourceList.resolve(self.config.model.source)
        if _SourceList.STOP in sources:
            return context_text

        prompt = self._build_prompt(query, question, context_text)
        messages = [
            {"role": "system", "content": "你是《明日方舟》剧情资料员。只依据给定原文回答，不脑补、不臆造剧情。原文没提到的内容要明说。用简洁自然的中文口语回答，控制在 200 字以内。只输出回答本身，不要输出思考过程、分析步骤或任何解释。"},
            {"role": "user", "content": prompt},
        ]

        raw = ""
        answer = ""
        for index, source in enumerate(sources):
            if time.monotonic() >= deadline:
                # 预算耗尽就别再试下一个了，否则用户要干等整条链依次超时
                self._log("WARNING", "时间预算已耗尽，跳过剩余来源 %s", "/".join(sources[index:]))
                break
            raw = (await self._call_maibot(messages, deadline, task_name=source)
                   if source in MAIBOT_MODEL_TASKS
                   else await self._call_endpoint_pool(messages, deadline))
            answer = sanitize_model_output(raw)
            if answer:
                break
            # 关键：**不能只看 raw 非空就停**。免费轮盘抽到推理模型时，raw 恰恰
            # 是一整段非空的思维链——在这里 break 链条就断了，后面的来源永远
            # 轮不上，而「思维链污染」正是这条回退链要解决的场景。
            self._log(
                "INFO",
                "模型来源 %s 不可用（%s），降级到下一个",
                source,
                "无输出" if not raw else "输出被判为无效（多半是推理模型的思维链或安全判定串）",
            )
            if raw:
                self._log("WARNING", "该来源输出开头：%r", raw[:60])
        return answer

    @staticmethod
    def _build_prompt(query: str, question: str, context_text: str) -> str:
        """拼装用户侧提示词。"""
        parts = []
        if question.strip():
            parts.append(f"用户的问题：{question.strip()}")
        else:
            parts.append(f"用户想查的内容：{query.strip()}")
        parts.append(f"检索关键词：{query.strip()}")
        parts.append(f"剧情原文：\n{context_text}")
        return "\n\n".join(parts)

    async def _call_maibot(self, messages: List[Dict[str, str]], deadline: float, task_name: str = "") -> str:
        """复用 MaiBot 已配置的模型。

        Args:
            messages: OpenAI 格式消息列表。
            deadline: 总时间预算截止时间戳。
            task_name: 配置里的来源枚举值（中文）。这里会翻成 MaiBot 认的英文任务名。

        Returns:
            模型回答；失败时返回空串。
        """
        model_cfg = self.config.model
        remaining = deadline - time.monotonic()
        if remaining <= 1.0:
            self._log("WARNING", "调用 MaiBot 模型前预算已耗尽")
            return ""

        # 界面上是中文分类名，宿主只认英文；配置被手改成不认识的值就退回 utils
        resolved = _SourceList.task_name(task_name)

        try:
            result = await asyncio.wait_for(
                self.ctx.llm.generate(
                    prompt=messages,
                    task_name=resolved,
                    temperature=model_cfg.temperature,
                    max_tokens=model_cfg.max_tokens,
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            self._log("WARNING", "MaiBot 模型调用超时")
            return ""
        except Exception as exc:
            self._log("WARNING", "MaiBot 模型调用失败: %s", exc)
            return ""

        if isinstance(result, dict) and result.get("success"):
            return str(result.get("response") or "").strip()
        return ""

    async def _call_endpoint_pool(self, messages: List[Dict[str, str]], deadline: float) -> str:
        """在自填的端点池里轮转，按退避重试，全部失败则降级。

        Args:
            messages: OpenAI 格式消息列表。
            deadline: 总时间预算截止时间戳。

        Returns:
            第一个成功端点的回答；全部失败返回空串。
        """
        model_cfg = self.config.model
        resilience = self.config.resilience

        candidates = [ep for ep in model_cfg.endpoints if ep.base_url.strip() and ep.model.strip()]
        if not candidates:
            self._log("WARNING", "模型来源为 plugin，但端点池为空")
            return ""

        now = time.monotonic()
        # 熔断中的端点直接跳过，把预算留给还活着的
        alive = [ep for ep in candidates if self._cooldown_until.get(ep.name or ep.base_url, 0.0) <= now]
        if not alive:
            # 全在冷却：挑最早恢复的那个，并清一次冷却给它一个机会
            alive = [min(candidates, key=lambda ep: self._cooldown_until.get(ep.name or ep.base_url, 0.0))]
            self._cooldown_until.pop(alive[0].name or alive[0].base_url, None)

        last_error = ""
        for attempt in range(min(resilience.max_attempts, len(alive))):
            endpoint = alive[attempt % len(alive)]
            key = endpoint.name or endpoint.base_url
            remaining = deadline - time.monotonic()
            if remaining <= 1.0:
                self._log("WARNING", "端点 %s 前预算已耗尽，停止重试", key)
                break

            ok, text_or_error = await self._call_single_endpoint(endpoint, messages, min(remaining, endpoint.timeout))
            if ok:
                self._fail_count[key] = 0
                self._cooldown_until.pop(key, None)
                return text_or_error

            last_error = text_or_error
            self._fail_count[key] = self._fail_count.get(key, 0) + 1
            if self._fail_count[key] >= resilience.error_threshold:
                self._cooldown_until[key] = time.monotonic() + endpoint.cooldown
                self._log("WARNING", "端点 %s 连续失败 %d 次，熔断 %.0fs", key, self._fail_count[key], endpoint.cooldown)

            # 指数退避 + 抖动，避免多个端点同时炸时同步重试
            backoff = min(resilience.max_backoff, resilience.base_backoff * (2**attempt))
            if attempt + 1 < min(resilience.max_attempts, len(alive)) and backoff > 0:
                await asyncio.sleep(backoff + random.uniform(0, backoff * 0.3))

        self._log("WARNING", "端点池全部失败，最后一个错误：%s", last_error)
        return ""

    async def _call_single_endpoint(
        self,
        endpoint: EndpointConfig,
        messages: List[Dict[str, str]],
        timeout: float,
    ) -> tuple[bool, str]:
        """调用单个端点一次。

        Args:
            endpoint: 端点配置。
            messages: OpenAI 格式消息列表。
            timeout: 本次调用的超时上限。

        Returns:
            (是否成功, 回答文本或错误描述)。
        """
        model_cfg = self.config.model
        url = f"{endpoint.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if endpoint.api_key.strip():
            headers["Authorization"] = f"Bearer {endpoint.api_key.strip()}"

        payload = {
            "model": endpoint.model,
            "messages": messages,
            "temperature": model_cfg.temperature,
            "max_tokens": model_cfg.max_tokens,
            "stream": False,
        }

        try:
            data = await asyncio.to_thread(_post_json_sync, url, payload, headers, timeout)
        except asyncio.TimeoutError:
            return False, "请求超时"
        except urllib.error.HTTPError as exc:
            # 4xx 多半是端点本身的问题（鉴权/模型名/额度），重试无意义
            detail = exc.read().decode("utf-8", errors="replace")[:200] if exc.fp else ""
            return False, f"HTTP {exc.code} {detail}"
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            return False, f"连接失败: {exc}"
        except (ValueError, json.JSONDecodeError) as exc:
            return False, f"响应解析失败: {exc}"

        try:
            text = str(data["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError):
            return False, f"响应结构异常: {json.dumps(data, ensure_ascii=False)[:200]}"

        if not text:
            return False, "模型返回空内容"
        return True, text


def create_plugin() -> ArknightsLorePlugin:
    """创建插件实例。

    Returns:
        明日方舟剧情速查插件实例。
    """
    return ArknightsLorePlugin()
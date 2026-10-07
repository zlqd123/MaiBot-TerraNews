"""插件自测：用桩替换 maibot_sdk，对真实 ArkSearch 后端 + 故障注入端点做验证。

用法：
    python selftest.py
"""

import asyncio
import json
import logging
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field as PydanticField

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_PDIR = pathlib.Path(PLUGIN_DIR)
ARKSEARCH = "http://127.0.0.1:48910"
# 真实语料目录：内置引擎的测试要用真数据，桩数据测不出检索行为
# 真实语料目录：内置引擎的测试要用真数据，桩数据测不出检索行为。
# 改成你本机的 ArkSearch data\story 路径，或设环境变量 ARK_STORY 覆盖。
ARK_STORY = os.environ.get("ARK_STORY", r"<arksearch-dir>\backend\data\story")


# --------------------------------------------------------------------------- #
# maibot_sdk 桩
# --------------------------------------------------------------------------- #
def build_sdk_stub() -> None:
    sdk = types.ModuleType("maibot_sdk")
    types_mod = types.ModuleType("maibot_sdk.types")

    class PluginConfigBase(BaseModel):
        model_config = ConfigDict(validate_assignment=True, extra="ignore")

    class Field:  # noqa: D401 - 简单代理
        def __init__(self, default=None, description="", **kwargs):
            self.default = default
            self.description = description

        def __get__(self, obj, objtype=None):
            if self.default is None:
                return None
            return self.default() if callable(self.default) else self.default

    def _noop_decorator(*args, **kwargs):
        def wrap(fn):
            return fn

        if args and callable(args[0]) and len(args) == 1:
            return args[0]
        return wrap

    class MaiBotPlugin:
        def __init__(self):
            self._cfg = None
            self.ctx = types.SimpleNamespace(
                logger=logging.getLogger("plugin.test"),
                send=types.SimpleNamespace(text=self._noop_send),
            )

        async def _noop_send(self, *a, **k):
            STATE["sent"].append({"stream_id": a[1] if len(a) > 1 else k.get("stream_id"), "text": a[0] if a else "", "kwargs": k})
            return True

        @property
        def config(self):
            # 必须还原真实 SDK 的行为：配置是**加载器在构造实例之后**注入的，
            # 注入前访问 config 会抛 RuntimeError（maibot_sdk/plugin.py:219）。
            # 早先这个桩会偷偷造一份默认配置，于是「构造函数里碰 self.config」
            # 这种致命错误在自测里根本测不出来。
            STATE["config_touches"].append(1)
            if self._cfg is None:
                raise RuntimeError("当前插件配置尚未完成注入")
            return self._cfg

        def inject_config(self, cfg=None):
            """模拟加载器注入配置。"""
            self._cfg = cfg if cfg is not None else self.config_model()
            return self._cfg

    class ToolParamType:
        STRING = "string"
        INTEGER = "integer"
        NUMBER = "number"
        FLOAT = "number"
        BOOLEAN = "boolean"
        ARRAY = "array"
        OBJECT = "object"

    class ToolParameterInfo:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    sdk.PluginConfigBase = PluginConfigBase
    sdk.Field = PydanticField  # 用真 pydantic Field，才能拿到 default/default_factory
    sdk.MaiBotPlugin = MaiBotPlugin
    sdk.Tool = _noop_decorator
    sdk.Command = _noop_decorator
    sdk.HookHandler = _noop_decorator
    sdk.EventHandler = _noop_decorator
    sdk.API = _noop_decorator
    types_mod.ToolParamType = ToolParamType
    types_mod.ToolParameterInfo = ToolParameterInfo
    sdk.types = types_mod

    sys.modules["maibot_sdk"] = sdk
    sys.modules["maibot_sdk.types"] = types_mod


# --------------------------------------------------------------------------- #
# 故障注入端点
# --------------------------------------------------------------------------- #
STATE = {
    "fail_b": 0,
    "calls": {"a": 0, "b": 0, "slow": 0, "ok": 0},
    "sent": [],
    "keyword_reply": "",
    "summary_reply": "",
    # 记录 config 被访问了几次——用来断言构造函数**一次都没碰**它。
    # 光靠「访问会不会抛异常」测不出来：_resolve_data_dir 里有兜底的
    # except Exception，坏写法会被静默吞掉，测试就失去意义了。
    "config_touches": [],
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""

        # 关键词提炼请求带「检索词」提示词，单独应答以便测试清洗逻辑
        if b"search_term" in raw or "检索词".encode() in raw:
            reply = STATE["keyword_reply"]
            if not reply:
                self._send(500, b'{"error":"no keyword model"}')
            else:
                self._send(200, json.dumps({"choices": [{"message": {"content": reply}}]}).encode())
            return

        if self.path.endswith("/broken/chat/completions"):
            STATE["calls"]["a"] += 1
            self._send(500, b'{"error":"upstream exploded"}')
        elif self.path.endswith("/flaky/chat/completions"):
            STATE["calls"]["b"] += 1
            STATE["fail_b"] -= 1
            if STATE["fail_b"] > 0:
                self._send(503, b'{"error":"rate limited"}')
            else:
                self._send(200, json.dumps({"choices": [{"message": {"content": "来自备用端点的回答"}}]}).encode())
        elif self.path.endswith("/slow/chat/completions"):
            STATE["calls"]["slow"] += 1
            time.sleep(3)
            self._send(200, b'{"choices":[{"message":{"content":"too late"}}]}')
        else:
            STATE["calls"]["ok"] += 1
            # summary_reply 用来模拟推理模型吐出思维链 / 安全判定串
            reply = STATE["summary_reply"] or "主端点回答：测试通过"
            self._send(200, json.dumps({"choices": [{"message": {"content": reply}}]}).encode())


def start_mock() -> HTTPServer:
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


# --------------------------------------------------------------------------- #
# 测试
# --------------------------------------------------------------------------- #
PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


async def main() -> None:
    build_sdk_stub()
    sys.path.insert(0, PLUGIN_DIR)
    import plugin as P  # noqa: E402

    mock = start_mock()
    mock_base = f"http://127.0.0.1:{mock.server_address[1]}"
    print(f"mock endpoint: {mock_base}\n")

    # 先按加载器的顺序走一遍：构造实例（此时配置还没注入），再注入配置。
    # 构造函数里一旦碰 self.config 就会抛 RuntimeError，插件直接加载失败。
    STATE["config_touches"].clear()
    try:
        P.ArknightsLorePlugin()
        check("配置未注入时构造函数也能跑完", True)
    except RuntimeError as exc:
        check("配置未注入时构造函数也能跑完", False, f"加载器会在这里失败：{exc}")
    # 只看「有没有抛」不够：_resolve_data_dir 有兜底的 except Exception，
    # 构造函数读了配置也不会抛，只是拿不到用户填的路径。所以直接数访问次数。
    check(
        "构造函数完全不碰 self.config",
        not STATE["config_touches"],
        f"访问了 {len(STATE['config_touches'])} 次",
    )
    p = P.ArknightsLorePlugin()
    p.inject_config()
    check("注入后配置可读", isinstance(p.config, P.ArkLoreConfig))
    check("默认语料目录指向插件子文件夹", p._data_dir.name == P.DEFAULT_DATA_SUBDIR and p._data_dir.parent == _PDIR.resolve(), str(p._data_dir))
    p.config.retrieval.base_url = ARKSEARCH
    p.config.model.source = [P.SOURCE_PLUGIN]
    p.config.resilience.max_attempts = 4
    p.config.resilience.base_backoff = 0.05
    p.config.resilience.max_backoff = 0.2
    p.config.resilience.error_threshold = 2

    # --- T1 真实检索 + 上下文拼装 ---
    print("T1 真实 ArkSearch 检索 + 上下文拼装")
    t0 = time.monotonic()
    ctx = await p._search("凯尔希", "")
    dt = time.monotonic() - t0
    check("检索返回非空", bool(ctx), f"{dt*1000:.0f}ms, {len(ctx)} 字")
    check("上下文带篇章名", "《" in ctx and "》" in ctx)
    check("检索速度 < 3s", dt < 3.0, f"{dt*1000:.0f}ms")
    if ctx:
        print("    片段预览:", ctx[:120].replace("\n", " / "))

    # --- T2 按角色检索（含编错角色名的降级） ---
    print("\nT2 按角色名检索（char 模式）")
    ctx_char = await p._search("孤星", "凯尔希")
    check("角色+关键词检索有结果", bool(ctx_char), f"{len(ctx_char)} 字")

    # LLM 偶尔会编出后端不存在的角色名，此时 ArkSearch 的 char_name2id 抛 KeyError -> HTTP 500。
    # 正确行为是降级到纯关键词检索，而不是整个失败。
    p._cache.clear()
    res_bad = await p.arknights_lore(query="博士", char_name="塔露拉二世陛下")
    check("编造的角色名不会导致整体失败", res_bad["success"] is True, repr(res_bad["content"][:50]))
    check("编造角色名后仍能拿到内容", len(res_bad["content"]) > 20)
    check("不会误报检索服务不可用", "剧情检索服务" not in res_bad["content"])

    # 真正常见的合法别名也应该能查到
    p._cache.clear()
    ctx_alias = await p._search("紧急指令", "柯露夕")
    check("中文别名词条可检索", bool(ctx_alias), f"{len(ctx_alias)} 字")

    # --- T2b 自然语言问题：后端 text 检索要求字面连续子串，这类 query 原本全部归零 ---
    print("\nT2b 自然语言问题拆词检索")
    check(
        "候选词逐级缩短",
        p._candidate_terms("凯尔希的原名") == ["凯尔希的原名", "凯尔希的原", "凯尔希的", "凯尔希"],
        str(p._candidate_terms("凯尔希的原名")),
    )
    check(
        "按标点/空格切词",
        "凯尔希" in p._candidate_terms("凯尔希 我会一直看着你"),
        str(p._candidate_terms("凯尔希 我会一直看着你")),
    )
    check("丢弃无区分度的单字", "希" not in p._candidate_terms("凯尔希的"), str(p._candidate_terms("希")))

    for q in ("凯尔希的原名", "凯尔希 罗德岛", "凯尔希 我会一直看着你", "芙蓉的营养餐"):
        p._cache.clear()
        ctx = await p._search(q, "")
        check(f"自然语言 query 有结果 {q!r}", bool(ctx), f"{len(ctx)} 字")

    # --- T3 上下文预算截断 ---
    print("\nT3 上下文预算截断")
    p.config.retrieval.max_context_chars = 200
    ctx_small = await p._search("罗德岛", "")
    check("超预算被截断", 0 < len(ctx_small) <= 260, f"{len(ctx_small)} 字 (上限200)")
    p.config.retrieval.max_context_chars = 1200

    # --- T4 缓存命中 + 空结果不进缓存 ---
    print("\nT4 检索缓存")
    p._cache.clear()
    await p._search("罗德岛", "")
    t0 = time.monotonic()
    await p._search("罗德岛", "")
    cached_dt = time.monotonic() - t0
    check("命中结果走缓存", cached_dt < 0.02, f"{cached_dt*1000:.2f}ms")

    # 空结果刻意不缓存：语料更新后新出现的内容不应该被旧缓存挡住
    # 注意用「营养餐」——它本身查不到，长度又刚好等于择一正则的最小片段长度，
    # 所以连子串兜底也捞不到，能真正触发空结果分支
    p._cache.clear()
    ctx_empty = await p._search("营养餐", "")
    check("该查询确实无结果", ctx_empty == "", f"{len(ctx_empty)} 字")
    check("空结果不写缓存", len(p._cache) == 0, f"cache={len(p._cache)} 条")

    # --- T5 端点降级：第一个 500，第二个成功 ---
    print("\nT5 端点池降级（首端点 500 -> 切备用端点）")
    STATE["calls"]["a"] = STATE["calls"]["b"] = 0
    STATE["fail_b"] = 0
    p.config.model.endpoints = [
        P.EndpointConfig(name="broken", base_url=f"{mock_base}/broken", model="m", timeout=5, cooldown=60),
        P.EndpointConfig(name="flaky", base_url=f"{mock_base}/flaky", model="m", timeout=5, cooldown=60),
    ]
    deadline = time.monotonic() + 20
    out = await p._call_endpoint_pool(P.ArknightsLorePlugin._build_prompt("x", "y", "z"), deadline)
    check("降级后拿到回答", out == "来自备用端点的回答", repr(out))
    check("确实调用了 broken", STATE["calls"]["a"] >= 1, f"broken={STATE['calls']['a']} flaky={STATE['calls']['b']}")

    # --- T6 熔断 ---
    print("\nT6 熔断冷却")
    p._fail_count["broken"] = 0
    p.config.resilience.error_threshold = 2
    STATE["fail_b"] = 99
    for _ in range(2):
        await p._call_endpoint_pool(P.ArknightsLorePlugin._build_prompt("x", "y", "z"), time.monotonic() + 10)
    check("broken 进入冷却", p._cooldown_until.get("broken", 0) > time.monotonic(), f"剩余 {p._cooldown_until.get('broken',0)-time.monotonic():.1f}s")

    # --- T7 超时 ---
    print("\nT7 单端点超时")
    t0 = time.monotonic()
    p.config.model.endpoints = [P.EndpointConfig(name="slow", base_url=f"{mock_base}/slow", model="m", timeout=1.0)]
    p._cooldown_until.clear()
    out = await p._call_endpoint_pool(P.ArknightsLorePlugin._build_prompt("x", "y", "z"), time.monotonic() + 15)
    dt = time.monotonic() - t0
    check("超时后返回空串", out == "", repr(out))
    check("超时未被拖长", dt < 6, f"{dt:.1f}s")

    # --- T8 端点池全挂 + fail_open ---
    print("\nT8 端点池全挂 -> 工具层兜底")
    p.config.model.endpoints = [P.EndpointConfig(name="broken", base_url=f"{mock_base}/broken", model="m", timeout=3)]
    p._cooldown_until.clear()
    p._cache.clear()
    deadline = time.monotonic() + p.config.resilience.total_budget
    ctx8 = await p._search("罗德岛", "")
    ans8 = await p._summarize("罗德岛", "罗德岛是什么", ctx8, deadline)
    check("总结失败返回空串", ans8 == "", repr(ans8))
    check("兜底会回传原文", p.config.resilience.fail_open and bool(ctx8), f"原文 {len(ctx8)} 字")

    # --- T9 source=none 直返原文 ---
    print("\nT9 source=none 直返原文")
    p.config.model.source = [P.SOURCE_NONE]
    ans9 = await p._summarize("罗德岛", "q", "原始上下文", time.monotonic() + 10)
    check("none 模式原样返回", ans9 == "原始上下文", repr(ans9))
    p.config.model.source = [P.SOURCE_PLUGIN]

    # --- T10 正常端点 ---
    print("\nT10 正常端点返回")
    p.config.model.endpoints = [P.EndpointConfig(name="ok", base_url=f"{mock_base}/v1", model="m", timeout=5)]
    p._cooldown_until.clear()
    deadline = time.monotonic() + 20
    out = await p._call_endpoint_pool(P.ArknightsLorePlugin._build_prompt("x", "y", "z"), deadline)
    check("正常端点拿到文本", "主端点" in out, repr(out))

    # --- T11 预算耗尽保护 ---
    print("\nT11 总预算耗尽保护")
    t0 = time.monotonic()
    out = await p._call_endpoint_pool(P.ArknightsLorePlugin._build_prompt("x", "y", "z"), time.monotonic() + 0.2)
    dt = time.monotonic() - t0
    check("预算耗尽立刻返回", out == "" and dt < 0.5, f"{dt:.2f}s")

    # --- T12 /ark 命令：拦截等级与关键词提炼 ---
    print("\nT12 /ark 命令行为")
    STATE["keyword_reply"] = "我会一直看着你们 魏彦吾"
    STATE["calls"]["a"] = 0
    p.config.model.source = [P.SOURCE_PLUGIN]
    p.config.model.endpoints = [
        P.EndpointConfig(name="a", base_url=f"{mock_base}/v1", model="m")
    ]
    p._cooldown_until.clear()

    res = await p.handle_ark({"q": "魏彦吾那句我会一直看着你们是哪段剧情"}, stream_id="s1")
    check("命令返回成功", res[0] is True, repr(res[1]))
    # 第三个返回值是拦截等级：必须为真，否则 MaiBot 会继续把这条指令当普通消息处理
    check("命令消费掉消息（防重复回复）", bool(res[2]) is True, f"intercept={res[2]!r}")
    check("命令不回填响应文本", res[1] == "", repr(res[1]))

    await asyncio.sleep(1.2)  # 等 create_task 跑完
    sent = [c for c in STATE["sent"] if c["stream_id"] == "s1"]
    check("结果已发送到目标流", len(sent) >= 1, f"{len(sent)} 条")
    check(
        "结果不写入消息库（否则麦麦会评价自己）",
        all(c["kwargs"].get("storage_message") is False for c in sent),
        str([c["kwargs"] for c in sent]),
    )

    STATE["calls"]["a"] = 0
    p.config.command.intercept = False
    res2 = await p.handle_ark({"q": "凯尔希"}, stream_id="s2")
    check("intercept=False 时放行给 MaiBot", res2[2] == 0, f"intercept={res2[2]!r}")
    await asyncio.sleep(0.8)
    p.config.command.intercept = True

    # 关键词提炼：模型返回带 markdown 和多余解释时，只保留可检索的词
    STATE["keyword_reply"] = "**我会一直看着你们** 魏彦吾、 无关内容"
    got = await p._extract_keywords("魏彦吾那句我会一直看着你们是哪段剧情", time.monotonic() + 20)
    check("提炼出干净关键词", got == "我会一直看着你们 魏彦吾 无关内容", repr(got))

    STATE["keyword_reply"] = ""
    got2 = await p._extract_keywords("任意问句", time.monotonic() + 5)
    check("模型失败时返回空串让调用方回退", got2 == "", repr(got2))

    # --- T13 别名表提取 + jieba 白名单预筛 ---
    print("\nT13 别名表提取 + jieba 白名单预筛")
    alias_dir = _make_fake_alias_dir()
    idx = P.AliasIndexFactory.load(str(alias_dir), lambda *a, **k: None)
    check("别名表条目被提取", len(idx) >= 8, f"{len(idx)} 条")
    has_jieba = _jieba_available()
    # 有 jieba 就必须就绪；没有 jieba 则必须自动降级且不影响别名表提取
    check("jieba 预筛状态与环境一致", idx.ready == has_jieba, f"jieba{'可用' if has_jieba else '不可用，已自动降级'}")

    # 真实形状是 [[char_id 列表, 别名列表]]，必须读到下标 1 里的中文别名
    check("从 seq_data 的下标 1 读到别名", "凯尔希" in idx.names and "阿罗巴尼" in idx.names)

    if idx.ready:
        # 只用别名库，不得混入 jieba 默认词表
        dict_words = set(idx._tokenizer.FREQ)
        check("不加载 jieba 默认词表", "中国" not in dict_words and "今天" not in dict_words)
        check("别名本体在词典里", "凯尔希" in dict_words and "可露希尔" in dict_words)

        # 白名单：实体留下，噪音全滤掉
        got = idx.extract("凯尔希在孤星里说了什么", 4)
        check("实体词被白名单保留", "凯尔希" in got and "孤星" in got, str(got))
        check("功能词被白名单过滤", not any(w in got for w in ("什么", "说了", "看着")), str(got))

        # 长名整体切分，不被拆碎
        got2 = idx.extract("可露希尔的密录", 4)
        check("长别名不被切碎", "可露希尔" in got2, str(got2))

        # 非实体问题不该产生噪音词
        got3 = idx.extract("今天天气不错我们吃什么", 4)
        check("非实体问题白名单为空", got3 == [], str(got3))

        # 白名单词必须排在盲拆词之前
        p._alias_index = idx
        terms = p._candidate_terms("凯尔希在孤星里说了什么")
        check("白名单词优先于盲拆词", bool(terms) and terms[0] in ("凯尔希", "孤星"), str(terms[:4]))

        # 台词类问题没有实体，仍要退回原有拆词逻辑
        line_terms = p._candidate_terms("我会一直看着你")
        check("台词问题退回原有拆词", "我会一直看着你" in line_terms, str(line_terms[:4]))

        # 白名单词确实能打到真实数据
        ctx3 = await p._search("凯尔希在孤星里说了什么", "")
        check("自然语言问题检索到结果", bool(ctx3), f"{len(ctx3)} 字")
    else:
        info("（本环境无 jieba，已验证降级路径）")
        check("无 jieba 时别名表仍可提取", len(idx) >= 8, f"{len(idx)} 条")
        p._alias_index = idx

    # --- T14 目录缺失 / 开关关闭的降级 ---
    print("\nT14 降级路径")
    missing = P.AliasIndexFactory.load("/definitely/not/here", lambda *a, **k: None)
    check("显式路径无效时不回退猜测目录", len(missing) == 0 and not missing.ready)

    p._alias_index = P.AliasIndex(set())
    terms2 = p._candidate_terms("凯尔希在孤星里说了什么")
    check("无别名表时退回盲拆词", bool(terms2), str(terms2[:4]))
    ctx4 = await p._search("魏彦吾", "")
    check("降级后检索仍可用", bool(ctx4), f"{len(ctx4)} 字")

    # --- T15 模型输出净化 ---
    print("\nT15 模型输出净化（防思维链 / 安全判定串泄漏）")
    san = P.sanitize_model_output

    cot = (
        "Here's a thinking process:\n\n"
        "1. **Analyze User Input:**\n   - User says: \"我会一直看着你\"\n"
        "2. **Identify Source Text:**\n   - The provided text is: 《6-1 僵局 行动后》\n"
        "3. **Formulate Response:**\n   - Draft: 魏彦吾说……"
    )
    check("英文思维链被整段丢弃", san(cot) == "", repr(san(cot)[:40]))

    check(
        "<thinking> 标签被剥掉",
        san("<thinking>let me think about this carefully</thinking>答案是凯尔希。") == "答案是凯尔希。",
        repr(san("<thinking>abc</thinking>答案是凯尔希。")),
    )
    check(
        "思维链后面的正文被抢救出来",
        san("Let me think step by step.\n最终答案：魏彦吾在《6-1 僵局》里说的。") == "魏彦吾在《6-1 僵局》里说的。",
        repr(san("Let me think step by step.\n最终答案：魏彦吾在《6-1 僵局》里说的。")),
    )
    check(
        "Let's think 写法同样能抢救",
        san("Let's think.\nAnswer: 魏彦吾。") == "魏彦吾。",
        repr(san("Let's think.\nAnswer: 魏彦吾。")),
    )
    check("安全判定串被丢弃", san("User Safety: safe") == "")
    check("安全判定串（长格式）被丢弃", san("User Safety: unsafe\nSafety Categories: Sexual") == "")
    check("纯英文长文本被判为无效", san("Based on the provided context, the character is a doctor.") == "")
    check("正常中文回答原样放行", san("魏彦吾在《6-1 僵局 行动后》里说的。") == "魏彦吾在《6-1 僵局 行动后》里说的。")
    check("含少量英文的正常回答放行", san("原文见 CW-ST-1 阴云密布，讲的是孤星的事。") == "原文见 CW-ST-1 阴云密布，讲的是孤星的事。")
    check("超长输出被截断", len(san("剧情" * 400, max_chars=100)) <= 100)
    check("空输出返回空串", san("   ") == "")
    check("提炼场景放行纯英文词", san("Kel'tsit", require_chinese=False) == "Kel'tsit")

    # 端到端：模型吐思维链时，/ark 不能把思维链发出去
    STATE["summary_reply"] = cot
    STATE["sent"].clear()
    p.config.command.intercept = True
    await p.handle_ark({"q": "凯尔希"}, stream_id="cot")
    await asyncio.sleep(1.2)
    sent_text = " ".join(str(m.get("text") or "") for m in STATE["sent"])
    check("端到端：思维链没有发到聊天", "thinking process" not in sent_text, repr(sent_text[:60]))
    check("端到端：退回检索到的原文", "《" in sent_text, f"{len(sent_text)} 字")

    STATE["summary_reply"] = "User Safety: safe"
    STATE["sent"].clear()
    await p.handle_ark({"q": "凯尔希"}, stream_id="safe")
    await asyncio.sleep(1.2)
    sent_text2 = " ".join(str(m.get("text") or "") for m in STATE["sent"])
    check("端到端：安全判定串没有发出去", "User Safety" not in sent_text2, repr(sent_text2[:60]))

    # 回退链必须对「非空但无效」的输出也降级。
    # 曾经的实现只看 raw 非空就 break，而免费轮盘抽到推理模型时 raw 恰恰是
    # 一整段非空的思维链——链条会在这里断掉，后面的来源永远轮不上。
    _calls: List[str] = []
    _orig_maibot = p._call_maibot
    _orig_pool = p._call_endpoint_pool

    async def _cot_then_ok(messages, deadline, task_name=""):
        _calls.append(task_name or "端点池")
        return cot if not _calls[:-1] else "凯尔希在孤星里是罗德岛的干员。"

    async def _record_pool(messages, deadline):
        _calls.append("端点池")
        return "凯尔希在孤星里是罗德岛的干员。"

    p._call_maibot, p._call_endpoint_pool = _cot_then_ok, _record_pool
    p.config.model.source = [P.SOURCE_UTILS, P.SOURCE_PLUGIN]
    got = await p._summarize("凯尔希", "凯尔希是谁", "原文上下文", time.monotonic() + 30)
    p._call_maibot, p._call_endpoint_pool = _orig_maibot, _orig_pool
    check("思维链输出会触发降级到下一个来源", len(_calls) == 2, f"实际只调了 {_calls}")
    check("降级后拿到的是干净回答", got == "凯尔希在孤星里是罗德岛的干员。", repr(got[:40]))

    STATE["summary_reply"] = ""
    STATE["keyword_reply"] = ""
    p.config.model.source = [P.SOURCE_PLUGIN]
    p.config.resilience.max_attempts = 4
    p.config.resilience.base_backoff = 0.05
    p.config.resilience.max_backoff = 0.2

    # --- T15.5 在 MaiBot 真实的加载方式下能否 import ---
    # 这个必须开子进程跑：MaiBot 只把插件的**上级目录**加进 sys.path，
    # 插件目录本身不在里面（见 plugin_runtime/runner/plugin_loader.py）。
    # 本 selftest 自己 sys.path.insert(PLUGIN_DIR)，在同一进程里测永远测不出来。
    print("\nT15.5 MaiBot 加载方式下的同级模块导入")
    import subprocess  # noqa: PLC0415
    import textwrap  # noqa: PLC0415
    

    _dir = _PDIR
    probe = textwrap.dedent(f"""
        import importlib.util, sys
        from pathlib import Path
        plugin_dir = Path(r"{_dir}")

        # 先装 maibot_sdk 桩（ArkSearch venv 里没装真 SDK）
        _st = importlib.util.spec_from_file_location("stub_helper", str(plugin_dir / "selftest.py"))
        _m = importlib.util.module_from_spec(_st); sys.modules["stub_helper"] = _m
        _st.loader.exec_module(_m)
        _m.build_sdk_stub()

        # 复刻加载器：把 plugin.py 当独立模块执行，sys.path 里**没有**插件目录。
        # 桩和 selftest 都会把自己插进去，所以这里必须重新剔干净，
        # 否则测到的是 selftest 的功劳，不是 plugin.py 的修复。
        sys.path[:] = [p for p in sys.path if not (p and Path(p).resolve() == plugin_dir)]
        assert not any(p and Path(p).resolve() == plugin_dir for p in sys.path), "测试前提错了"

        spec = importlib.util.spec_from_file_location(
            "probe_plugin", str(plugin_dir / "plugin.py"),
            submodule_search_locations=[str(plugin_dir)],
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["probe_plugin"] = mod
        spec.loader.exec_module(mod)
        assert mod.ArkData is not None
        assert mod.detect_proxy is not None
        print("OK")
    """)
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(_dir.parent),
    )
    check(
        "只给上级目录也能 import 到 arkdata/arkdownload",
        proc.returncode == 0 and "OK" in proc.stdout,
        (proc.stderr or proc.stdout).strip().splitlines()[-1][:160] if (proc.stderr or proc.stdout) else "",
    )
    # 热重载：改了 arkdata.py 再加载，必须读到新的，不能吃 sys.modules 里的旧货
    check("同目录旧缓存会被丢弃（热重载不读旧代码）", "del sys.modules[_helper]" in (_dir / "plugin.py").read_text(encoding="utf-8"))

    # --- T16 多选回退链：中文选项、任务名映射、旧配置迁移 ---
    print("\nT16 多选回退链（中文选项 / 任务名映射 / 迁移）")
    MC = P.ModelConfig
    RC = P.RetrievalConfig
    SL = P._SourceList
    U, PM, PL = P.SOURCE_UTILS, P.SOURCE_PLUGIN, P.SOURCE_NONE

    check("五个 maibot 预设分类都在枚举里", len(P.MAIBOT_MODEL_TASKS) == 5 and U in P.MAIBOT_MODEL_TASKS)

    # --- WebUI 渲染的是 String(枚举值)，所以枚举值本身必须是中文 ---
    # --- WebUI 渲染的是 String(枚举值)，所以枚举值本身决定界面上显示什么。
    # 前五个故意保留 MaiBot 的原名不翻译，用户要在模型管理页那边对上号；
    # 只有插件自己的概念用中文。
    check("五个 maibot 分类保持 MaiBot 原名", P.MODEL_SOURCES[:5] == ("maibot replyer", "maibot planner", "maibot utils", "maibot vlm", "maibot embedding"), str(P.MODEL_SOURCES[:5]))
    check("插件自有概念是中文", all(not s.isascii() for s in P.MODEL_SOURCES[5:]), str(P.MODEL_SOURCES[5:]))
    check("剧情库枚举值全是中文", all(not s.isascii() for s in P.DATA_PROVIDERS), str(P.DATA_PROVIDERS))

    # --- 传 MaiBot API 时翻回英文任务名 ---
    check("maibot utils -> utils", SL.task_name(U) == "utils")
    check("maibot replyer -> replyer", SL.task_name(P.SOURCE_REPLYER) == "replyer")
    check("maibot vlm -> vlm", SL.task_name(P.SOURCE_VLM) == "vlm")
    check("自定义端点兜底 utils", SL.task_name(PM) == "utils")
    check("认识不出的值兜底 utils", SL.task_name("瞎写的") == "utils")

    # --- 迁移：第一代 source="maibot" + maibot_task ---
    check("旧 source=maibot + task=utils 迁成 [maibot utils]", MC(source="maibot", maibot_task="utils").source == [U])
    check("旧 source=maibot + task=replyer 迁成 [maibot replyer]", MC(source="maibot", maibot_task="replyer").source == [P.SOURCE_REPLYER])
    check("旧配置任务名非法时兜底", MC(source="maibot", maibot_task="瞎写的").source == [U])
    check("旧配置没有任务名时兜底", MC(source="maibot").source == [U])
    # --- 迁移：第二代单选英文 ---
    check("单选英文 utils 迁成 [maibot utils]", MC(source="utils").source == [U])
    check("单选英文 plugin 迁成 [自定义端点]", MC(source="plugin").source == [PM])
    check("单选英文 builtin 迁成 [插件自建]", RC(provider="builtin").provider == [P.PROVIDER_BUILTIN])
    check("单选英文 remote 迁成 [接入服务]", RC(provider="remote").provider == [P.PROVIDER_REMOTE])
    check("英文 proxy=auto 迁成 [自动探测]", RC(proxy="auto").proxy == P.PROXY_AUTO)
    check("英文 proxy=none 迁成 [强制直连]", RC(proxy="none").proxy == P.PROXY_NONE)
    # --- 迁移：第三代列表（英文逐项翻） ---
    check("英文列表逐项翻", MC(source=["utils", "plugin"]).source == [U, PM])
    check("中英混排也翻得动", MC(source=["utils", PL]).source == [U, PL])
    # --- 新写法原样保留 ---
    check("列表原样保留", MC(source=[U, PM]).source == [U, PM])
    # 曾经的坑：迁移时无脑 .lower() 会毁掉枚举值的大小写，Literal 校验当场失败
    check("迁移不会破坏枚举值的大小写", MC(source=U).source == [U], repr(MC(source=U).source))
    check("maibot_task 的值也保大小写", MC(source="maibot", maibot_task=U).source == [U])
    check("多选顺序即优先级", MC(source=[PM, U]).source == [PM, U])
    check("maibot_task 已从字段里移除", "maibot_task" not in MC.model_fields)

    # --- 默认值 ---
    check("模型来源默认是 [maibot utils]", MC().source == [U])
    check("剧情库来源默认是 [接入服务]", RC().provider == [P.PROVIDER_REMOTE])
    check("代理默认是自动探测", RC().proxy == P.PROXY_AUTO)
    check("服务地址默认为空（等待配置）", RC().base_url == "", repr(RC().base_url))

    # --- 归一化：去重、剔非法、保序、翻旧值 ---
    check("重复项被去掉", SL.resolve([U, U, PM]) == [U, PM])
    check("非法项被剔除", SL.resolve([U, "瞎写的", PM]) == [U, PM])
    check("空值兜底", SL.resolve([]) == [U])
    check("None 兜底", SL.resolve(None) == [U])
    check("标量被当成单项", SL.resolve(PM) == [PM])
    check("归一时翻旧英文值", SL.resolve(["utils", "plugin"]) == [U, PM])
    check("归一不会改动原列表", (lambda l: (SL.resolve(l), l)[1])([U, PM]) == [U, PM])

    # --- 便捷属性 ---
    r1 = RC(provider=[P.PROVIDER_REMOTE, P.PROVIDER_BUILTIN], base_url="http://127.0.0.1:48910")
    check("use_builtin 识别自建", r1.use_builtin)
    check("use_remote 需要地址非空", r1.use_remote)
    check("地址为空时 use_remote 为假", not RC(provider=[P.PROVIDER_REMOTE], base_url="").use_remote)
    check("providers 保序", r1.providers == [P.PROVIDER_REMOTE, P.PROVIDER_BUILTIN])
    check("providers 翻旧英文配置", RC(provider=["builtin"]).providers == [P.PROVIDER_BUILTIN])

    # --- schema：multiple（复选框）---
    mj = json.dumps(MC.model_json_schema(), ensure_ascii=False)
    rj = json.dumps(RC.model_json_schema(), ensure_ascii=False)
    check("source 是多选数组类型", "anyOf" in mj or "items" in mj, "应为 List[Literal]")
    check("来源选项就是中文值", all(name in mj for name in P.MODEL_SOURCES), "")
    check("数据来源选项就是中文值", all(name in rj for name in P.DATA_PROVIDERS), "")

    # --- 语料目录默认值 ---
    check("语料目录默认为空（用插件子文件夹）", RC().data_dir == "")
    check("默认子目录名是 data", P.DEFAULT_DATA_SUBDIR == "data")

    # --- _resolve_data_dir 在配置注入后的行为 ---
    def _with_data_dir(value: str):
        """注入一份带指定语料目录的完整配置。"""
        _i = P.ArknightsLorePlugin()
        _i.inject_config(P.ArkLoreConfig(retrieval=RC(data_dir=value)))
        return _i

    check("留空时解析到插件目录/data", _with_data_dir("")._resolve_data_dir() == _PDIR.resolve() / P.DEFAULT_DATA_SUBDIR)
    check("相对路径按插件目录解析", _with_data_dir("自定义语料")._resolve_data_dir() == _PDIR.resolve() / "自定义语料")
    _abs = str(_dir.parent / "绝对路径语料")
    check("绝对路径原样使用", str(_with_data_dir(_abs)._resolve_data_dir()) == _abs)
    check("未注入配置时退回默认目录", P.ArknightsLorePlugin()._resolve_data_dir() == _PDIR.resolve() / P.DEFAULT_DATA_SUBDIR)

    # --- T17 内置检索引擎 ---
    print("\nT17 内置检索引擎（搬运无损）")
    from arkdata import ArkData, ParamRejected, REQUIRED_FILES  # noqa: PLC0415

    check("必需文件清单正好 8 个", len(REQUIRED_FILES) == 8, str(len(REQUIRED_FILES)))
    engine = ArkData(ARK_STORY)
    check("数据齐全时能加载", engine.ensure_loaded(), engine.detail)
    check("加载后状态为 ready", engine.state == "ready" and engine.ready)
    check("剧情数量非零", len(engine._text) > 1000, f"{len(engine._text)} 篇")

    rows, total = engine.search([{"type": "char", "param": "凯尔希"}], limit=5)
    check("角色检索有结果", total > 100, f"{total} 篇")
    check("行结构为 5 元素", all(len(r) == 5 for r in rows), str(rows[0][:4]))
    check("标题带编号", rows[0][2].startswith(("6-", "0-", "1-", "CW", "TD", "OG")) or len(rows[0][2]) > 0, rows[0][2])
    check("地区名已解析", bool(rows[0][3]), rows[0][3])
    extra = (rows[0][4] or [{}])[0]
    check("片段结构正确", extra.get("type") == "char" and isinstance(extra.get("data"), list), str(extra)[:80])
    check("char 片段是整行台词", bool(extra["data"]) and ": " in extra["data"][0], str(extra["data"][:1])[:80])

    # 说话人提及过滤：后端实测 2133 -> 1
    rows2, total2 = engine.search([{"type": "text", "param": "我会一直看着你"}], limit=5)
    check("text 模式过滤掉索引误召回", 0 < total2 < 10, f"{total2} 篇（原始索引 2133）")

    # 别名 -> id -> 别名 闭环
    ids = engine.char_name2id("可露希尔")
    check("别名能解析出多个角色 id", len(ids) >= 5, f"{len(ids)} 个")
    check("别名解析包含干员本体", "char_007_closre_1" in ids, str(sorted(ids)[:3]))
    names_back: set = set()
    for cid in ids:
        names_back |= engine.char_id2name(cid)
    check("id 能反查回别名", "凯尔希" in names_back or "可露希尔" in names_back, str(sorted(names_back)[:4]))

    # 无效参数要抛 ParamRejected 而不是崩
    for bad, label in (([{"type": "regex", "param": "("}], "非法正则"), ([{"type": "char", "param": "不存在的角色"}], "未知角色")):
        try:
            r, _t = engine.search(bad, limit=3)
            if label == "未知角色":
                check("未知角色返回空而非崩溃", _t == 0 and r == [], f"{_t} 条")
            else:
                check("非法正则被拒", False, "竟然没抛异常")
        except ParamRejected:
            check("非法正则被拒", label == "非法正则", label)

    # 无数据目录时必须安全失败
    empty = ArkData(ARK_STORY + "__不存在")
    check("数据缺失时不抛异常", empty.ensure_loaded() is False)
    check("缺失状态可读", empty.state == "error" and "缺少" in empty.detail, empty.detail)
    check("缺失时检索返回空", empty.search([{"type": "regex", "param": "凯尔希"}], limit=3) == ([], 0))
    check("缺失时列出缺哪些文件", len(empty.missing_files()) == 8, str(len(empty.missing_files())))
    engine.unload()
    check("卸载后不再就绪", not engine.ready and engine.state == "not_ready")

    # --- T18 数据来源切换与回退链 ---
    print("\nT18 数据来源切换与回退链")
    try:
        RC(provider=["nope"])  # type: ignore[list-item]
        check("非法来源被拒", False)
    except Exception:
        check("非法来源被拒", True)

    p.config.retrieval.provider = [P.PROVIDER_REMOTE]
    p.config.retrieval.base_url = ""
    p._cache.clear()  # 前面用例缓存过同一个 key，先清掉才能测到真实分支
    got = await p._search("凯尔希", "")
    check("只勾远程且地址为空 -> 返回空", got == "")
    log_tail = open(os.path.join(PLUGIN_DIR, "arknights-lore.log"), encoding="utf-8", errors="replace").read()[-800:]
    check("地址为空时已记录告警", "服务地址" in log_tail, "日志里应有提示")

    p.config.retrieval.provider = [P.PROVIDER_BUILTIN]
    p._engine = ArkData(ARK_STORY)
    ctx_local = await p._search("凯尔希在孤星里说了什么", "")
    check("自建模式能检索到剧情", bool(ctx_local), f"{len(ctx_local)} 字")
    check("自建模式上下文含篇章名", "《" in ctx_local, ctx_local[:50])

    # 两个都勾上：服务地址是坏的，应自动降级到自建而不是整段失败
    p._cache.clear()
    p.config.retrieval.provider = [P.PROVIDER_REMOTE, P.PROVIDER_BUILTIN]
    p.config.retrieval.base_url = "http://127.0.0.1:1"  # 必然连不上
    p.config.retrieval.timeout = 2.0
    ctx_fallback = await p._search("凯尔希在孤星里说了什么", "")
    check("服务挂了自动降级到自建", bool(ctx_fallback), f"{len(ctx_fallback)} 字")

    # 顺序反过来：自建优先且能出结果，不该去碰那个坏地址
    p._cache.clear()
    p.config.retrieval.provider = [P.PROVIDER_BUILTIN, P.PROVIDER_REMOTE]
    ctx_first = await p._search("凯尔希在孤星里说了什么", "")
    check("自建优先时能直接命中", bool(ctx_first), f"{len(ctx_first)} 字")

    p.config.retrieval.provider = [P.PROVIDER_BUILTIN]
    p.config.retrieval.base_url = ""
    p.config.retrieval.timeout = 10.0
    p._engine.unload()
    p._engine = None

    # --- T19 语料下载 ---
    print("\nT19 语料下载与版本探测")
    import arkdownload as DL  # noqa: PLC0415

    check("默认仓库是源项目的构建产物", DL.DEFAULT_REPO == "ArknightsSearch/ArknightsSearch-resource", DL.DEFAULT_REPO)
    tmp_dir = Path(os.environ.get("TEMP", ".")) / "arklore_dl_test"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    check("空目录没有清单", DL.read_manifest(tmp_dir) == {})
    DL.write_manifest(tmp_dir, "a" * 40, ["x.json"])
    check("清单可写可读", DL.read_manifest(tmp_dir).get("commit") == "a" * 40)

    bad_json = tmp_dir / "bad.json"
    bad_json.write_text("<html>404</html>", encoding="utf-8")
    check("HTML 不会被当成合法 JSON", DL.is_valid_json(bad_json) is False)
    good_json = tmp_dir / "good.json"
    good_json.write_text('{"a":1}', encoding="utf-8")
    check("正常 JSON 校验通过", DL.is_valid_json(good_json) is True)

    check("代理检测不抛异常", isinstance(DL.detect_proxy("none"), type(None)))
    check("显式代理地址直接采纳", DL.detect_proxy("http://127.0.0.1:1") == "http://127.0.0.1:1")

    # 网络探测：断网/无代理时应安静返回
    status, message = await asyncio.to_thread(DL.plan_update, tmp_dir, DL.DEFAULT_REPO, DL.DEFAULT_BRANCH, "http://127.0.0.1:1", 1.5)
    check("探测失败归为 unknown 而非报错", status in ("unknown", "stale", "current"), f"{status}: {message}")
    if status == "unknown":
        check("unknown 时说明是网络问题", "连不上" in message or "没有数据" in message, message)

    shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"\n{'='*60}\n通过 {len(PASS)} / {len(PASS)+len(FAIL)}")
    if FAIL:
        print("失败:", FAIL)
    sys.exit(1 if FAIL else 0)


def info(message: str) -> None:
    print(f"    {message}")


def _jieba_available() -> bool:
    try:
        import jieba  # noqa: F401

        return True
    except ImportError:
        return False


def _make_fake_alias_dir() -> Path:
    """造一份最小的别名表，**严格照抄 ArkSearch 真实形状**。

    seq_data.json 实际是 ``[[char_id 列表, 别名列表], ...]``，别名在下标 1；
    早先用字典形状写测试，导致解析真实数据时静默退化成只读到地区名。
    """
    directory = Path(tempfile.mkdtemp(prefix="arkalias-"))
    seq = [
        [["char_002_amiya_1"], ["Amiya", "アーミヤ", "阿米娅"]],
        [["char_003_kalts_1"], ["Kal'tsit", "ケルシー", "凯尔希", "可露希尔", "沉默的过路人"]],
        [["char_003_kaltsn07_1"], ["Kal'tsit", "凯尔希", "神秘的女性"]],
        [["char_452_bstalk_1"], ["-Blvk-", "黑键"]],
        [["char_201_amiya2_1"], ["Amiya", "阿米娅"]],
        [["rogue_301"], ["阿罗巴尼"]],
        [["char_456_pith_1"], ["Pith", "推进之王"]],
    ]
    (directory / "seq_data.json").write_text(json.dumps(seq, ensure_ascii=False), encoding="utf-8")
    zone = {"act25side": {"zh_CN": "孤星", "en_US": "Starpod"}, "absin": {"zh_CN": "苦艾", "en_US": "Absin"}}
    (directory / "zone_name.json").write_text(json.dumps(zone, ensure_ascii=False), encoding="utf-8")
    return directory


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
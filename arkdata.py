"""插件内置的明日方舟剧情检索引擎。

从 ArknightsSearch 后端搬运而来（MIT，版权归原项目所有），去掉了 FastAPI 与
``core.*`` 依赖，改成**惰性加载**的普通类，方便在 MaiBot 的插件 Runner 进程里
直接调用。

原后端在 ``import`` 时就把 47MB JSON 全部载入并建好倒排索引（``data.py`` 末尾的
一串 ``init_*``）。插件不能这么干——Runner 进程是 MaiBot 的热重载目标，import
就吃掉 75MB 内存、拖慢每次改配置。所以这里拆成两步：``download`` 落盘由
:mod:`arkdownload` 负责，本模块只在**真正要检索时**才加载，且加载失败不抛异常、
只置一个可查询的状态。

产出结构与后端 ``/story`` 接口（``require=PC``）逐字节对齐::

    [story_id, story_type, long_name_zh_CN, zone_name_zh_CN, [extra, ...]]

这样插件侧 ``_build_context`` 不需要区分「走 HTTP」还是「走本地」。
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

__all__ = ["ArkData", "REQUIRED_FILES", "LANGS"]

# 本地检索真正需要的文件——正好是后端加载的那 8 个。story_data 要留，
# 它提供标题和地区，是行结构的来源。
REQUIRED_FILES = (
    "story_data.json",
    "text_data.json",
    "text_index.json",
    "zone_name.json",
    "seq_data.json",
    "char_id2story.json",
    "char_name2story.json",
    "zone_index.json",
)

# 后端 support_language 的三个语言代码
LANGS = ("zh_CN", "ja_JP", "en_US")
LANG = "zh_CN"

# ArkSearch 对无法识别的角色名直接抛错（内部是 KeyError），HTTP 侧表现为 500。
# 内置模式下没有 HTTP 层，用自己的异常类型表达同一件事。
class ParamRejected(Exception):
    """参数不合法——正则编错、角色名不存在。属于降级信号，不是服务故障。"""


def _to_set(data: Dict[str, Any]) -> Dict[str, Set[str]]:
    """把 JSON 里的 list 值统一换成 set，和后端 ``to_set`` 一致。"""
    for key in data:
        data[key] = set(data[key])
    return data


class _StoryMeta:
    """一条剧情的元信息。

    搬运自后端 ``StoryData``，只保留生成展示标题用得到的部分。
    """

    SUFFIX = {"zh_CN": ("行动前", "行动后"), "ja_JP": ("戦闘前", "戦闘後"), "en_US": ("Before Operation", "After Operation")}

    __slots__ = ("id", "type", "code", "zone", "long_name")

    def __init__(self, data: Dict[str, Any]) -> None:
        self.id = data["id"]
        self.type = data["type"]
        self.code = data.get("code")
        self.zone = data["zone"]
        self.long_name: Dict[str, str] = {}
        self._init_name(data.get("name") or {})

    def _init_name(self, name: Dict[str, str]) -> None:
        if self.type in ("Memory", "Rogue"):
            self._same(name)
            return
        if not name:
            # 关卡条目没有 name，用编号顶上
            self._same({lang: self.code or "" for lang in LANGS})
            return
        if self.code:
            # 「CW-ST-1 阴云密布」这种：长名带编号
            self.long_name = {lang: f"{self.code} {text}" for lang, text in name.items()}
        else:
            self._same(name)
        # 注意这里不能 return：没有编号的条目（如「愚人节剧情05」）同样要加后缀。
        # beg/end 是同一段剧情的前后两幕，标题要区分。
        if self.id.endswith("beg"):
            self._add_suffix(0)
        elif self.id.endswith("end"):
            self._add_suffix(1)

    def _same(self, names: Dict[str, str]) -> None:
        self.long_name = dict(names)

    def _add_suffix(self, index: int) -> None:
        for lang in self.long_name:
            self.long_name[lang] += " " + self.SUFFIX.get(lang, ("", ""))[index]


class ArkData:
    """惰性加载的本地剧情库。

    典型用法::

        engine = ArkData(data_dir)
        if engine.ensure_loaded():
            rows = engine.search(params, limit=4)

    所有公开方法在未加载时都安全返回空结果，绝不抛异常——调用方需要区分的是
    「没查到」和「库还没准备好」，通过 :attr:`state` 区分而不是靠异常。
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self._lock = threading.Lock()
        self._loaded = False
        self._state = "not_ready"
        self._detail = ""

        # 以下由 _load 填充
        self._story: Dict[str, _StoryMeta] = {}
        self._text: Dict[str, str] = {}
        self._text_index: Dict[str, Set[str]] = {}
        self._zone_name: Dict[str, Dict[str, str]] = {}
        self._char_id2story: Dict[str, Set[str]] = {}
        self._char_name2story: Dict[str, Set[str]] = {}
        self._zone_index: Dict[str, Set[str]] = {}
        self._seq_names: List[Set[str]] = []
        self._seq_ids: List[Set[str]] = []
        self._char_name2seq: Dict[str, Set[int]] = {}
        self._char_id2seq: Dict[str, Set[int]] = {}

    # ---------------------------------------------------------------- 状态 -- #
    @property
    def ready(self) -> bool:
        """是否已成功加载并可检索。"""
        return self._loaded

    @property
    def state(self) -> str:
        """加载状态：``not_ready`` / ``loading`` / ``ready`` / ``error`` / ``missing``。"""
        return self._state

    @property
    def detail(self) -> str:
        """状态的可读说明，用于直接回给用户。"""
        return self._detail

    def missing_files(self) -> List[str]:
        """列出缺失的数据文件。

        Returns:
            缺失的文件名列表；齐全时为空列表。
        """
        return [name for name in REQUIRED_FILES if not (self.data_dir / name).is_file()]

    # ---------------------------------------------------------------- 加载 -- #
    def ensure_loaded(self) -> bool:
        """确保数据已载入内存，可重入且线程安全。

        47MB JSON 的解析要 2~4 秒，必须放进工作线程，不能占住事件循环。

        Returns:
            是否可用。
        """
        if self._loaded:
            return True
        with self._lock:
            if self._loaded:
                return True
            self._state = "loading"
            try:
                self._load()
            except Exception as exc:  # 数据损坏/权限问题，不能让插件整体崩掉
                self._state = "error"
                self._detail = f"加载剧情库失败：{exc}"
                return False
            self._loaded = True
            self._state = "ready"
            self._detail = f"已加载 {len(self._text)} 篇剧情"
            return True

    def _load(self) -> None:
        missing = self.missing_files()
        if missing:
            self._state = "missing"
            raise FileNotFoundError("缺少数据文件：" + "、".join(missing))

        def _read(name: str) -> Any:
            with open(self.data_dir / name, "r", encoding="utf-8") as fh:
                return json.load(fh)

        raw_story = _read("story_data.json")
        text_all = _read("text_data.json")
        self._text = text_all.get(LANG) or {}
        self._text_index = _to_set(_read("text_index.json"))
        self._zone_name = _read("zone_name.json")
        self._char_id2story = _to_set(_read("char_id2story.json"))
        self._char_name2story = _to_set(_read("char_name2story.json"))
        self._zone_index = _to_set(_read("zone_index.json"))

        self._story = {seq: _StoryMeta(v) for seq, v in raw_story.items()}

        # seq_data 是 [char_id列表, 别名列表] 的二元组序列，和 jieba 白名单读的是同一份
        for entry in _read("seq_data.json"):
            ids = entry[0] if isinstance(entry, (list, tuple)) else entry.get("char_id", ())
            names = entry[1] if isinstance(entry, (list, tuple)) else entry.get("name", ())
            self._seq_ids.append(set(ids))
            self._seq_names.append(set(names))

        for index, names in enumerate(self._seq_names):
            for name in names:
                self._char_name2seq.setdefault(name, set()).add(index)
        for index, ids in enumerate(self._seq_ids):
            for cid in ids:
                self._char_id2seq.setdefault(cid, set()).add(index)

    def unload(self) -> None:
        """释放内存。数据更新后调用，避免新旧两份同时驻留。"""
        with self._lock:
            self._story = {}
            self._text = {}
            self._text_index = {}
            self._zone_name = {}
            self._char_id2story = {}
            self._char_name2story = {}
            self._zone_index = {}
            self._seq_names = []
            self._seq_ids = []
            self._char_name2seq = {}
            self._char_id2seq = {}
            self._loaded = False
            self._state = "not_ready"
            self._detail = ""

    # ---------------------------------------------------------------- 检索 -- #
    def char_name2id(self, name: str) -> Set[str]:
        """角色名（含别名）→ 内部角色 id 集合。"""
        result: Set[str] = set()
        for seq in self._char_name2seq.get(name, ()):  # noqa: SIM118 - 展平并集
            result |= self._seq_ids[seq]
        return result

    def char_id2name(self, char_id: str) -> Set[str]:
        """内部角色 id → 该角色的全部别名。"""
        result: Set[str] = set()
        for seq in self._char_id2seq.get(char_id, ()):  # noqa: SIM118
            result |= self._seq_names[seq]
        return result

    _SPACE_RE = re.compile(r"\s")
    # 命中位置前面是「角色名: 」时要排除——那是「某角色说过这个词」，不是剧情提到这个词
    _SECONDARY_RE = r"%s(?!: )(?:.(?!: ))*$"

    def search(self, params: List[Dict[str, str]], limit: int = 20) -> Tuple[List[List[Any]], int]:
        """按参数组检索剧情。

        多个参数之间取**交集**——和后端一致，所以「char=凯尔希 + regex=孤星」
        这种过严组合同样会打空，调用方需要自己逐级放宽。

        Args:
            params: 参数组，每项形如 ``{"type": "regex", "param": "凯尔希"}``。
                ``type`` 取 text/char/zone/regex。
            limit: 最多返回几条。

        Returns:
            ``(行列表, 命中总数)``。行结构与后端 ``/story`` 的 ``require=PC`` 一致。

        Raises:
            ParamRejected: 正则非法或角色名不存在。
        """
        if not self.ensure_loaded():
            return [], 0

        text_group = [p["param"] for p in params if p["type"] == "text"]
        # 每个检索词命中一组剧情，多个词之间取交集。注意这里必须是**列表**：
        # 集合里装集合不可哈希，而且各词的命中集要留到最后才求交。
        result: List[Set[str]] = [self._text_index.get(i, set()) for i in set(self._SPACE_RE.sub("", " ".join(text_group)))] if text_group else []
        for param in params:
            if param["type"] == "text":
                continue
            result.append(self._search_one(param))

        if len(result) > 1:
            result = result[0].intersection(*result[1:])
        elif result:
            result = result[0]
        else:
            result = set()

        if text_group:
            result = {s for s in result if not self._is_speaker_mention(text_group, s)}

        ordered = sorted(result)
        total = len(ordered)
        extra = _ExtraBuilder(self)
        extra.prepare(params)
        return [self._format(extra, seq) for seq in ordered[:limit]], total

    def _is_speaker_mention(self, text_group: List[str], seq: str) -> bool:
        """判断这条命中是否该从 text 模式结果里剔除。

        判据搬运自后端 ``search()`` 末尾那段，缩进很反直觉，务必照抄语义：

        - 词在正文里**找不到**（``i1 == -1``）→ 剔除。倒排索引收录的是切分后的
          片段，原文不一定还有连续的完整拼写，所以要回正文确认一次。
        - 找到但后面没有 ``": "`` → 保留：这是剧情正文，不是「角色名: 台词」结构。
        - 找到且构成说话人标记、且正文里另有一处非说话人的提及 → 剔除。

        Args:
            text_group: text 模式的检索词。
            seq: 剧情在数据集中的键。

        Returns:
            是否剔除。
        """
        text = self._text.get(seq, "")
        for term in text_group:
            i1 = text.find(term)
            if i1 != -1:
                i2 = text.find("\n", i1 + len(term))
                i3 = text.find(": ", i1 + len(term), i2)
                if i3 == -1 or re.search(self._SECONDARY_RE % re.escape(term), text, flags=re.MULTILINE):
                    continue  # 确实是正文提及，换下一个词判断
            # i1 == -1 时也会走到这里——原样保留后端的这个行为
            return True
        return False

    def _search_one(self, param: Dict[str, str]) -> Set[str]:
        kind, value = param["type"], param["param"]
        if kind == "char":
            # char_name2id 给出的是**内部角色 id**，所以查 char_id2story；
            # 再把角色名本身在 char_name2story 里的直查结果并进来。
            # 少了后者，角色名恰好等于某篇剧情标题时会漏（实测差 3~200 篇）。
            hits = [self._char_id2story.get(i, set()) for i in self.char_name2id(value)]
            hits.append(self._char_name2story.get(value, set()))
            return set().union(*hits) if hits else set()
        if kind == "zone":
            return set(self._zone_index.get(value, set()))
        if kind == "regex":
            try:
                regex = re.compile(value, flags=re.M)
            except re.error as exc:
                raise ParamRejected(str(exc)) from exc
            return {k for k, text in self._text.items() if regex.search(text)}
        raise ParamRejected(f"未知检索类型：{kind}")

    def _format(self, extra: "_ExtraBuilder", seq: str) -> List[Any]:
        """把一条剧情组装成后端同构的行。"""
        meta = self._story.get(seq)
        if meta is None:
            return []
        try:
            zone = self._zone_name.get(meta.zone, {}).get(LANG, meta.zone)
        except Exception:  # zone_id 在 zone_name 里缺失属于数据问题，不该让整次检索失败
            zone = meta.zone
        return [meta.id, meta.type, meta.long_name.get(LANG, ""), zone, extra.build(seq)]


class _ExtraBuilder:
    """从原文里抠出命中片段。

    搬运自后端 ``extra.py``。后端用 pydantic 模型再由 FastAPI 序列化，这里直接产
    出等价的 dict，省一层模型开销。
    """

    def __init__(self, engine: "ArkData") -> None:
        self._engine = engine
        self._specs: List[Tuple[str, str]] = []

    def prepare(self, params: List[Dict[str, str]]) -> None:
        """按参数组规划片段抽取方式。

        Args:
            params: 与 :meth:`ArkData.search` 相同的参数组。
        """
        self._specs = [(p["type"], p["param"]) for p in params]

    def build(self, seq: str) -> List[Dict[str, Any]]:
        """产出该剧情的所有命中片段。

        Args:
            seq: 剧情在数据集中的键。

        Returns:
            与后端 ``Extra.get()`` 等价的 dict 列表。
        """
        text = self._engine._text.get(seq, "")
        out: List[Dict[str, Any]] = []
        for kind, value in self._specs:
            if kind == "text":
                out.append({"type": "text", "data": self._text_snippets(text, value), "has_more": False, "raw": value})
            elif kind == "regex":
                out.append({"type": "regex", "data": self._regex_snippets(text, value), "has_more": False, "raw": value})
            elif kind == "char":
                out.append({"type": "char", "data": self._char_snippets(text, value), "has_more": False, "raw": value})
        return out

    def _text_snippets(self, text: str, target: str) -> List[List[Optional[str]]]:
        """字面匹配片段，五元组 [上文, 说话人, 命中, 下文, 次行]。"""
        if not target:
            return []
        rows: List[List[Optional[str]]] = []
        forward = 0
        added_index = 0
        while len(rows) < 5:
            at = text.find(target, forward)
            if at == -1:
                break
            forward = at + len(target)

            row: List[Optional[str]] = [target if i == 2 else "" for i in range(5)]
            n2 = text.find("\n", forward)
            row[3] = text[forward:] if n2 == -1 else text[forward:n2]

            # 命中行本身是「角色名: 台词」，说明这是说话人标记，跳过
            if row[3].find(": ") != -1:
                forward = n2
                continue

            n1 = text.rfind("\n", 0, at)
            row[1] = text[:at] if n1 == -1 else text[n1 + 1 : at]
            if n1 != -1:
                n0 = text.rfind("\n", 0, n1)
                if n0 >= added_index:
                    row[0] = text[:n1] if n0 == -1 else text[n0 + 1 : n1]

            if n2 != -1:
                n2 += 1
                n3 = text.find("\n", n2)
                row[4] = text[n2:] if n3 == -1 else text[n2:n3]
                if row[4].find(target) > row[4].find(": "):
                    # 下一行又提到目标词，这段是噪声
                    added_index = n2
                    row[4] = ""
                else:
                    added_index = n3

            rows.append(row)
        return rows

    def _regex_snippets(self, text: str, pattern: str) -> List[List[Optional[str]]]:
        """正则匹配片段，五元组同上。"""
        if not pattern:
            return []
        try:
            regex = re.compile(r"(?:(.+)\n)?^(.*)(" + pattern + r")(.*)$(?:\n(.+))", flags=re.M)
        except re.error as exc:
            raise ParamRejected(str(exc)) from exc
        rows = []
        for group in regex.findall(text)[:5]:
            if len(group[2]) > 50:
                # 长命中做首尾截断，避免整段灌进提示词
                rows.append(list(group[:2]) + [group[2][:25] + f"...[{len(group[2]) - 50}]..." + group[2][-25:]] + list(group[-2:]))
            else:
                rows.append(list(group))
        return rows

    def _char_snippets(self, text: str, name: str) -> List[str]:
        """按角色名抓整行台词。"""
        names: Set[str] = set()
        for char_id in self._engine.char_name2id(name):
            names |= self._engine.char_id2name(char_id)
        if not names:
            return []
        regex = re.compile(r"^(?:%s):.*" % "|".join(re.escape(i) for i in names), flags=re.MULTILINE)
        return regex.findall(text)[:5]

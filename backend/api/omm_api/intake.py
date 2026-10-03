"""发送前的接待判定 v2（ADR-0024）：轮次理解（结构化）→ 路由守卫（确定性）。

首页每次发送先到这里，按「用户这句话想干什么」分四路（``route``）：

- ``start``：明确要做 + 题面够 + 把握够 → 前端创建 Project / TaskRun 启动六阶段；
- ``propose``：像建模题，但没明确要求开始、或判定把握不够 → 首页对话里先复述题意并提议，
  用户下一句确认（「开始」「好的」）才启动（2026-10-02 用户拍板「拿不准先提议」）；
- ``clarify``：想建模但看不出要解决什么 → 首页对话里问缺的信息；
- ``reply``：寒暄 / 问知识 / 问文件 / 拒绝提议 → 首页对话直接回答，不建任务。

v1 的毛病是「拿不准一律建任务」：赛题词优先于问句、长度够就放行、附件即放行、判定失败也
放行，判定还看不到上文——「美赛和国赛有什么区别」「这张图是什么意思」都会建项目并跑完整条
六阶段链路，首页对话里的「好的，开始吧」则孤立判定、题面全丢。v2 把理解和路由拆开：

1. **事件和话语分开**：附件、@ 引用只是证据，单独不构成「开始建模」——文件上传 ≠ 开始建模。
2. **结构化理解**：本地信号先看一遍；拿不准的交池中最弱模型做一次 JSON 判定（带最近几轮
   对话与上一轮提议），只输出结构字段（意图 / 是否要求执行 / 是否承接上文 / 题型 / 缺什么 /
   把握），不再写回复。
3. **路由守卫用规则定**：route 只由本模块的确定性规则给出；判定挂了时只有本地强证据（执行
   措辞 + 任务信号 / 题面 / 附件）才 start，其余先提议或直接回答——不确定绝不硬路由。
4. **升级为任务时带上下文**：确认提议时 goal 用提议时记下的题面（``task_goal``），首页对话
   摘录（``context_excerpt``）随任务交给问题分析节点。

未配置自定义 API 的用户既没有判定模型也没有对话模型：本地规则照样拦寒暄 / 明显问句 / 只传
文件不说要做什么，像题面的照旧进演示链路（2026-10-02 用户拍板）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from .llm import (
    ChatOutcome,
    LlmConfig,
    complete_once,
    config_usable,
    endpoint_strength,
    is_third_party_host,
)

logger = logging.getLogger("omm.intake")

#: 对外保留的旧三值意图：只认 intent 的旧前端照常工作（start ⇔ modeling_task）。
INTENTS = ("modeling_task", "needs_info", "chat")
ROUTES = ("start", "propose", "clarify", "reply")
KINDS = ("chat", "knowledge", "file_analysis", "modeling_task")
SPEECH_ACTS = ("greet", "ask", "command", "confirm", "cancel", "modify", "supplement")
DOMAINS = ("optimization", "prediction", "evaluation", "simulation", "data_analysis", "statistics", "none")
MISSING_FIELDS = ("problem", "objective", "data")

_ROUTE_INTENTS = {
    "start": "modeling_task",
    "propose": "needs_info",
    "clarify": "needs_info",
    "reply": "chat",
}

INTAKE_READ_TIMEOUT_S = 10.0
INTAKE_MAX_TOKENS = 256
#: 判定说「要建模、也要求开始」时，把握达到这一档才直接启动；不到就先提议。
START_CONFIDENCE = 0.6
#: 判定没给 confidence 时按拿不准处理（低于启动线）：没说把握的结论不该一步建任务。
_DEFAULT_CONFIDENCE = 0.5

#: 进入判定提示词的输入截断
_GOAL_EXCERPT_CHARS = 800
#: 达到该长度的输入视为粘贴的题面（问句标记只看句首句尾，执行措辞不再被问句否掉）
_LONG_GOAL_CHARS = 200
#: 短于该长度的输入里，任何问句标记都按问句算；更长的文本里「是否 / 多少 / 如何」常是题面
#: 本身的措辞（「判断是否适合建设」「最多能运送多少」），只认句首句尾的问句形态
_SHORT_TEXT_CHARS = 60
#: 不超过该长度、又没有任务型信号的输入按寒暄处理（「你好」「在吗」）
_TRIVIAL_GOAL_CHARS = 4
#: 每个附件进入判定提示词的正文摘录截断
_ATTACHMENT_EXCERPT_CHARS = 500
#: 进入判定提示词的附件数量上限（更多附件只会稀释信号）
_ATTACHMENT_PROMPT_LIMIT = 5
#: 命中任务型信号还需要的最短长度：「帮我优化一下」命中「优化」却没说优化什么
_TASK_SIGNAL_MIN_CHARS = 10
#: 进判定的最近对话：条数与每条截断（首页对话一轮两条）
_HISTORY_LIMIT = 12
_JUDGE_HISTORY_TURNS = 6
_JUDGE_HISTORY_CHARS = 200
_JUDGE_PENDING_CHARS = 300
#: 是非题式回应（确认 / 拒绝提议）的长度上限：更长的是新内容，不是在回答是非题
_PROPOSAL_ANSWER_MAX_CHARS = 30
#: task_goal 上限：与 CreateTaskRunInput.goal 一致
TASK_GOAL_MAX_CHARS = 4000
#: 从上文拼题面时最多取几条用户发言、合计多少字
_HISTORY_STATEMENT_TURNS = 3
_HISTORY_STATEMENT_CHARS = 2000
#: 首页对话摘录（context_excerpt）：轮数、每侧截断与总上限
_CONTEXT_TURNS = 8
_CONTEXT_USER_CHARS = 600
_CONTEXT_REPLY_CHARS = 400
CONTEXT_EXCERPT_MAX_CHARS = 3000

# ── 词表 ────────────────────────────────────────────────────────────────────

#: 赛题标识：用户在说某道竞赛题。v1 里它直接放行、优先于问句；v2 只作为「题面 / 材料」证据，
#: 「美赛和国赛有什么区别」照样是知识问答。英文条目服务 COMAP 系赛题。
_PROBLEM_MARKERS = (
    "赛题", "a题", "b题", "c题", "d题", "e题", "f题",
    "高教社杯", "数学建模竞赛", "国赛", "美赛", "研赛", "数模",
    "mcm", "icm", "mathorcup", "华数杯", "深圳杯", "电工杯", "认证杯",
)

#: 任务型信号：有求解目标的活。**刻意不含「建模」「模型」这类话题词**——「帮我做个建模」
#: 只报话题、不说对象，正是要问清楚的那类。英文取词根以覆盖变形（optimiz → optimization）。
_TASK_SIGNALS = (
    "优化", "预测", "求解", "调度", "排产", "排班", "选址", "分配", "规划", "评估",
    "分类", "聚类", "拟合", "回归", "仿真", "定价", "配送", "库存", "路径", "决策",
    "最小化", "最大化", "最短", "最优", "灵敏度", "评价体系", "综合评价",
    "optimiz", "predict", "forecast", "schedul", "allocat", "minimiz",
    "maximiz", "cluster", "classif", "regression", "simulat", "routing",
    "assignment", "inventory", "pricing", "sensitivity",
)

#: 问句标记（短文本里出现即算问句）。
_INQUIRY_MARKERS = (
    "怎么", "如何", "为什么", "为啥", "什么", "能不能", "可不可以", "是不是", "有没有",
    "哪些", "哪个", "哪种", "多少", "是否", "推荐", "教我", "学习", "入门", "区别",
    "吗", "呢", "?", "？",
)
#: 长文本只认这些问句形态：句首发问、句尾问号 / 语气词。
_QUESTION_OPENERS = (
    "怎么", "如何", "为什么", "为啥", "请问", "能不能", "可不可以", "是不是", "有没有",
    "什么是", "哪些", "哪个", "哪种",
)
_QUESTION_ENDINGS = ("吗", "呢", "么", "?", "？")

#: 问句标记的子串误命中：「机器学习」的「学习」、「推荐系统」的「推荐」是方法名与对象名。
#: 判问句前先抹掉这些复合词（只影响问句判断，任务型信号照常命中）。
_INQUIRY_COMPOUND_EXCEPTIONS = (
    "机器学习", "深度学习", "强化学习", "迁移学习", "监督学习", "无监督学习",
    "半监督学习", "集成学习", "表示学习", "统计学习", "元学习", "联邦学习",
    "在线学习", "对比学习", "自监督学习", "学习率", "学习曲线",
    "推荐系统", "推荐算法", "推荐模型", "推荐引擎", "推荐列表", "推荐策略",
)

#: 知识型问句：问概念、方法选择、规则、写法——可以直接回答，不需要真去建模求解。
#: 刻意不收「怎么做 / 怎么解」：对着一道具体的题问怎么做，可能是想让系统动手，交判定。
_KNOWLEDGE_MARKERS = (
    "区别", "是什么", "什么是", "什么意思", "啥意思", "含义", "概念", "定义", "原理",
    "怎么写", "如何写", "怎么学", "如何学", "从哪", "入门", "学习", "推荐", "有哪些",
    "哪些方法", "哪种方法", "哪种模型", "什么模型", "什么方法", "什么算法", "用什么", "用哪",
    "适合用", "一般用", "通常用", "优缺点", "怎么理解", "如何理解", "为什么", "介绍一下",
    "解释一下", "讲讲", "讲一下", "说说", "科普", "注意事项", "注意什么", "技巧", "经验",
    "评分标准", "怎么准备", "如何准备", "有什么用", "能做什么",
)

#: 请求措辞：用户在请系统动手。本身不等于要建模（「帮我解释一下 GM(1,1)」是知识问答），
#: 它的作用是让带问号的客气话（「能不能帮我做个预测模型？」）仍算执行意图。
_REQUEST_MARKERS = (
    "帮我", "帮忙", "请你", "请帮", "麻烦", "给我", "替我", "能不能帮", "可以帮", "能帮",
    "请建立", "请求解", "请给出", "请设计", "请制定", "请完成", "请构建", "请计算",
)

#: 执行措辞：明确要系统现在就做（建立 / 求解 / 给出方案 / 开始）。
_EXECUTION_MARKERS = (
    "建立模型", "建立数学模型", "建立一个", "建一个", "建个", "建模求解", "构建模型", "构建一个",
    "搭建", "求解", "求出", "给出方案", "给出最优", "给出一个", "制定方案", "设计方案", "设计一个",
    "做一个", "做个", "做一下", "做这道", "做这个题", "解这道", "解决以下", "解决下列", "解决这",
    "完成这", "完成以下", "开始建模", "开始做", "开始吧", "开始求解", "启动建模", "开干", "动手",
    "跑一下", "跑一遍", "写论文", "写一篇", "问题一", "问题1", "问题 1",
    "build a model", "solve", "formulate",
)
#: 动词和宾语之间隔着对象的执行说法：「建立共享单车调度优化模型」「设计一套配送方案」。
_EXECUTION_PATTERN = re.compile(r"(建立|构建|搭建|设计|制定|建)[^，。,.;；？?！!]{0,24}(模型|方案|算法|评价体系)")

#: 整句只说「开始」：没有待确认提议时要看上文或附件里有没有题面。
_BARE_START = (
    "开始", "开始吧", "开始建模", "开始建模吧", "开始做", "开始做吧", "开始求解", "开始了",
    "可以开始了", "开干", "开工", "启动", "启动建模", "做吧", "来吧", "go", "start",
)

#: 寒暄 / 致谢 / 问身份：整句只由这些（加语气词与标点）组成才算。
_GREETING_PHRASES = tuple(sorted({
    "你好", "您好", "你好啊", "hi", "hello", "hey", "嗨", "哈喽", "哈啰", "在吗", "在不在",
    "早上好", "早安", "中午好", "下午好", "晚上好", "晚安", "谢谢", "谢谢你", "谢谢您", "多谢",
    "感谢", "感谢你", "谢了", "辛苦了", "再见", "拜拜", "bye", "你是谁", "你叫什么",
    "你是什么", "你能做什么", "你会什么", "介绍一下你自己", "介绍下你自己", "测试", "test",
    "ok", "好的", "收到", "嗯", "哦", "嗯嗯",
}, key=len, reverse=True))
_PARTICLE_CHARS = "啊呀呢吧哦噢嘛啦哈嗨~～!！。.,，、…?？:：;；)）(（ "

#: 指代上文：这句话要配合前面的对话才有意义。
_CONTINUATION_MARKERS = (
    "刚才", "刚刚", "上面", "前面", "之前", "上述", "上一", "这个数据", "那个数据", "这些数据",
    "这道题", "那道题", "这个题", "那个题", "这题", "按你说的", "按你的", "就按", "就用",
    "照这个", "接着", "继续", "那就", "同样的", "还是那个",
)
#: 修改措辞：在改上一轮提议或上文的题。
_MODIFY_MARKERS = (
    "改成", "改为", "换成", "换为", "改一下", "调整", "加上", "增加", "去掉", "删掉",
    "不要用", "别用", "替换", "目标改", "约束改",
)
#: 带附件时「题面在附件里」的说法。
_ATTACHMENT_REF_MARKERS = (
    "见附件", "附件里", "附件中", "附件是", "附件为", "在附件", "如附件", "看附件", "附件的题",
    "题目在", "题面在", "附件题目", "附件给出", "附件提供", "这道题", "这个题", "这题",
    "题目如下", "题目见",
)

#: 对上一轮提议的回应。
_START_WORDS = ("开始", "启动", "开干", "开工", "做吧", "来吧", "按这个", "就按", "就这么", "就这样", "照这个")
_CONFIRM_PREFIXES = (
    "确认", "确定", "好的", "好啊", "好呀", "好吧", "好嘞", "好滴", "好", "可以", "行", "嗯", "恩",
    "是的", "是啊", "对的", "对啊", "没问题", "同意", "ok", "okay", "yes", "当然",
)
_CONFIRM_EXACT = ("是", "对", "要", "可", "y", "go", "start")
#: 以确认词打头、其实另有所指的说法（「好像不太对」「可以用别的方法吗」）。
_CONFIRM_EXCEPTIONS = ("好像", "好多", "好久", "好难", "好慢", "可以用", "可以换", "可以改", "行不行")
_DENY_PREFIXES = (
    "不用", "不要", "先不", "暂时不", "暂不", "算了", "不了", "取消", "别", "不", "否", "no",
    "等等", "等一下", "再说", "以后再", "还不",
)
_DENY_EXCEPTIONS = ("不错", "不客气", "不用谢", "不用改", "不要改", "不过")
#: 「好的，那就开始吧」里「开始」之前的客套：剥掉后再看是不是光秃秃的「开始」
_START_PREFIX_FILLERS = ("那就", "那", "现在", "直接", "马上", "立即", "我们")
_NEGATIONS = ("不要", "不用", "先不", "暂不", "先别", "不", "别", "没", "未")
#: 判断确认句有没有附带新要求时要抹掉的客套与连接词
_CONFIRM_FILLERS = ("建模", "吧", "了", "那就", "直接", "现在", "马上", "立即", "我们", "帮我", "请", "谢谢")

#: 本地题型猜测（判定缺席时的 domain）：按顺序取首个命中的类别。
_DOMAIN_SIGNALS = (
    ("optimization", (
        "优化", "最优", "最小化", "最大化", "最短", "调度", "排产", "排班", "选址", "分配",
        "规划", "路径", "配送", "库存", "定价", "optimiz", "minimiz", "maximiz", "schedul",
        "allocat", "routing", "assignment", "inventory", "pricing",
    )),
    ("prediction", ("预测", "预报", "拟合", "回归", "趋势", "forecast", "predict", "regression")),
    ("evaluation", ("评价", "评估", "指标体系", "排名", "打分", "权重", "evaluat")),
    ("simulation", ("仿真", "模拟", "元胞", "蒙特卡洛", "simulat")),
    ("statistics", ("检验", "显著", "相关性", "方差分析", "置信", "统计", "估计", "贝叶斯", "先验", "分布")),
    ("data_analysis", ("聚类", "分类", "数据分析", "特征", "画像", "可视化", "cluster", "classif")),
)
_DOMAIN_LABELS = {
    "optimization": "优化",
    "prediction": "预测",
    "evaluation": "评价",
    "simulation": "仿真",
    "data_analysis": "数据分析",
    "statistics": "统计",
}
_DOMAIN_ALIASES = {
    "优化": "optimization", "optimisation": "optimization", "opt": "optimization",
    "预测": "prediction", "forecast": "prediction", "forecasting": "prediction",
    "评价": "evaluation", "评估": "evaluation", "assessment": "evaluation",
    "仿真": "simulation", "模拟": "simulation",
    "数据分析": "data_analysis", "data analysis": "data_analysis", "data-analysis": "data_analysis",
    "analysis": "data_analysis", "统计": "statistics", "stats": "statistics", "statistical": "statistics",
    "无": "none", "非数模": "none", "其他": "none", "other": "none", "": "none",
}
_MISSING_LABELS = {"problem": "题目正文", "objective": "求解目标", "data": "数据"}

#: 判定模型的五个意图：比对外三值多出 knowledge / file_analysis，路由守卫据此分流。
_JUDGE_INTENTS = ("modeling_task", "needs_info", "knowledge", "file_analysis", "chat")
#: 弱模型常把意图写成同义词或省略后缀：语义明确时不因字面对不上白烧一次调用。
_INTENT_ALIASES = {
    "modeling": "modeling_task",
    "modelling_task": "modeling_task",
    "modeling-task": "modeling_task",
    "modeling task": "modeling_task",
    "task": "modeling_task",
    "建模": "modeling_task",
    "建模任务": "modeling_task",
    "need_info": "needs_info",
    "needs-info": "needs_info",
    "needs info": "needs_info",
    "needsinfo": "needs_info",
    "缺少信息": "needs_info",
    "补充信息": "needs_info",
    "question": "knowledge",
    "qa": "knowledge",
    "知识": "knowledge",
    "问答": "knowledge",
    "咨询": "knowledge",
    "file": "file_analysis",
    "file-analysis": "file_analysis",
    "file analysis": "file_analysis",
    "analyze_file": "file_analysis",
    "文件": "file_analysis",
    "文件分析": "file_analysis",
    "分析文件": "file_analysis",
    "闲聊": "chat",
    "对话": "chat",
}

#: 判定回复里的候选 JSON 片段（不含嵌套）：弱模型爱在 JSON 前后写解释、套 Markdown 围栏，
#: 逐个候选试解析比取最外层「{ … }」跨度稳——后者会被解释文字里的花括号带偏。
_JSON_OBJECT = re.compile(r"\{[^{}]*\}", re.S)

# ── 回应模板（确认页状态行、未配置接口时的首页回应） ─────────────────────────

_GREETING_REPLY = (
    "你好！我是 OpenMathModel 的数学建模智能体：可以聊建模问题、帮你看文件；"
    "把完整赛题（正文与数据附件）发给我，确认后即可启动从读题到论文的完整建模流程。"
)
_KNOWLEDGE_REPLY = "这是建模知识类问题，我直接在对话里回答，不会创建任务；需要正式建模时把完整赛题发给我即可。"
_FILE_REPLY = "我先在对话里帮你看这份文件，不会创建任务；如果它就是要解决的赛题，回复「开始建模」即可启动。"
_DENY_REPLY = "好的，先不启动建模。想调整题目或换个问题，直接告诉我就行。"
_CLARIFY_PROBLEM_REPLY = "请提供完整的赛题正文（可附题目文档与数据文件），我才能为你启动建模任务。"
_CLARIFY_OBJECTIVE_REPLY = "还差求解目标：请说明要优化、预测还是评价什么（以及有哪些约束），我再为你启动建模任务。"
#: 未配置接口时首页没有对话模型：回应里说清对话要先配接口，题面照旧能进演示链路。
_UNCONFIGURED_NOTE = (
    "当前还没有配置模型接口，对话问答需要先在设置中心「自定义 API」添加接口；"
    "直接发送完整赛题会进入演示建模流程。"
)

# ── 数据结构 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IntakeAttachment:
    """判定可见的附件证据：文件名与浏览器解析出的正文摘录（可为空）。"""

    name: str
    excerpt: str = ""
    characters: int = 0


@dataclass(frozen=True)
class IntakeTurn:
    """首页对话里的一轮原话：``role`` 为 user / assistant，不含前端注入的任务 / 附件块。"""

    role: str
    text: str


@dataclass(frozen=True)
class PendingTask:
    """上一轮提议、等待用户确认的建模任务（题面 + 题型）。"""

    goal: str
    domain: str = ""


@dataclass(frozen=True)
class Understanding:
    """一句话的结构化理解：意图、言语行为、是否要求执行、是否承接上文、题型、缺什么、把握。"""

    kind: str = "chat"
    speech_act: str = "ask"
    execution: bool = False
    continuation: str = "new"
    domain: str = "none"
    missing: tuple[str, ...] = ()
    confidence: float = 0.0

    @property
    def requires_modeling(self) -> bool:
        return self.kind == "modeling_task"


@dataclass(frozen=True)
class IntakeDecision:
    route: str  # "start" | "propose" | "clarify" | "reply"
    understanding: Understanding = field(default_factory=Understanding)
    #: 模板回应：确认页状态行用；未配置接口时首页以它代替对话模型的回复。
    reply: str = ""
    #: 判定来源："heuristic"（本地规则）/ "judge"（模型判定）/ "fallback"（未配置或判定失败）。
    source: str = "fallback"
    #: start / propose 时的任务题面：确认提议时取提议记下的那份，指代上文时由上文拼出。
    task_goal: str = ""
    #: start 时的首页对话摘录：随任务进问题分析节点。
    context_excerpt: str = ""
    #: 被本地规则改写掉的模型原始意图，仅供日志回溯，不出接口。
    overridden_from: str = ""

    @property
    def intent(self) -> str:
        return _ROUTE_INTENTS[self.route]


@dataclass(frozen=True)
class _Signals:
    """本地从这句话里读出的信号：全部是确定性的词表 / 长度判断。"""

    text: str
    problem_marker: bool
    task_signal: bool
    inquiry: bool
    knowledge: bool
    request: bool
    execution: bool
    greeting: bool
    bare_start: bool
    continuation: bool
    modification: bool
    attachment_ref: bool
    domain: str

    @property
    def long(self) -> bool:
        return len(self.text) >= _LONG_GOAL_CHARS

    @property
    def statement(self) -> bool:
        """题面形态：粘贴的长文，或带任务型信号的陈述句。"""
        return self.long or (self.task_signal and not self.inquiry)

    @property
    def asking(self) -> bool:
        """明显在问（知识 / 方法 / 规则 / 文件内容），且没有要系统动手。"""
        return (self.knowledge or self.inquiry) and not self.execution

    @property
    def about_modeling(self) -> bool:
        """问的是建模相关的事（问句只有这样才算知识问答，「今天天气怎么样」是闲聊）。"""
        return self.knowledge or self.task_signal or self.problem_marker or self.domain != "none"


@dataclass(frozen=True)
class _Context:
    text: str
    signals: _Signals
    has_attachments: bool
    attachments: tuple[IntakeAttachment, ...]
    history: tuple[IntakeTurn, ...]
    pending: Optional[PendingTask]
    configured: bool
    confirmed: bool


@dataclass(frozen=True)
class _Verdict:
    """判定模型的结构化输出；字段缺省为 None 表示模型没给，由本地信号补位。"""

    intent: str
    execution: Optional[bool]
    continuation: str
    domain: str
    missing: Optional[tuple[str, ...]]
    confidence: Optional[float]


# ── 本地信号 ─────────────────────────────────────────────────────────────────


def _compact(text: str) -> str:
    return re.sub(r"[\s\u3000]+", "", text.strip().lower())


def _without_compounds(lowered: str) -> str:
    for compound in _INQUIRY_COMPOUND_EXCEPTIONS:
        lowered = lowered.replace(compound, " ")
    return lowered


def _contains_marker(lowered: str, marker: str) -> bool:
    """赛题标识匹配。纯字母标识（mcm / icm）要求两侧不是字母：否则「MCMC」
    （马尔可夫链蒙特卡洛）会被当成美赛缩写。数字与符号不算边界——「2024mcm」
    「mcm/icm」照常命中。中文标识按子串匹配。"""
    if not marker.isascii() or not marker.isalpha():
        return marker in lowered
    start = 0
    while True:
        index = lowered.find(marker, start)
        if index == -1:
            return False
        before = lowered[index - 1] if index > 0 else ""
        after = lowered[index + len(marker)] if index + len(marker) < len(lowered) else ""
        if not (before.isascii() and before.isalpha()) and not (
            after.isascii() and after.isalpha()
        ):
            return True
        start = index + 1


def _is_inquiry(text: str) -> bool:
    asking = _without_compounds(text.lower())
    if len(text) < _SHORT_TEXT_CHARS:
        return any(marker in asking for marker in _INQUIRY_MARKERS)
    compact = _compact(asking).rstrip("。.!！~～…")
    return compact.startswith(_QUESTION_OPENERS) or compact.endswith(_QUESTION_ENDINGS)


def _is_greeting(text: str) -> bool:
    remaining = _compact(text).strip(_PARTICLE_CHARS)
    if not remaining:
        # 纯标点 / 语气词（「？？」「哈哈」）：没有内容可判，按寒暄回应
        return bool(text.strip())
    while remaining:
        for phrase in _GREETING_PHRASES:
            if remaining.startswith(phrase):
                remaining = remaining[len(phrase) :].lstrip(_PARTICLE_CHARS)
                break
        else:
            return False
    return True


def _is_bare_start(text: str) -> bool:
    rest = _compact(text).strip(_PARTICLE_CHARS)
    prefixes = sorted(_CONFIRM_PREFIXES + _START_PREFIX_FILLERS, key=len, reverse=True)
    stripped = True
    while rest and stripped:
        stripped = False
        for prefix in prefixes:
            if rest.startswith(prefix) and rest != prefix:
                rest = rest[len(prefix) :].strip(_PARTICLE_CHARS)
                stripped = True
                break
    return rest in _BARE_START


def _guess_domain(lowered: str) -> str:
    for domain, words in _DOMAIN_SIGNALS:
        if any(word in lowered for word in words):
            return domain
    return "none"


def _signals(text: str, has_attachments: bool, confirmed: bool = False) -> _Signals:
    lowered = text.lower()
    compact = _compact(text)
    inquiry = _is_inquiry(text)
    knowledge = any(marker in _without_compounds(lowered) for marker in _KNOWLEDGE_MARKERS)
    request = compact.startswith("请") or any(marker in compact for marker in _REQUEST_MARKERS)
    execution_words = any(marker in lowered for marker in _EXECUTION_MARKERS) or bool(
        _EXECUTION_PATTERN.search(lowered)
    )
    long = len(text) >= _LONG_GOAL_CHARS
    # 问句里的「求解」「建立模型」是在问方法（「怎么求解这个模型」）；带请求措辞的问句是
    # 客气的命令，长文里的问句措辞多半是题面本身——这两种执行措辞照算
    execution = confirmed or (execution_words and (request or long or not inquiry))
    return _Signals(
        text=text,
        problem_marker=any(_contains_marker(lowered, marker) for marker in _PROBLEM_MARKERS),
        task_signal=len(text) >= _TASK_SIGNAL_MIN_CHARS and any(word in lowered for word in _TASK_SIGNALS),
        inquiry=inquiry,
        knowledge=knowledge,
        request=request,
        execution=execution,
        greeting=_is_greeting(text),
        bare_start=_is_bare_start(text),
        continuation=any(marker in compact for marker in _CONTINUATION_MARKERS),
        modification=any(marker in compact for marker in _MODIFY_MARKERS),
        attachment_ref=has_attachments and any(marker in compact for marker in _ATTACHMENT_REF_MARKERS),
        domain=_guess_domain(lowered),
    )


def _modeling_signal(goal: str) -> str:
    """日志与复现脚本用的粗分：``"problem"``（赛题标识）/ ``"task"``（任务型陈述）/ ``""``。"""
    lowered = goal.lower()
    if any(_contains_marker(lowered, marker) for marker in _PROBLEM_MARKERS):
        return "problem"
    if _is_inquiry(goal):
        return ""
    if len(goal) >= _TASK_SIGNAL_MIN_CHARS and any(word in lowered for word in _TASK_SIGNALS):
        return "task"
    return ""


def _occurs_unnegated(compact: str, word: str) -> Optional[bool]:
    """该词在句中出现时，是否至少有一次前面不带否定词；没出现返回 None。"""
    positions = [match.start() for match in re.finditer(re.escape(word), compact)]
    if not positions:
        return None
    return any(
        not any(compact[max(0, start - 3) : start].endswith(negation) for negation in _NEGATIONS)
        for start in positions
    )


def _proposal_answer(text: str) -> str:
    """对上一轮提议的回应：``"confirm"`` / ``"deny"`` / ``""``（不是在回答这道是非题）。"""
    compact = _compact(text).strip(_PARTICLE_CHARS)
    if not compact:
        return ""
    if compact in _CONFIRM_EXACT:
        return "confirm"
    starts_confirm = compact.startswith(_CONFIRM_PREFIXES) and not compact.startswith(_CONFIRM_EXCEPTIONS)
    if len(compact) > _PROPOSAL_ANSWER_MAX_CHARS and not starts_confirm:
        return ""
    hits = [hit for hit in (_occurs_unnegated(compact, word) for word in _START_WORDS) if hit is not None]
    if any(hits):
        return "confirm"
    if hits:
        # 只出现了被否定的「开始」（「好，但先别开始」）：明确不启动
        return "deny"
    if compact.startswith(_DENY_PREFIXES) and not compact.startswith(_DENY_EXCEPTIONS):
        return "deny"
    if _compact(text).rstrip("。.!！~～…").endswith(_QUESTION_ENDINGS):
        # 「可以吗？」「要多久？」是在反问，交判定看上下文
        return ""
    return "confirm" if starts_confirm else ""


def _adds_content(text: str) -> bool:
    """确认句里有没有附带新要求（「可以，不过目标改成成本最小」）。"""
    rest = _compact(text)
    for word in sorted(_CONFIRM_PREFIXES + _START_WORDS + _CONFIRM_FILLERS, key=len, reverse=True):
        rest = rest.replace(word, "")
    return len(re.sub(r"[\W_]+", "", rest)) >= 4


# ── 题面与上下文 ─────────────────────────────────────────────────────────────


def _cap(text: str, limit: int = TASK_GOAL_MAX_CHARS) -> str:
    return text.strip()[:limit]


def _summary(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else f"{flat[:limit]}…"


def _merge_goal(base: str, extra: str) -> str:
    base, extra = base.strip(), extra.strip()
    if not extra or extra in base:
        return _cap(base)
    if not base:
        return _cap(extra)
    return _cap(f"{base}\n\n补充要求：{extra}")


def _history_statement(history: Sequence[IntakeTurn]) -> str:
    """从上文里找题面：最近几条实质性的用户发言（跳过寒暄、知识问句与「好的 / 开始」）。"""
    picked: list[str] = []
    total = 0
    for turn in reversed(history):
        if turn.role != "user":
            continue
        text = turn.text.strip()
        signals = _signals(text, False)
        if signals.greeting or signals.bare_start or (signals.asking and not signals.long):
            continue
        if _proposal_answer(text) and not _adds_content(text):
            continue
        if not (signals.statement or signals.problem_marker or len(text) >= 30):
            continue
        picked.append(text)
        total += len(text)
        if len(picked) >= _HISTORY_STATEMENT_TURNS or total >= _HISTORY_STATEMENT_CHARS:
            break
    return "\n".join(reversed(picked))


def _attachment_goal(ctx: _Context) -> str:
    names = [item.name for item in ctx.attachments if item.name][:5]
    listing = f"（附件：{'、'.join(names)}）" if names else ""
    return f"按附件与引用资料中的赛题完成建模{listing}"


def _compose_goal(ctx: _Context, continuation: str) -> str:
    """start / propose 的题面：新话题就是这句话本身；承接上文时以提议题面或上文题面为底，
    这句话带了新内容才作为补充要求接在后面（「开始吧」不进题面）。"""
    if continuation != "continue":
        return _cap(ctx.text)
    extra = ctx.text if _adds_content(ctx.text) else ""
    if ctx.pending is not None:
        return _merge_goal(ctx.pending.goal, extra)
    base = _history_statement(ctx.history)
    if not base:
        return _cap(ctx.text)
    return _merge_goal(base, extra)


def _context_excerpt(history: Sequence[IntakeTurn]) -> str:
    lines: list[str] = []
    for turn in history[-_CONTEXT_TURNS:]:
        user = turn.role == "user"
        text = " ".join(turn.text.split())[: _CONTEXT_USER_CHARS if user else _CONTEXT_REPLY_CHARS]
        if text:
            lines.append(f"{'用户' if user else '助手'}：{text}")
    return "\n".join(lines)[:CONTEXT_EXCERPT_MAX_CHARS]


def _local_missing(ctx: _Context, continuation: str) -> tuple[str, ...]:
    if ctx.has_attachments or continuation == "continue":
        return ()
    signals = ctx.signals
    if signals.task_signal or signals.problem_marker or signals.statement:
        return ()
    return ("problem",)


# ── 决定构造 ─────────────────────────────────────────────────────────────────


def _speech_act(ctx: _Context, kind: str, execution: Optional[bool] = None) -> str:
    signals = ctx.signals
    if signals.greeting:
        return "greet"
    if kind != "modeling_task" and signals.asking:
        return "ask"
    if signals.modification and (ctx.pending is not None or ctx.history):
        return "modify"
    if signals.execution if execution is None else execution:
        return "command"
    if signals.asking:
        return "ask"
    return "supplement"


def _reply_text(ctx: _Context, kind: str, speech_act: str) -> str:
    if speech_act == "cancel":
        return _DENY_REPLY
    if not ctx.configured:
        if kind == "knowledge":
            return f"这是建模知识类问题，需要模型接口才能在对话里回答。{_UNCONFIGURED_NOTE}"
        if kind == "file_analysis":
            return (
                "要在对话里解读这份文件，需要先在设置中心「自定义 API」添加模型接口；"
                "如果它就是要解决的赛题，回复「开始建模」即可进入演示建模流程。"
            )
        return f"你好！我是 OpenMathModel 的数学建模智能体。{_UNCONFIGURED_NOTE}"
    if kind == "knowledge":
        return _KNOWLEDGE_REPLY
    if kind == "file_analysis":
        return _FILE_REPLY
    return _GREETING_REPLY


def _propose_reply(goal: str, domain: str) -> str:
    label = _DOMAIN_LABELS.get(domain, "")
    return (
        f"看起来这是一道{label}建模题：「{_summary(goal, 60)}」。确认后我会创建项目并启动六阶段建模"
        "（读题 → 数据 → 方案 → 实验 → 检验 → 论文）；回复「开始」即可，也可以先补充题面或数据。"
    )


def _start(
    ctx: _Context,
    *,
    goal: str,
    source: str,
    speech_act: str = "",
    execution: Optional[bool] = None,
    continuation: str = "new",
    domain: str = "",
    confidence: float = 0.9,
    overridden_from: str = "",
) -> IntakeDecision:
    act = speech_act or _speech_act(ctx, "modeling_task", execution)
    return IntakeDecision(
        route="start",
        understanding=Understanding(
            kind="modeling_task",
            speech_act=act,
            execution=(ctx.signals.execution if execution is None else execution) or act in ("confirm", "command"),
            continuation=continuation,
            domain=domain or ctx.signals.domain,
            confidence=confidence,
        ),
        source=source,
        task_goal=_cap(goal) or _cap(ctx.text),
        context_excerpt=_context_excerpt(ctx.history),
        overridden_from=overridden_from,
    )


def _propose(
    ctx: _Context,
    *,
    goal: str,
    source: str,
    execution: Optional[bool] = None,
    continuation: str = "new",
    domain: str = "",
    missing: tuple[str, ...] = (),
    confidence: float = 0.5,
    overridden_from: str = "",
) -> IntakeDecision:
    domain = domain or ctx.signals.domain
    goal = _cap(goal) or _cap(ctx.text)
    return IntakeDecision(
        route="propose",
        understanding=Understanding(
            kind="modeling_task",
            speech_act=_speech_act(ctx, "modeling_task", execution),
            execution=ctx.signals.execution if execution is None else execution,
            continuation=continuation,
            domain=domain,
            missing=missing,
            confidence=confidence,
        ),
        reply=_propose_reply(goal, domain),
        source=source,
        task_goal=goal,
        overridden_from=overridden_from,
    )


def _clarify(
    ctx: _Context,
    *,
    source: str,
    missing: tuple[str, ...] = ("problem",),
    execution: Optional[bool] = None,
    continuation: str = "new",
    domain: str = "",
    confidence: float = 0.8,
) -> IntakeDecision:
    missing = tuple(item for item in missing if item in MISSING_FIELDS) or ("problem",)
    reply = _CLARIFY_OBJECTIVE_REPLY if "problem" not in missing and "objective" in missing else _CLARIFY_PROBLEM_REPLY
    return IntakeDecision(
        route="clarify",
        understanding=Understanding(
            kind="modeling_task",
            speech_act=_speech_act(ctx, "modeling_task", execution),
            execution=ctx.signals.execution if execution is None else execution,
            continuation=continuation,
            domain=domain or ctx.signals.domain,
            missing=missing,
            confidence=confidence,
        ),
        reply=reply,
        source=source,
    )


def _reply(
    ctx: _Context,
    *,
    kind: str,
    source: str,
    speech_act: str = "",
    continuation: str = "new",
    domain: str = "",
    confidence: float = 0.9,
) -> IntakeDecision:
    act = speech_act or _speech_act(ctx, kind)
    return IntakeDecision(
        route="reply",
        understanding=Understanding(
            kind=kind,
            speech_act=act,
            execution=False,
            continuation=continuation,
            domain=domain or (ctx.signals.domain if kind != "chat" else "none"),
            confidence=confidence,
        ),
        reply=_reply_text(ctx, kind, act),
        source=source,
    )


# ── 判定模型 ─────────────────────────────────────────────────────────────────


def _judge_prompt(ctx: _Context, evidence: Sequence[IntakeAttachment]) -> str:
    parts = [
        "你是数学建模工作台首页的接待员，只负责理解用户这一句话，不负责回答。"
        "首页还没有创建建模任务；只有用户确实要系统解决一道具体的建模题时才会启动建模"
        "（创建项目并运行读题到论文六个阶段，耗时长、花费大）。",
        '只输出一行 JSON，不要任何解释，形如：{"intent": "modeling_task", "execution": false, '
        '"continuation": "new", "domain": "optimization", "missing": [], "confidence": 0.8}',
        "intent 五选一：",
        "- modeling_task：要系统解决一道具体的建模题——看得出要解决的对象和求解 / 优化 / 预测 / 评价目标，"
        "哪怕只有一句话、没有数据；题面也可能在附件或上文里。",
        "- needs_info：想让系统建模，但对象和目标都没说（「帮我做个建模」「解决这道题」却看不到题目）。",
        "- knowledge：问建模知识、方法选择、竞赛规则、论文写法、概念含义，直接回答即可，不需要真的建模求解"
        "（「美赛和国赛有什么区别」「共享单车调度一般用什么模型」）。",
        "- file_analysis：针对上传的文件提问或让系统看看文件（「这张图是什么意思」「这个数据有什么特点」），"
        "没有要求据此建模。",
        "- chat：寒暄、闲聊、感谢、情绪、与建模无关的话。",
        "execution：用户是否明确要求系统现在就开始做（「请建立模型求解」「帮我做」「开始吧」= true；"
        "只描述题目、提问、犹豫、讨论 = false）。",
        "continuation：这句话是否承接上文的题目或提议（「就用刚才的数据」「目标改成成本最小」"
        "「那就这么做」= continue；开启新话题 = new）。",
        "domain：optimization / prediction / evaluation / simulation / data_analysis / statistics / none 之一。",
        'missing：要启动建模还缺什么，从 "problem"（看不出要解决什么问题）、"objective"（没说要优化 / 预测 / '
        '评价什么）、"data"（题目离不开数据却没给）中选，可为空数组；题面在附件或上文里就不算缺。',
        "confidence：你对 intent 判断的把握，0 到 1。",
        "上传文件、@ 引用资料只是证据，不等于要建模：要看用户的话本身是在问、在聊，还是要系统动手。",
    ]
    if ctx.pending is not None:
        parts.append(
            f"\n上一轮系统提议过启动建模、正在等用户确认，提议的题面：{_summary(ctx.pending.goal, _JUDGE_PENDING_CHARS)}"
        )
    if ctx.history:
        parts.append("\n最近对话（旧 → 新）：")
        for turn in ctx.history[-_JUDGE_HISTORY_TURNS:]:
            role = "用户" if turn.role == "user" else "助手"
            parts.append(f"  {role}：{_summary(turn.text, _JUDGE_HISTORY_CHARS)}")
    if evidence:
        parts.append("\n用户上传了附件，正文摘录如下（附件内容与用户的话一起看，但上传本身不代表要建模）：")
        for attachment in evidence[:_ATTACHMENT_PROMPT_LIMIT]:
            parts.append(
                f"附件「{attachment.name}」内容摘录：\n{attachment.excerpt[:_ATTACHMENT_EXCERPT_CHARS]}"
            )
    elif ctx.has_attachments:
        parts.append("\n用户附带了文件或 @ 引用资料（内容没有解析出文字，看不到正文）。")
    parts.append(f"\n用户这一句：\n{ctx.text[:_GOAL_EXCERPT_CHARS]}")
    return "\n".join(parts)


def _normalize_intent(raw: object) -> Optional[str]:
    value = str(raw or "").strip().strip("\"'").lower()
    if value in _JUDGE_INTENTS:
        return value
    return _INTENT_ALIASES.get(value)


def _normalize_domain(raw: object) -> str:
    value = str(raw or "").strip().strip("\"'").lower()
    if value in DOMAINS:
        return value
    return _DOMAIN_ALIASES.get(value, "none")


def _as_bool(raw: object) -> Optional[bool]:
    if isinstance(raw, bool):
        return raw
    value = str(raw if raw is not None else "").strip().lower()
    if value in ("true", "yes", "1", "是", "y"):
        return True
    if value in ("false", "no", "0", "否", "n"):
        return False
    return None


def _as_confidence(raw: object) -> Optional[float]:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        value = raw.strip().lower().rstrip("%")
        named = {"high": 0.85, "高": 0.85, "medium": 0.6, "中": 0.6, "low": 0.3, "低": 0.3}
        if value in named:
            return named[value]
        try:
            number = float(value)
        except ValueError:
            return None
    elif isinstance(raw, (int, float)):
        number = float(raw)
    else:
        return None
    if 1 < number <= 100:
        number /= 100
    return min(1.0, max(0.0, number))


def _as_missing(raw: object) -> Optional[tuple[str, ...]]:
    if raw is None:
        return None
    items = raw if isinstance(raw, list) else re.split(r"[,，、\s]+", str(raw))
    return tuple(dict.fromkeys(str(item).strip().lower() for item in items if str(item).strip().lower() in MISSING_FIELDS))


def _verdict_from_object(data: object) -> Optional[_Verdict]:
    if not isinstance(data, dict):
        return None
    intent = _normalize_intent(data.get("intent"))
    if intent is None:
        return None
    continuation = str(data.get("continuation") or "").strip().lower()
    return _Verdict(
        intent=intent,
        execution=_as_bool(data.get("execution")),
        continuation="continue" if continuation in ("continue", "继续", "承接", "true", "yes") else "new",
        domain=_normalize_domain(data.get("domain")) if data.get("domain") is not None else "",
        missing=_as_missing(data.get("missing")),
        confidence=_as_confidence(data.get("confidence")),
    )


def _parse_judge_reply(text: str) -> Optional[_Verdict]:
    """从判定回复里取出结构化理解；解析不出返回 None，交调用方走判定缺席的兜底。

    先按「最外层跨度」解析（模型规矩输出一行 JSON 的常态路径），失败再逐个扫描不含嵌套
    的候选片段——模型在 JSON 前后写解释、套围栏或连发多个对象时靠它兜住。
    """
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = _verdict_from_object(json.loads(text[start : end + 1]))
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            return parsed
    for match in _JSON_OBJECT.finditer(text):
        try:
            parsed = _verdict_from_object(json.loads(match.group(0)))
        except json.JSONDecodeError:
            continue
        if parsed is not None:
            return parsed
    return None


def _judge(
    config: LlmConfig,
    ctx: _Context,
    evidence: Sequence[IntakeAttachment],
    on_usage: Callable[[ChatOutcome], None] | None,
) -> Optional[_Verdict]:
    candidates = [
        endpoint
        for endpoint in config.endpoints
        if config.allow_proxy or not is_third_party_host(endpoint.host)
    ]
    if not candidates:
        return None
    judge = min(candidates, key=endpoint_strength)
    try:
        outcome = complete_once(
            judge,
            [{"role": "user", "content": _judge_prompt(ctx, evidence)}],
            max_tokens=INTAKE_MAX_TOKENS,
            read_timeout=INTAKE_READ_TIMEOUT_S,
        )
    except Exception as error:  # noqa: BLE001 - 判定挂了走本地兜底，绝不让接待报错
        logger.warning("task intake judge failed on %s: %s", judge.name, error)
        return None
    if on_usage is not None:
        try:
            on_usage(outcome)
        except Exception:  # noqa: BLE001 - 用量记账绝不允许影响接待
            logger.exception("task intake usage callback failed")
    return _parse_judge_reply(outcome.text)


# ── 路由守卫 ─────────────────────────────────────────────────────────────────


def _decide_unconfigured(ctx: _Context) -> IntakeDecision:
    """未配置接口：本地规则拦寒暄 / 明显问句 / 只传文件；像题面的照旧进演示链路。"""
    signals = ctx.signals
    if signals.bare_start:
        return _bare_start(ctx, source="fallback")
    if signals.asking:
        return _reply(ctx, kind=_asking_kind(ctx), speech_act="ask", source="heuristic")
    if ctx.has_attachments and not (
        signals.execution or signals.statement or signals.attachment_ref or signals.problem_marker
    ):
        return _reply(ctx, kind="file_analysis", source="heuristic")
    if ctx.pending is not None and (signals.modification or signals.continuation):
        # 还在改上一轮的提议，没说开始：带着改动再提议一次
        return _propose(
            ctx, goal=_merge_goal(ctx.pending.goal, ctx.text), continuation="continue", source="heuristic"
        )
    continuation = "continue" if signals.continuation and ctx.history else "new"
    return _start(ctx, goal=_compose_goal(ctx, continuation), continuation=continuation, source="fallback")


def _asking_kind(ctx: _Context) -> str:
    if ctx.has_attachments:
        return "file_analysis"
    return "knowledge" if ctx.signals.about_modeling else "chat"


def _bare_start(ctx: _Context, *, source: str) -> IntakeDecision:
    """没有待确认提议时的一句「开始」：上文有题面就按上文启动，带着附件就按附件启动，否则问题面。"""
    base = _history_statement(ctx.history)
    if base:
        return _start(ctx, goal=base, speech_act="command", continuation="continue", source=source)
    if ctx.has_attachments:
        return _start(ctx, goal=_attachment_goal(ctx), speech_act="command", source=source)
    return _clarify(ctx, execution=True, source=source)


def _decide_locally(ctx: _Context, attachments_visible: bool) -> Optional[IntakeDecision]:
    """已配置接口时的本地强证据：能确定的不出网；拿不准返回 None 交判定。"""
    signals = ctx.signals
    if signals.bare_start:
        return None
    if signals.long and signals.execution and not signals.knowledge:
        # 粘贴的赛题（题面 + 「请建立模型 / 问题一」）：一步直达，不烧判定调用
        return _start(ctx, goal=ctx.text, source="heuristic")
    if signals.knowledge and not signals.execution and not signals.long and not ctx.has_attachments and ctx.pending is None:
        return _reply(ctx, kind="knowledge", speech_act="ask", source="heuristic")
    if ctx.has_attachments and not attachments_visible and not signals.long:
        # 附件内容不可见（纯图片 / 扫描件 / 关闭解析 / @ 引用赛题）：判定也看不到正文，按话语定
        material = signals.attachment_ref or signals.task_signal or signals.problem_marker
        if signals.execution and material:
            return _start(ctx, goal=ctx.text, source="heuristic")
        if signals.asking:
            return _reply(ctx, kind="file_analysis", speech_act="ask", source="heuristic")
        if material:
            return _propose(ctx, goal=ctx.text, source="heuristic")
        if not signals.execution:
            return _reply(ctx, kind="file_analysis", source="heuristic")
    return None


def _decide_without_judge(ctx: _Context) -> IntakeDecision:
    """判定失败 / 输出无法解析：只有本地强证据才启动，像题面的先提议，其余直接回答。"""
    signals = ctx.signals
    if signals.bare_start:
        return _bare_start(ctx, source="fallback")
    continuation = (
        "continue" if (signals.continuation or signals.modification) and (ctx.history or ctx.pending) else "new"
    )
    if signals.asking and not signals.long:
        return _reply(ctx, kind=_asking_kind(ctx), speech_act="ask", source="fallback", confidence=0.5)
    material = (
        signals.task_signal
        or signals.statement
        or signals.problem_marker
        or ctx.has_attachments
        or continuation == "continue"
    )
    goal = _compose_goal(ctx, continuation)
    if signals.execution and material:
        return _start(ctx, goal=goal, continuation=continuation, source="fallback", confidence=0.5)
    if signals.statement or signals.task_signal or signals.problem_marker or signals.attachment_ref or continuation == "continue":
        return _propose(ctx, goal=goal, continuation=continuation, source="fallback")
    if signals.execution:
        # 要系统动手却看不出做什么（「帮我做一个数学建模的作业」）：问题面
        return _clarify(ctx, source="fallback", confidence=0.5)
    kind = "file_analysis" if ctx.has_attachments else "chat"
    return _reply(ctx, kind=kind, source="fallback", confidence=0.5)


def _route_verdict(ctx: _Context, verdict: _Verdict) -> IntakeDecision:
    """判定结论 → 路由（规则定，不交模型）。"""
    signals = ctx.signals
    execution = signals.execution if verdict.execution is None else verdict.execution
    if ctx.confirmed or signals.bare_start:
        execution = True
    continuation = verdict.continuation if (ctx.history or ctx.pending is not None) else "new"
    confidence = _DEFAULT_CONFIDENCE if verdict.confidence is None else verdict.confidence
    domain = verdict.domain if verdict.domain and verdict.domain != "none" else signals.domain
    missing = verdict.missing if verdict.missing is not None else _local_missing(ctx, continuation)

    if verdict.intent in ("knowledge", "file_analysis", "chat"):
        return _reply(
            ctx,
            kind=verdict.intent,
            continuation=continuation,
            domain=domain if verdict.intent != "chat" else "none",
            confidence=confidence,
            source="judge",
        )
    if signals.bare_start and continuation == "new":
        # 一句「开始」、判定也说不承接上文：题面只可能在附件里，没有附件就问题面
        return _bare_start(ctx, source="judge")
    goal = _compose_goal(ctx, continuation)
    common = {"execution": execution, "continuation": continuation, "domain": domain, "confidence": confidence}
    if verdict.intent == "needs_info":
        if signals.task_signal and not signals.inquiry:
            # 本地已看到对象与求解目标，判定却说「信息不足」——描述简略不是拦人的理由：
            # 明确要做就启动，没说要做就先提议（v1 的否决语义，按 2026-10-02 的提议策略收窄）
            if execution:
                return _start(ctx, goal=goal, source="heuristic", overridden_from="needs_info", **common)
            return _propose(ctx, goal=goal, source="heuristic", overridden_from="needs_info", **common)
        return _clarify(ctx, missing=missing or ("problem",), source="judge", **common)
    if "problem" in missing and not ctx.has_attachments and continuation != "continue":
        return _clarify(ctx, missing=missing, source="judge", **common)
    if execution and confidence >= START_CONFIDENCE and "objective" not in missing:
        return _start(ctx, goal=goal, source="judge", **common)
    return _propose(ctx, goal=goal, missing=missing, source="judge", **common)


def _decide_intake(
    config: LlmConfig,
    goal: str,
    has_attachments: bool,
    attachments: Sequence[IntakeAttachment],
    on_usage: Callable[[ChatOutcome], None] | None,
    history: Sequence[IntakeTurn],
    pending: Optional[PendingTask],
    confirmed: bool,
) -> IntakeDecision:
    text = goal.strip()
    ctx = _Context(
        text=text,
        signals=_signals(text, has_attachments, confirmed),
        has_attachments=has_attachments,
        attachments=tuple(attachments),
        history=tuple(history),
        pending=pending,
        configured=config_usable(config),
        confirmed=confirmed,
    )
    signals = ctx.signals

    # ① 回应上一轮提议：系统自己问的是非题，本地直判、不出网
    if pending is not None:
        answer = _proposal_answer(text)
        if answer == "confirm":
            return _start(
                ctx,
                goal=_merge_goal(pending.goal, text if _adds_content(text) else ""),
                speech_act="confirm",
                continuation="continue",
                domain=_normalize_domain(pending.domain) if pending.domain else "",
                source="heuristic",
            )
        if answer == "deny":
            return _reply(ctx, kind="chat", speech_act="cancel", continuation="continue", source="heuristic")
        # 其它：提议作废、按新话判；承接上文的修改由判定 / 兜底以「提议题面 + 本句」再提议

    # ② 寒暄与极短输入：本地处理，不值得出网（未配置也一样）
    if not has_attachments and not signals.bare_start:
        if signals.greeting:
            return _reply(ctx, kind="chat", speech_act="greet", source="heuristic")
        if len(text) <= _TRIVIAL_GOAL_CHARS and not signals.task_signal and not signals.continuation:
            # 带赛题标识（「A题」「美赛」）或请求措辞（「帮我建模」）是想做题但没给题面 → 问题面；
            # 其余是寒暄，对着一句「在吗」索要赛题正文属答非所问
            if signals.problem_marker or signals.request or signals.execution:
                return _clarify(ctx, source="heuristic")
            return _reply(ctx, kind="chat", speech_act="greet", source="heuristic")

    # ③ 一句「开始」却没有上文也没有材料：题面无从谈起，不必出网
    if signals.bare_start and not history and not has_attachments:
        return _clarify(ctx, execution=True, source="heuristic")

    if not ctx.configured:
        return _decide_unconfigured(ctx)

    evidence = [item for item in attachments if item.excerpt.strip()]
    local = _decide_locally(ctx, attachments_visible=bool(evidence))
    if local is not None:
        return local
    verdict = _judge(config, ctx, evidence, on_usage)
    if verdict is None:
        return _decide_without_judge(ctx)
    return _route_verdict(ctx, verdict)


def decide_intake(
    config: LlmConfig,
    goal: str,
    has_attachments: bool,
    attachments: Sequence[IntakeAttachment] = (),
    on_usage: Callable[[ChatOutcome], None] | None = None,
    *,
    history: Sequence[IntakeTurn] = (),
    pending_task: Optional[PendingTask] = None,
    confirmed: bool = False,
) -> IntakeDecision:
    """判定一次发送该怎么走（start / propose / clarify / reply）。绝不抛异常。

    每次判定都留一行日志：门拦错人时用户只会说「进不去建模」或「随便说句话就建了任务」，
    没有 route / source / 理解字段就无从回溯是本地规则还是判定模型的结论。
    """
    turns = [turn for turn in history if turn.text.strip()][-_HISTORY_LIMIT:]
    pending = pending_task if pending_task is not None and pending_task.goal.strip() else None
    try:
        decision = _decide_intake(
            config, goal, has_attachments, attachments, on_usage, turns, pending, confirmed
        )
    except Exception:  # noqa: BLE001 - 接待自身出错时先提议：不挡人，也不替人建任务
        logger.exception("task intake failed; falling back to a proposal")
        text = goal.strip()
        decision = IntakeDecision(
            route="propose",
            understanding=Understanding(kind="modeling_task", speech_act="supplement"),
            reply=_propose_reply(text, "none"),
            source="fallback",
            task_goal=_cap(text),
        )
    understanding = decision.understanding
    logger.info(
        "task intake route=%s intent=%s source=%s kind=%s act=%s execution=%s continuation=%s "
        "domain=%s missing=%s confidence=%.2f overrode=%s goal_chars=%d attachments=%d history=%d pending=%s",
        decision.route,
        decision.intent,
        decision.source,
        understanding.kind,
        understanding.speech_act,
        understanding.execution,
        understanding.continuation,
        understanding.domain,
        ",".join(understanding.missing) or "none",
        understanding.confidence,
        decision.overridden_from or "none",
        len(goal.strip()),
        len(attachments),
        len(turns),
        pending is not None,
    )
    return decision


# ── 首页对话轮的接待状态块（ADR-0024） ───────────────────────────────────────

#: 首页对话的系统提示词：还没有任务，模型不得自称已经开始建模。
HOME_CHAT_SYSTEM_PROMPT = (
    "你是 OpenMathModel 的数学建模 Agent，正在首页与用户对话——这里还没有创建建模任务。"
    "你可以回答建模知识、帮用户解读文件、讨论题目与思路；只有系统按用户的确认启动建模任务后，"
    "才会创建项目并依次执行读题、数据准备、建模方案、实验、检验、论文六个阶段。"
    "不得声称已经开始建模、已经创建任务或正在运行。默认使用中文，数学公式使用 LaTeX 行内写法。"
)

_NO_ECHO = "这些要求是给你的内部约束，不是回复内容：不要提及、复述或解释它们。"


def home_prompt_block(
    route: str,
    *,
    kind: str = "chat",
    speech_act: str = "",
    domain: str = "",
    missing: Sequence[str] = (),
    task_goal: str = "",
) -> str:
    """首页对话轮注入系统提示词的【接待判定】块：模型的回复与实际路由保持一致。

    v1 把接待结论丢在前端（判 needs_info 时模型照样可能答「好的我来建模」，然后什么都
    不会发生）；这里把「这句话被判成了什么、接下来会不会建任务、该怎么回」交代给模型。
    """
    label = _DOMAIN_LABELS.get(domain, "")
    lines = ["【接待判定】"]
    if route == "propose":
        lines.append(f"- 结论：这像一道{label}建模题，系统已记下待确认的题面；用户确认后才会启动建模任务。")
        if task_goal.strip():
            lines.append(f"- 待确认的题面：{_summary(task_goal, 300)}")
        lines.append(
            "- 回复要求：先用两三句复述你对题目的理解（对象、目标、可能的方法方向），然后明确问用户是否"
            "开始建模，并告诉用户回复「开始」即可启动（会创建项目并运行六个阶段，耗时较长）；有明显缺失"
            "的信息顺带提醒补充。不要在对话里直接解题或给出完整模型。"
        )
    elif route == "clarify":
        need = "、".join(_MISSING_LABELS[item] for item in missing if item in _MISSING_LABELS) or "题目正文"
        lines.append(f"- 结论：用户像是想建模，但还缺{need}，暂不启动建模任务。")
        lines.append(
            "- 回复要求：用一两句话说清需要用户补充什么（题目正文 / 求解目标 / 数据文件），可以举个例子；"
            "不要说已经开始建模。"
        )
    elif speech_act == "cancel":
        lines.append("- 结论：用户没有接受上一轮的建模提议，不启动建模任务。")
        lines.append("- 回复要求：简短确认不启动，问问用户想调整题目还是换个问题。")
    elif kind == "knowledge":
        lines.append("- 结论：这是建模知识问答，不启动建模任务。")
        lines.append("- 回复要求：直接、准确地回答问题；不要说「我来为你建模」，也不要询问是否创建任务。")
    elif kind == "file_analysis":
        lines.append("- 结论：用户在问上传的文件，不启动建模任务。")
        lines.append(
            "- 回复要求：根据随消息附上的文件内容回答；看不到文件内容时直接说明看不到。若文件像是一道赛题，"
            "回答后可以提一句：回复「开始建模」即可据此启动建模任务。"
        )
    else:
        lines.append("- 结论：寒暄或闲聊，不启动建模任务。")
        lines.append("- 回复要求：简短友好地回应；可以顺带一句：把完整赛题（正文与数据附件）发给我，确认后即可开始建模。")
    lines.append(_NO_ECHO)
    return "\n".join(lines)


__all__ = [
    "CONTEXT_EXCERPT_MAX_CHARS",
    "DOMAINS",
    "HOME_CHAT_SYSTEM_PROMPT",
    "INTENTS",
    "KINDS",
    "MISSING_FIELDS",
    "ROUTES",
    "SPEECH_ACTS",
    "START_CONFIDENCE",
    "TASK_GOAL_MAX_CHARS",
    "IntakeAttachment",
    "IntakeDecision",
    "IntakeTurn",
    "PendingTask",
    "Understanding",
    "decide_intake",
    "home_prompt_block",
]

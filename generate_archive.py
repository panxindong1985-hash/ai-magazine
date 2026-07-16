#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI HOT 日报 → 每日文件 + 索引页（无限归档版，可每日定时运行）
- 拉取「全部可用」日报列表（dailies 列表，一次性返回所有历史日期）
- 用 archive.json 做增量清单：已生成的日期不再重抓，只追加新出现的一天
- 通过 items(mode=all, 最近7天窗口) 补全近 7 天条目的真实发布时间；更早的退化为仅显示日期
- 每天产出独立 HTML：ai-daily-YYYY-MM-DD.html（永不删除旧档）
- 产出索引页：index.html（卡片列表，互相跳转）
输出目录：脚本同目录
"""
import json, re, os, sys, ssl, time, datetime, threading, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as _FTimeout

# 运行模式：--render-only 仅用本地归档重渲染（跳过抓取/回填/翻译，最快出页面）；
#           --no-translate 跳过英文→中文翻译；--no-backfill 跳过头像/图片本地化回填
RENDER_ONLY = "--render-only" in sys.argv
NO_TRANSLATE = "--no-translate" in sys.argv
NO_BACKFILL = "--no-backfill" in sys.argv

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
BASE = "https://aihot.virxact.com/api/public"
# dailies 接口不支持翻页(offset/page 均被忽略)，take 上限约 120(超过即 400)
# 取 120 可覆盖未来约 4 个月；已抓取的日期由 archive.json 永久保留，不会因接口截断而丢失
DAILIES_TAKE = 120

SECTIONS = [
    ("模型发布/更新", "#4f46e5"),
    ("产品发布/更新", "#059669"),
    ("行业动态",     "#d97706"),
    ("论文研究",     "#e11d48"),
    ("技巧与观点",   "#0284c7"),
]

# 主要 AI 公司 → (别名关键词, 阵营)。阵营 region: "us"=美国 / "cn"=中国 / "eu"=欧洲公司（当前仅法国 Mistral，标题按其所属国家显示🇫🇷法国）；甘特图按阵营分块呈现，欧洲置底。
COMPANIES = [
    # ── 易在别家新闻中被提及、需优先识别的具体品牌（放前，降低被宽别名公司抢命中）──
    ("Apple",    "#555555", ["apple", "苹果", "afm", "apple foundation"], "us"),
    ("稀宇科技",  "#00bcd4", ["minimax", "minmax", "abab"], "cn"),
    ("Thinking Machines", "#6d28d9", ["thinking machines", "thinking machine"], "us"),
    ("深度求索", "#e11d48", ["deepseek", "深度求索"], "cn"),
    ("百度",     "#2932e1", ["百度", "文心", "ernie", "千帆"], "cn"),
    ("阿里",     "#ff6a00", ["阿里", "通义", "qwen", "千问", "高德"], "cn"),
    ("腾讯",     "#12b7f5", ["腾讯", "混元", "元宝", "hunyuan"], "cn"),
    ("字节",     "#fe2c55", ["字节", "豆包", "coze", "扣子"], "cn"),
    ("智谱",     "#0ea5e9", ["智谱", "chatglm", "zhipu", "glm"], "cn"),
    ("月之暗面", "#8b5cf6", ["月之暗面", "moonshot", "kimi"], "cn"),
    ("百川",     "#c2185b", ["百川", "baichuan"], "cn"),
    ("讯飞星火", "#0d9488", ["讯飞", "星火", "iflytek", "spark"], "cn"),
    ("Mistral",  "#ff7000", ["mistral"], "eu"),
    # ── 宽别名公司放后，减少误命中 ──
    ("OpenAI",  "#10a37f", ["openai", "chatgpt", "sora"], "us"),
    ("Anthropic","#d97706", ["anthropic", "claude"], "us"),
    ("Google",  "#4285f4", ["google", "deepmind", "gemini", "gemma"], "us"),
    ("Meta",    "#0866ff", ["meta", "llama", "llama 3", "llama3"], "us"),
    ("Microsoft","#7c3aed", ["microsoft", "微软", "copilot", "bing"], "us"),
    ("xAI",     "#111827", ["xai", "grok"], "us"),
    ("NVIDIA",  "#76b900", ["nvidia", "英伟达"], "us"),
    ("Amazon",   "#ff9900", ["amazon", "亚马逊", "bedrock", "nova", "titan"], "us"),
]
# 主要模型清单：时间线「按模型分行」用。匹配顺序自上而下；未命中则退化为按公司聚合。
# (模型名, 所属公司, [关键词小写])
MODELS = [
    ("GPT-5",     "OpenAI",    ["gpt-5", "gpt5", "gpt 5"]),
    ("GPT-4o",    "OpenAI",    ["gpt-4o", "gpt4o", "gpt-4"]),
    ("o3 / o4",   "OpenAI",    ["o3", "o4", "openai o"]),
    ("Sora",      "OpenAI",    ["sora"]),
    ("Bidi",      "OpenAI",    ["bidi"]),
    ("GPT-Live",  "OpenAI",    ["gpt-live", "gpt live", "gpt‑live"]),
    ("GPT-Realtime", "OpenAI", ["gpt-realtime", "实时翻译"]),
    ("Claude",    "Anthropic", ["claude"]),
    ("Gemini",    "Google",    ["gemini"]),
    ("Gemma",     "Google",    ["gemma"]),
    ("Veo",       "Google",    ["veo"]),
    ("Magenta",   "Google",    ["magenta", "mrt2"]),
    ("Llama",     "Meta",      ["llama"]),
    ("Copilot",   "Microsoft", ["copilot"]),
    ("Orca",      "Microsoft", ["orca"]),
    ("Phi",       "Microsoft", ["phi"]),
    ("Grok",      "xAI",       ["grok"]),
    ("DeepSeek",  "深度求索",  ["deepseek", "深度求索"]),
    ("文心 ERNIE","百度",       ["文心", "ernie", "千帆"]),
    ("通义千问",  "阿里",       ["通义", "qwen", "千问", "wan", "万相"]),
    ("HappyHorse","阿里",       ["happyhorse", "HappyHorse"]),
    ("ABot-Earth","阿里",       ["abot-earth", "abot"]),
    ("混元",      "腾讯",       ["混元", "hunyuan", "元宝"]),
    ("Hy-MT",     "腾讯",       ["hy-mt", "hy mt"]),
    ("豆包",      "字节",       ["豆包", "doubao"]),
    ("即梦",      "字节",       ["即梦", "jimeng"]),
    ("Seedance",  "字节",       ["seedance"]),   # 文生视频，单独成行 + 重大更新红色高亮
    ("Coze 扣子", "字节",       ["coze", "扣子"]),
    ("智谱 GLM",  "智谱",       ["智谱", "chatglm", "zhipu", "glm", "清影", "清言"]),
    ("Kimi",      "月之暗面",   ["kimi", "moonshot"]),
    # ── 新增公司（仅作 daily-feed 归类用，里程碑见 MILESTONES）──
    ("Mistral 系列", "Mistral", ["mistral"]),
    ("Nova",       "Amazon",   ["nova"]),
    ("Titan",      "Amazon",   ["titan"]),
    ("Apple 基础模型","Apple",  ["apple intelligence", "foundation model", "apple 基础", "apple foundation"]),
    ("Baichuan",   "百川",     ["baichuan", "百川"]),
    ("MiniMax 系列","稀宇科技",["abab", "minimax", "minmax", "m2.7", "m2.5"]),
    ("星火",       "讯飞星火",  ["星火", "spark", "讯飞"]),
    # ── NVIDIA ──
    ("Nemotron",   "NVIDIA",    ["nemotron"]),
    ("Cosmos",     "NVIDIA",    ["cosmos"]),
    ("SANA",       "NVIDIA",    ["sana"]),
    # ── 其它实验室基础 / 生成模型（daily-feed 归类用）──
    ("Seed",       "字节",       ["seed"]),
    ("Muse",       "Meta",      ["muse"]),
    ("MAI",        "Microsoft", ["mai-thinking", "mai "]),
]
COMP_MAP = {name: (color, region) for name, color, _, region in COMPANIES}

# 模型归族：把同一模型系列的不同大版本合并到「同一行」，时间线上沿年份线性呈现。
# 例：GPT-3 / GPT-4 / GPT-4o / GPT-5 同属 GPT 一行；Claude 1→4 同属 Claude 一行。
# 未在此表中的具体模型名原样成行；命中公司名（即未匹配到具体模型的兜底）直接剔除。
FAMILY = {
    # OpenAI
    "GPT-3": "GPT", "GPT-4": "GPT", "GPT-4o": "GPT", "GPT-4.5": "GPT",
    "GPT-4.1": "GPT", "gpt-oss": "GPT", "ChatGPT": "GPT", "GPT-5": "GPT",
    "GPT-5.1": "GPT", "GPT-5.2": "GPT", "GPT-5.4": "GPT", "GPT-5.5": "GPT", "GPT-5.6": "GPT",
    "o3 / o4": "OpenAI o 系列", "Sora": "Sora", "Sora 2": "Sora", "DALL·E 3": "DALL·E",
    "Bidi": "Bidi", "GPT-Live": "GPT-Live", "GPT-Realtime": "GPT-Realtime",
    # Anthropic
    "Claude": "Claude",
    # Google
    "Gemini": "Gemini", "Gemma": "Gemma", "Veo": "Veo", "Bard": "Gemini", "Magenta": "Magenta",
    # Meta
    "Llama": "Llama",
    # xAI
    "Grok": "Grok",
    # DeepSeek
    "DeepSeek": "DeepSeek 系列",
    # Microsoft
    "Copilot": "Copilot",
    "Orca": "Orca 系列", "Phi": "Phi 系列",
    # 百度
    "文心 ERNIE": "文心 ERNIE",
    # 阿里
    "通义千问": "通义千问 Qwen", "HappyHorse": "HappyHorse", "ABot-Earth": "ABot-Earth",
    # 腾讯
    "混元": "混元", "Hy-MT": "Hy-MT",
    # 字节
    "豆包": "豆包", "即梦": "即梦", "Seedance": "Seedance", "Coze 扣子": "Coze 扣子",
    # 智谱
    "智谱 GLM": "智谱 GLM",
    # 月之暗面
    "Kimi": "Kimi",
    # Mistral（欧洲）
    "Mistral 7B": "Mistral 系列", "Mixtral": "Mistral 系列",
    "Mistral Large": "Mistral 系列", "Mistral Small": "Mistral 系列",
    # Amazon
    "Nova": "Nova", "Titan": "Titan",
    # Apple
    "Apple 基础模型": "Apple 基础模型",
    # 百川
    "Baichuan": "Baichuan",
    # MiniMax
    "abab": "MiniMax 系列", "M2.5": "MiniMax 系列", "M2.7": "MiniMax 系列",
    # 讯飞星火
    "星火": "星火",
    # NVIDIA
    "Nemotron": "Nemotron 系列", "Cosmos": "Cosmos", "SANA": "SANA",
    # 其它实验室
    "Seed": "Seed", "Muse": "Muse", "MAI": "MAI",
}

# ── 模型评分（LMArena 文本榜 Arena Elo，2026-07 公开快照）──────────────────────
# 用于甘特图「按模型评分降序」排序与每行右侧评分条展示。
# 数值取自 LMArena（原 LMSYS Chatbot Arena）公开 Arena Elo 排行榜 2026 年 7 月快照，
# 代表该模型系列当前最强公开版本的盲测偏好分（100 Elo ≈ 头部对战胜率差约 64%）。
# 仅收录「有公开可比文本 Arena 分数」的模型；视频/图像/语音类或未公开独立评分的
# 产品类（如 Copilot、豆包、混元、即梦、Seedance、Coze、Nova、Titan、Apple、百川、MiniMax、星火）
# 记为 None，时间线中显示「—」，并统一排在各公司评分模型之后。
RATINGS = {
    # OpenAI
    "GPT": 1508,            # GPT-5.6 / GPT-5.5 Pro 区间
    "OpenAI o 系列": 1370,  # o3 / o4 推理系列
    "Sora": None,           # 视频生成，无文本 Arena 分
    "DALL·E": None,         # 图像生成
    # Anthropic
    "Claude": 1508,         # Claude Opus 4.8 / Fable 5 区间
    # Google
    "Gemini": 1486,         # Gemini 3.1 / 3.2 Pro
    "Gemma": 1240,          # 开源轻量
    "Veo": None,            # 视频
    # Meta
    "Llama": 1370,          # Llama 4.5 Maverick
    # xAI
    "Grok": 1476,           # Grok 4.2
    # DeepSeek
    "DeepSeek 系列": 1450,  # DeepSeek V4.5 / V4.1 Pro
    # Microsoft
    "Copilot": None,        # 基于 OpenAI，无独立公开分数
    # 百度
    "文心 ERNIE": 1475,     # ERNIE-5.1
    # 阿里
    "通义千问 Qwen": 1486,  # Qwen3.7-Max
    # 腾讯
    "混元": None,
    # 字节
    "豆包": None, "即梦": None, "Seedance": None, "Coze 扣子": None,
    # 智谱
    "智谱 GLM": 1470,       # GLM-5.2
    # 月之暗面
    "Kimi": 1466,           # Kimi K2.6
    # Mistral（欧洲）
    "Mistral 系列": 1352,   # Mistral Large 3
    # Amazon
    "Nova": None, "Titan": None,
    # Apple
    "Apple 基础模型": None,
    # 百川
    "Baichuan": None,
    # MiniMax
    "MiniMax 系列": None,
    # 讯飞星火
    "星火": None,
}

# ── LMArena Elo 评分：每日自动刷新（综合对话榜，全量）─────────────────────────
# 数据源：Cherry AI 文档每日从 lmarena.ai 直抓生成的「全量」榜单 Markdown。
#   免费、无需鉴权；上游每日自动更新（页脚标注更新时间），我们每次构建重新拉取。
#   原始页面：https://docs.cherryai.com.cn/other/lmarena  （.md 版可直接抓取解析）
#   覆盖 373 个模型，含中美欧主流及中国模型（混元/文心/通义/智谱/DeepSeek/Kimi/MiniMax/StepFun/Yi…），
#   比原社区镜像（仅 top-20）覆盖更全，且能自动补上此前为 None 的中国模型。
# 拉取结果缓存到 ratings_cache.json（带时间戳并提交）；任何失败都回退到上方静态 RATINGS。
RATINGS_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ratings_cache.json")
RATINGS_API = "https://docs.cherryai.com.cn/other/lmarena.md"
# 模型类型分类：决定评分栏如何呈现（综合对话榜 Elo 仅对文本/多模态对话模型有效；
# 图像/视频生成模型不在该榜，保持「—」并在行内以类型标签说明）。
FAM_TYPE = {
    # 文本对话大模型
    "GPT": "文本", "OpenAI o 系列": "文本", "Claude": "文本", "Gemini": "文本",
    "Gemma": "文本", "Llama": "文本", "Grok": "文本", "DeepSeek 系列": "文本",
    "文心 ERNIE": "文本", "通义千问 Qwen": "文本", "混元": "文本", "智谱 GLM": "文本",
    "Kimi": "文本", "Mistral 系列": "文本", "MiniMax 系列": "文本", "星火": "文本",
    "Baichuan": "文本", "豆包": "文本", "Coze 扣子": "文本", "Copilot": "文本",
    "Nova": "文本", "Titan": "文本", "Apple 基础模型": "文本",
    # 生成式媒体（无文本 Arena 分）
    "Sora": "视频", "Veo": "视频", "Seedance": "视频", "即梦": "图像", "DALL·E": "图像",
}
# 家族 → LMArena 模型名匹配规则：(家族key, [名称子串], [厂商子串(可空)], [排除子串])
# 评分取该家族所有命中模型中的最高 Elo（即旗舰），与上方静态字典“区间顶端”意图一致。
LM_MAP = [
    ("GPT",              ["gpt"],                  [],            []),
    ("OpenAI o 系列",     ["o1", "o3", "o4", "o2"],  [],            ["gpt"]),
    ("Claude",           ["claude"],               [],            []),
    ("Gemini",           ["gemini"],               [],            []),
    ("Gemma",            ["gemma"],                [],            []),
    ("Llama",            ["llama"],                [],            []),
    ("Grok",             ["grok"],                 [],            []),
    ("DeepSeek 系列",     ["deepseek"],             [],            []),
    ("文心 ERNIE",        ["ernie"],                [],            []),
    ("通义千问 Qwen",      ["qwen"],                 [],            []),
    ("智谱 GLM",          ["glm"],                  [],            []),
    ("混元",              ["hunyuan"],              [],            []),
    ("豆包",              ["doubao", "seed"],        [],            ["seedance", "seedream"]),
    ("Coze 扣子",          ["coze"],                 [],            []),
    ("Kimi",             ["kimi"],                 [],            []),
    ("Mistral 系列",       ["mistral"],              [],            []),
    ("Baichuan",         ["baichuan"],             [],            []),
    ("MiniMax 系列",       ["minimax", "minmax", "abab"], [],       []),
    ("星火",              ["spark", "iflytek"],     [],            []),
]

# ── 模型能力分类（三级分组 / 顶部筛选维度）──────────────────────────────────
# 每个能力：(key, emoji, 中文标签, 主题色)。顺序即「顶部筛选」与「三级分组」的展示顺序。
# Long Context（长文本）为可选维度，仍纳入。
CAP_DEFS = [
    ("Chat",        "🧠", "对话",   "#4f46e5"),
    ("Reasoning",   "🧩", "推理",   "#7c3aed"),
    ("Coding",      "💻", "代码",   "#0ea5e9"),
    ("Vision",      "👁", "视觉",   "#059669"),
    ("Image",       "🎨", "图像",   "#db2777"),
    ("Video",       "🎥", "视频",   "#e11d48"),
    ("Audio",       "🔊", "语音",   "#f59e0b"),
    ("Agent",       "🤖", "智能体", "#0891b2"),
    ("LongContext", "📚", "长文本", "#64748b"),
]
# 家族 → 能力列表（首项 = 主能力，决定三级分组归属；其余为模型行展示的副能力标签）
CAPS = {
    # OpenAI
    "GPT": ["Chat"], "OpenAI o 系列": ["Reasoning"], "Claude": ["Chat", "Reasoning", "Agent"],
    "Gemini": ["Chat", "Vision", "LongContext", "Agent"], "Gemma": ["Chat"], "Llama": ["Chat"],
    "Grok": ["Chat", "Agent"], "DeepSeek 系列": ["Chat", "Reasoning", "Coding"],
    "Copilot": ["Agent", "Chat"], "Orca 系列": ["Chat"], "Phi 系列": ["Chat"],
    # 阿里 / 百度 / 腾讯 / 智谱 / 月之暗面 / Mistral / 百川 / 字节 / 讯飞 / 苹果 / 亚马逊 / NVIDIA
    "文心 ERNIE": ["Chat", "Vision"], "通义千问 Qwen": ["Chat", "Vision", "Agent"],
    "混元": ["Chat", "Vision"], "Hy-MT": ["Audio"], "豆包": ["Chat", "Agent"],
    "即梦": ["Image", "Video"], "Seedance": ["Video"], "Coze 扣子": ["Agent"],
    "智谱 GLM": ["Chat", "Vision"], "Kimi": ["Chat", "LongContext"], "Mistral 系列": ["Chat"],
    "Nova": ["Chat"], "Titan": ["Chat"], "Apple 基础模型": ["Chat"], "Baichuan": ["Chat"],
    "MiniMax 系列": ["Chat"], "星火": ["Chat"], "Nemotron 系列": ["Chat"],
    # 生成式媒体（无文本 Arena 分）
    "Sora": ["Video"], "Veo": ["Video"], "DALL·E": ["Image"], "Magenta": ["Audio"],
    "Cosmos": ["Video"], "SANA": ["Image"], "Muse": ["Image"],
    # 其它
    "Seed": ["Chat"], "MAI": ["Reasoning", "Chat"], "Bidi": ["Agent", "Chat"],
    "GPT-Live": ["Chat", "Audio"], "GPT-Realtime": ["Audio", "Chat"],
    "HappyHorse": ["Chat", "Agent"], "ABot-Earth": ["Agent", "Chat"],
    # 兜底：未知家族归入对话
}

def _caps_of(fam):
    return CAPS.get(fam, ["Chat"])
    """拉取 Cherry AI 全量榜单 Markdown，返回 [(模型名串, Elo整数)]；失败返回 []。"""
    try:
        req = urllib.request.Request(RATINGS_API, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception:
        return []
    out = []
    for line in raw.splitlines():
        s = line.strip()
        if not s.startswith("|") or "---" in s:
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 5:
            continue
        if cells[0] == "Rank" or cells[2] == "模型":   # 跳过表头
            continue
        m = re.search(r"(\d{3,4})", cells[3])           # 分数形如 1508±7 / 1508±7Preliminary
        if not m:
            continue
        elo = int(m.group(1))
        if elo < 1000 or elo > 1800:                    # 合理性过滤
            continue
        out.append((cells[2], elo))
    return out

def fetch_live_ratings():
    """从 Cherry 全量榜单解析出 {家族key: 最高Elo}，失败返回 {}。"""
    rows = _fetch_cherry_leaderboard()
    if not rows:
        return {}
    out = {}
    for fam, name_pats, vend_pats, excl_pats in LM_MAP:
        best = None
        for model, elo in rows:
            ml = model.lower()
            if not any(p in ml for p in name_pats):
                continue
            if excl_pats and any(p in ml for p in excl_pats):
                continue
            if vend_pats and not any(v in ml for v in vend_pats):
                continue
            if best is None or elo > best:
                best = elo
        if best is not None:
            out[fam] = best
    return out

def load_ratings_cache():
    try:
        with open(RATINGS_CACHE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d.get("ratings", {}) if isinstance(d, dict) else {}
    except Exception:
        return {}

def save_ratings_cache(ratings):
    try:
        with open(RATINGS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({"updated_at": int(time.time()), "ratings": ratings},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass

_LIVE_RATINGS = {}   # 运行时评分表（缓存 + 本次拉取合并），由 init_live_ratings 填充

def init_live_ratings():
    """构建前调用：载入缓存；非 --render-only 时尝试联网刷新并落盘。永不抛异常。"""
    global _LIVE_RATINGS
    cache = load_ratings_cache()
    _LIVE_RATINGS = cache
    if RENDER_ONLY:
        return
    try:
        live = fetch_live_ratings()
        if live:
            merged = {**cache, **live}
            save_ratings_cache(merged)
            _LIVE_RATINGS = merged
    except Exception:
        pass

def resolve_rating(fam):
    """评分解析：优先用每日刷新的 live 值，缺失/失败回退静态 RATINGS。"""
    v = _LIVE_RATINGS.get(fam)
    if v is not None:
        return v
    return RATINGS.get(fam)

# ── 历史里程碑（2020–2026 模型发布 / 重大产品版本更新）───────────────────────
# 经网络核实的主要 AI 模型与产品发布时间线；仅收录「模型发布」与「产品版本更新」，
# 不收录融资 / 合作 / 研究论文（技术报告）/ 模型登陆平台 / 榜单等非发布类事件。
# 本表是甘特时间线的唯一数据源（不再从每日日报抓取，杜绝污染）。
# 字段：d=日期, c=公司(须匹配 COMPANIES), m=模型/产品名(须命中 FAMILY 归族键),
#       k=model|product, t=标题, major=是否重大发布(时间线红色高亮), src=来源。
MILESTONES = [
    # ── OpenAI ──
    {"d":"2020-06-11","c":"OpenAI","m":"GPT-3","k":"model","t":"GPT-3 发布（1750亿参数，Few-shot 里程碑）","major":True,"src":"OpenAI / 维基"},
    {"d":"2022-11-30","c":"OpenAI","m":"ChatGPT","k":"product","t":"ChatGPT 发布（基于 GPT-3.5 的对话产品）","major":True,"src":"OpenAI"},
    {"d":"2023-03-14","c":"OpenAI","m":"GPT-4","k":"model","t":"GPT-4 发布（多模态，GPT-4 Turbo 于 11 月更新）","major":True,"src":"OpenAI"},
    {"d":"2023-10-19","c":"OpenAI","m":"DALL·E 3","k":"product","t":"DALL·E 3 在 ChatGPT 中可用","major":False,"src":"OpenAI"},
    {"d":"2024-05-13","c":"OpenAI","m":"GPT-4o","k":"model","t":"GPT-4o 发布（原生多模态 omni 模型）","major":True,"src":"OpenAI"},
    {"d":"2024-09-12","c":"OpenAI","m":"o3 / o4","k":"model","t":"OpenAI o1 推理模型预览发布","major":False,"src":"OpenAI"},
    {"d":"2024-12-05","c":"OpenAI","m":"o3 / o4","k":"model","t":"OpenAI o1 正式版发布","major":False,"src":"OpenAI"},
    {"d":"2024-12-09","c":"OpenAI","m":"Sora","k":"product","t":"Sora 视频生成模型公开发布","major":False,"src":"OpenAI"},
    {"d":"2025-02-27","c":"OpenAI","m":"GPT-4.5","k":"model","t":"GPT-4.5 发布","major":False,"src":"OpenAI"},
    {"d":"2025-04-14","c":"OpenAI","m":"GPT-4.1","k":"model","t":"GPT-4.1 系列发布（API，含 mini/nano）","major":False,"src":"OpenAI"},
    {"d":"2025-08-06","c":"OpenAI","m":"gpt-oss","k":"model","t":"gpt-oss-120B/20B 开放权重模型发布","major":False,"src":"OpenAI"},
    {"d":"2025-08-07","c":"OpenAI","m":"GPT-5","k":"model","t":"GPT-5 发布（整合 o3 推理，免费开放）","major":True,"src":"OpenAI"},
    {"d":"2025-09-30","c":"OpenAI","m":"Sora 2","k":"model","t":"Sora 2 视频生成模型发布","major":True,"src":"OpenAI"},
    {"d":"2025-11-13","c":"OpenAI","m":"GPT-5.1","k":"model","t":"GPT-5.1 发布（GPT-5 首个升级版）","major":True,"src":"OpenAI"},
    {"d":"2025-12-11","c":"OpenAI","m":"GPT-5.2","k":"model","t":"GPT-5.2 发布（专业工作 / 长程智能体）","major":True,"src":"OpenAI"},
    {"d":"2026-03-06","c":"OpenAI","m":"GPT-5.4","k":"model","t":"GPT-5.4 发布（前沿推理 / 编码 / Agent）","major":True,"src":"OpenAI"},
    {"d":"2026-04-23","c":"OpenAI","m":"GPT-5.5","k":"model","t":"GPT-5.5 发布（专业复杂任务）","major":True,"src":"OpenAI"},
    {"d":"2026-07-09","c":"OpenAI","m":"GPT-5.6","k":"model","t":"GPT-5.6（Sol/Terra/Luna 系列）发布","major":True,"src":"OpenAI"},
    # ── Anthropic ──
    {"d":"2023-03-14","c":"Anthropic","m":"Claude","k":"model","t":"Claude 1 首次公开发布","major":False,"src":"Anthropic"},
    {"d":"2023-07-11","c":"Anthropic","m":"Claude","k":"model","t":"Claude 2 发布（首个面向公众）","major":False,"src":"Anthropic"},
    {"d":"2023-11-21","c":"Anthropic","m":"Claude","k":"model","t":"Claude 2.1 发布（上下文扩至 200K）","major":False,"src":"Anthropic"},
    {"d":"2024-03-04","c":"Anthropic","m":"Claude","k":"model","t":"Claude 3 系列发布（Opus/Sonnet/Haiku）","major":True,"src":"Anthropic"},
    {"d":"2024-06-20","c":"Anthropic","m":"Claude","k":"model","t":"Claude 3.5 Sonnet 发布","major":False,"src":"Anthropic"},
    {"d":"2025-02-24","c":"Anthropic","m":"Claude","k":"model","t":"Claude 3.7 Sonnet 发布（混合推理）","major":False,"src":"Anthropic"},
    {"d":"2025-05-22","c":"Anthropic","m":"Claude","k":"model","t":"Claude 4（Opus / Sonnet）发布","major":True,"src":"Anthropic"},
    {"d":"2025-11-24","c":"Anthropic","m":"Claude","k":"model","t":"Claude Opus 4.5 发布","major":True,"src":"Anthropic"},
    {"d":"2026-02-05","c":"Anthropic","m":"Claude","k":"model","t":"Claude Opus 4.6 发布","major":True,"src":"Anthropic"},
    {"d":"2026-02-17","c":"Anthropic","m":"Claude","k":"model","t":"Claude Sonnet 4.6 发布","major":False,"src":"Anthropic"},
    {"d":"2026-04-16","c":"Anthropic","m":"Claude","k":"model","t":"Claude Opus 4.7 发布（编码 / 视觉增强）","major":True,"src":"Anthropic"},
    {"d":"2026-05-28","c":"Anthropic","m":"Claude","k":"model","t":"Claude Opus 4.8 发布","major":True,"src":"Anthropic"},
    {"d":"2026-06-30","c":"Anthropic","m":"Claude","k":"model","t":"Claude Sonnet 5 发布","major":True,"src":"Anthropic"},
    # ── Google ──
    {"d":"2023-03-21","c":"Google","m":"Bard","k":"product","t":"Bard 对话式 AI 产品发布","major":False,"src":"Google"},
    {"d":"2023-12-06","c":"Google","m":"Gemini","k":"model","t":"Gemini 1.0 发布（Ultra/Pro/Nano）","major":True,"src":"Google"},
    {"d":"2024-02-15","c":"Google","m":"Gemini","k":"model","t":"Gemini 1.5 发布（百万 token 上下文）","major":False,"src":"Google"},
    {"d":"2024-02-21","c":"Google","m":"Gemma","k":"model","t":"Gemma 1 开放模型发布","major":False,"src":"Google"},
    {"d":"2024-05-14","c":"Google","m":"Veo","k":"product","t":"Veo 视频生成模型发布","major":False,"src":"Google"},
    {"d":"2024-06-01","c":"Google","m":"Gemma","k":"model","t":"Gemma 2 发布","major":False,"src":"Google"},
    {"d":"2024-12-11","c":"Google","m":"Gemini","k":"model","t":"Gemini 2.0 亮相（agentic 能力）","major":False,"src":"Google"},
    {"d":"2025-02-05","c":"Google","m":"Gemini","k":"model","t":"Gemini 2.0 正式版（GA）","major":False,"src":"Google"},
    {"d":"2025-03-25","c":"Google","m":"Gemini","k":"model","t":"Gemini 2.5 Pro 实验版首秀","major":False,"src":"Google"},
    {"d":"2025-06-17","c":"Google","m":"Gemini","k":"model","t":"Gemini 2.5 Pro / Flash 全面开放（GA）","major":False,"src":"Google"},
    {"d":"2025-11-17","c":"Google","m":"Gemini","k":"model","t":"Gemini 3 发布（Pro / Deep Think）","major":True,"src":"Google"},
    {"d":"2025-11-17","c":"Google","m":"Veo","k":"model","t":"Veo 3.1 视频生成模型发布","major":False,"src":"Google"},
    {"d":"2026-02-19","c":"Google","m":"Gemini","k":"model","t":"Gemini 3.1 Pro 发布","major":True,"src":"Google"},
    {"d":"2026-04-02","c":"Google","m":"Gemma","k":"model","t":"Gemma 4 开放权重模型发布（Apache 2.0）","major":True,"src":"Google"},
    # ── Meta ──
    {"d":"2023-02-24","c":"Meta","m":"Llama","k":"model","t":"Llama 1 开源发布","major":False,"src":"Meta"},
    {"d":"2023-07-18","c":"Meta","m":"Llama","k":"model","t":"Llama 2 开源可商用发布","major":False,"src":"Meta"},
    {"d":"2024-04-18","c":"Meta","m":"Llama","k":"model","t":"Llama 3 发布（8B/70B）","major":True,"src":"Meta"},
    {"d":"2024-07-23","c":"Meta","m":"Llama","k":"model","t":"Llama 3.1 发布（405B 旗舰）","major":False,"src":"Meta"},
    {"d":"2024-09-25","c":"Meta","m":"Llama","k":"model","t":"Llama 3.2 发布（视觉/边缘模型）","major":False,"src":"Meta"},
    {"d":"2025-04-05","c":"Meta","m":"Llama","k":"model","t":"Llama 4 发布（MoE 原生多模态）","major":True,"src":"Meta"},
    {"d":"2026-04-08","c":"Meta","m":"Llama","k":"model","t":"Llama 5 开源发布（600B MoE，5M 上下文）","major":True,"src":"Meta"},
    # ── xAI ──
    {"d":"2023-11-04","c":"xAI","m":"Grok","k":"model","t":"Grok 1 发布","major":False,"src":"xAI"},
    {"d":"2024-08-13","c":"xAI","m":"Grok","k":"model","t":"Grok 2 发布","major":False,"src":"xAI"},
    {"d":"2025-02-17","c":"xAI","m":"Grok","k":"model","t":"Grok 3 发布","major":True,"src":"xAI"},
    {"d":"2025-07-09","c":"xAI","m":"Grok","k":"model","t":"Grok 4 发布（多智能体 / 原生工具）","major":True,"src":"xAI"},
    {"d":"2025-11-19","c":"xAI","m":"Grok","k":"model","t":"Grok 4.1 发布（2M 上下文）","major":True,"src":"xAI"},
    {"d":"2026-02-17","c":"xAI","m":"Grok","k":"model","t":"Grok 4.2 发布（公开测试版，快速学习 / 每周迭代）","major":True,"src":"xAI"},
    {"d":"2026-04-30","c":"xAI","m":"Grok","k":"model","t":"Grok 4.3 发布（原生视频输入 / 1M 上下文）","major":True,"src":"xAI"},
    # ── DeepSeek（中国）──
    {"d":"2024-01-05","c":"深度求索","m":"DeepSeek","k":"model","t":"DeepSeek LLM 首个大模型发布","major":False,"src":"深度求索"},
    {"d":"2024-05-07","c":"深度求索","m":"DeepSeek","k":"model","t":"DeepSeek-V2 开源 MoE 模型发布","major":False,"src":"深度求索"},
    {"d":"2024-12-26","c":"深度求索","m":"DeepSeek","k":"model","t":"DeepSeek-V3 开源发布（6710亿参数）","major":True,"src":"深度求索"},
    {"d":"2025-01-20","c":"深度求索","m":"DeepSeek","k":"model","t":"DeepSeek-R1 推理模型开源发布","major":True,"src":"深度求索"},
    {"d":"2025-08-21","c":"深度求索","m":"DeepSeek","k":"model","t":"DeepSeek-V3.1 发布（混合推理架构）","major":True,"src":"深度求索"},
    {"d":"2025-12-01","c":"深度求索","m":"DeepSeek","k":"model","t":"DeepSeek-V3.2 发布（面向智能体）","major":True,"src":"深度求索"},
    {"d":"2026-04-24","c":"深度求索","m":"DeepSeek","k":"model","t":"DeepSeek-V4 预览版发布（Pro/Flash，开放权重）","major":True,"src":"深度求索"},
    # ── Mistral（欧洲）──
    {"d":"2023-09-27","c":"Mistral","m":"Mistral 7B","k":"model","t":"Mistral 7B 开源发布（7.3B，Apache 2.0）","major":False,"src":"Mistral AI"},
    {"d":"2023-12-09","c":"Mistral","m":"Mixtral","k":"model","t":"Mixtral 8x7B 开源发布（首个开放 MoE 模型）","major":False,"src":"Mistral AI"},
    {"d":"2024-02-26","c":"Mistral","m":"Mistral Large","k":"model","t":"Mistral Large 发布（旗舰，对标 GPT-4）","major":True,"src":"Mistral AI"},
    {"d":"2024-07-24","c":"Mistral","m":"Mistral Large","k":"model","t":"Mistral Large 2 发布（开放权重）","major":True,"src":"Mistral AI"},
    {"d":"2025-01-30","c":"Mistral","m":"Mistral Small","k":"model","t":"Mistral Small 3 发布","major":False,"src":"Mistral AI"},
    {"d":"2025-12-02","c":"Mistral","m":"Mistral Large","k":"model","t":"Mistral Large 3 发布","major":True,"src":"Mistral AI"},
    {"d":"2026-04-28","c":"Mistral","m":"Mistral Large","k":"model","t":"Mistral 3 系列模型发布","major":True,"src":"Mistral AI"},
    # ── Amazon（美国）──
    {"d":"2023-04-13","c":"Amazon","m":"Titan","k":"model","t":"Amazon Titan 基础模型随 Bedrock 推出","major":False,"src":"Amazon"},
    {"d":"2024-12-02","c":"Amazon","m":"Nova","k":"model","t":"Amazon Nova 系列发布（Micro/Lite/Pro/Premier）","major":True,"src":"Amazon"},
    {"d":"2025-04-30","c":"Amazon","m":"Nova","k":"model","t":"Amazon Nova Premier 发布","major":False,"src":"Amazon"},
    # ── Apple（美国）──
    {"d":"2024-06-10","c":"Apple","m":"Apple 基础模型","k":"product","t":"Apple Intelligence 与端侧基础模型发布（WWDC24）","major":True,"src":"Apple"},
    # ── 百川（中国）──
    {"d":"2023-06-15","c":"百川","m":"Baichuan","k":"model","t":"Baichuan-7B 中英文大模型发布","major":False,"src":"百川智能"},
    {"d":"2024-01-29","c":"百川","m":"Baichuan","k":"model","t":"Baichuan 3 超千亿参数大模型发布","major":False,"src":"百川智能"},
    {"d":"2024-05-22","c":"百川","m":"Baichuan","k":"model","t":"Baichuan 4 基座大模型发布","major":True,"src":"百川智能"},
    # ── 稀宇科技 / MiniMax（中国）──
    {"d":"2024-01-15","c":"稀宇科技","m":"abab","k":"model","t":"MiniMax abab6 全量发布（国内首个 MoE 大模型）","major":False,"src":"稀宇科技"},
    {"d":"2024-04-15","c":"稀宇科技","m":"abab","k":"model","t":"MiniMax abab6.5 万亿参数 MoE 发布","major":False,"src":"稀宇科技"},
    {"d":"2026-02-11","c":"稀宇科技","m":"M2.5","k":"model","t":"MiniMax M2.5 原生 Agent 生产级模型发布","major":True,"src":"稀宇科技"},
    # ── 讯飞星火（中国）──
    {"d":"2023-05-06","c":"讯飞星火","m":"星火","k":"model","t":"讯飞星火大模型 V1.0 发布","major":True,"src":"科大讯飞"},
    {"d":"2023-10-24","c":"讯飞星火","m":"星火","k":"model","t":"讯飞星火 V3.0 发布","major":False,"src":"科大讯飞"},
    {"d":"2024-06-27","c":"讯飞星火","m":"星火","k":"model","t":"讯飞星火 V4.0 发布（对标 GPT-4 Turbo）","major":True,"src":"科大讯飞"},
    {"d":"2025-01-15","c":"讯飞星火","m":"星火","k":"model","t":"讯飞星火 X1 深度推理模型发布","major":True,"src":"科大讯飞"},
    # ── 百度（中国）──
    {"d":"2023-03-16","c":"百度","m":"文心 ERNIE","k":"product","t":"文心一言发布","major":False,"src":"百度"},
    {"d":"2023-10-17","c":"百度","m":"文心 ERNIE","k":"model","t":"文心大模型 4.0 发布","major":False,"src":"百度"},
    {"d":"2024-06-28","c":"百度","m":"文心 ERNIE","k":"model","t":"文心大模型 4.0 Turbo 发布","major":False,"src":"百度"},
    {"d":"2025-03-16","c":"百度","m":"文心 ERNIE","k":"model","t":"文心大模型 4.5 发布（原生多模态/深度思考）","major":False,"src":"百度"},
    {"d":"2026-01-22","c":"百度","m":"文心 ERNIE","k":"model","t":"文心大模型 5.0 正式版发布（原生全模态）","major":True,"src":"百度"},
    {"d":"2026-05-09","c":"百度","m":"文心 ERNIE","k":"model","t":"文心大模型 5.1 发布","major":True,"src":"百度"},
    # ── 阿里（中国）──
    {"d":"2023-04-11","c":"阿里","m":"通义千问","k":"model","t":"通义千问发布","major":False,"src":"阿里云"},
    {"d":"2024-06-07","c":"阿里","m":"通义千问","k":"model","t":"Qwen2 大模型开源发布","major":False,"src":"阿里云"},
    {"d":"2024-09-19","c":"阿里","m":"通义千问","k":"model","t":"Qwen2.5 发布","major":False,"src":"阿里云"},
    {"d":"2025-01-29","c":"阿里","m":"通义千问","k":"model","t":"Qwen2.5-Max 旗舰模型发布","major":False,"src":"阿里云"},
    {"d":"2025-04-29","c":"阿里","m":"通义千问","k":"model","t":"Qwen3 开源（混合推理模型）","major":True,"src":"阿里云"},
    {"d":"2026-02-16","c":"阿里","m":"通义千问","k":"model","t":"Qwen3.5 旗舰大模型发布（开源）","major":True,"src":"阿里云"},
    {"d":"2026-05-20","c":"阿里","m":"通义千问","k":"model","t":"Qwen3.7-Max 旗舰模型发布","major":True,"src":"阿里云"},
    # ── 智谱（中国）──
    {"d":"2023-03-15","c":"智谱","m":"智谱 GLM","k":"model","t":"ChatGLM 对话基座模型发布","major":False,"src":"智谱"},
    {"d":"2024-01-16","c":"智谱","m":"智谱 GLM","k":"model","t":"GLM-4 基座大模型发布","major":False,"src":"智谱"},
    {"d":"2025-07-28","c":"智谱","m":"智谱 GLM","k":"model","t":"GLM-4.5 开源旗舰模型发布","major":False,"src":"智谱"},
    {"d":"2025-09-30","c":"智谱","m":"智谱 GLM","k":"model","t":"GLM-4.6 发布","major":False,"src":"智谱"},
    {"d":"2025-12-22","c":"智谱","m":"智谱 GLM","k":"model","t":"GLM-4.7 发布","major":False,"src":"智谱"},
    {"d":"2026-02-11","c":"智谱","m":"智谱 GLM","k":"model","t":"GLM-5 旗舰通用大模型发布","major":True,"src":"智谱"},
    {"d":"2026-04-08","c":"智谱","m":"智谱 GLM","k":"model","t":"GLM-5.1 发布","major":True,"src":"智谱"},
    # ── 月之暗面（中国）──
    {"d":"2023-10-09","c":"月之暗面","m":"Kimi","k":"product","t":"Kimi 智能助手发布（20万汉字上下文）","major":False,"src":"月之暗面"},
    {"d":"2025-01-20","c":"月之暗面","m":"Kimi","k":"model","t":"Kimi K1.5 多模态思考模型发布","major":False,"src":"月之暗面"},
    {"d":"2025-07-11","c":"月之暗面","m":"Kimi","k":"model","t":"Kimi K2 开源发布（万亿参数 MoE）","major":True,"src":"月之暗面"},
    {"d":"2026-01-27","c":"月之暗面","m":"Kimi","k":"model","t":"Kimi K2.5 开源多模态代理大模型发布","major":True,"src":"月之暗面"},
    {"d":"2026-04-20","c":"月之暗面","m":"Kimi","k":"model","t":"Kimi K2.6 发布","major":True,"src":"月之暗面"},
    # ── 字节（中国）──
    {"d":"2023-08-17","c":"字节","m":"豆包","k":"product","t":"豆包（云雀）AI 对话产品公测","major":False,"src":"字节"},
    {"d":"2024-05-15","c":"字节","m":"豆包","k":"model","t":"豆包大模型正式发布","major":False,"src":"字节"},
    {"d":"2025-06-11","c":"字节","m":"豆包","k":"model","t":"豆包大模型 1.6 / Seedance 1.0 发布","major":False,"src":"字节"},
    {"d":"2026-02-12","c":"字节","m":"Seedance","k":"model","t":"Seedance 2.0 视频生成大模型发布","major":True,"src":"字节"},
    {"d":"2026-02-14","c":"字节","m":"豆包","k":"model","t":"豆包大模型 2.0 全系列发布","major":True,"src":"字节"},
    # ── 腾讯（中国）──
    {"d":"2023-09-07","c":"腾讯","m":"混元","k":"model","t":"腾讯混元大模型正式亮相","major":False,"src":"腾讯"},
    {"d":"2024-05-30","c":"腾讯","m":"混元","k":"product","t":"腾讯元宝 App 上线","major":False,"src":"腾讯"},
    {"d":"2026-04-23","c":"腾讯","m":"混元","k":"model","t":"腾讯混元 Hy3 预览版发布","major":False,"src":"腾讯"},
    {"d":"2026-07-06","c":"腾讯","m":"混元","k":"model","t":"腾讯混元 Hy3 正式版发布","major":True,"src":"腾讯"},
    # ── Microsoft（美国）──
    {"d":"2023-06-01","c":"Microsoft","m":"Orca","k":"model","t":"微软发布 Orca 小模型：通过模仿大模型推理链大幅提升小模型能力","major":False,"src":"Microsoft"},
    {"d":"2024-04-23","c":"Microsoft","m":"Phi","k":"model","t":"微软发布 Phi-3 系列小语言模型（端侧 3.8B 参数达 SOTA）","major":True,"src":"Microsoft"},
    {"d":"2023-03-16","c":"Microsoft","m":"Copilot","k":"product","t":"Microsoft 365 Copilot 发布","major":False,"src":"Microsoft"},
    {"d":"2024-05-21","c":"Microsoft","m":"Copilot","k":"product","t":"Copilot+ PC / Copilot Studio 发布","major":False,"src":"Microsoft"},
]
GANTT_TOP_N = 12  # 时间线甘特图展示事件数最多的 N 家公司

# 甘特时间线「年份分组」：用于合并稀疏年份、压缩空年份。
# 2021 不单列；2020/2021/2022 合并为前置区间「2022年前」，避免与「2023」标签拥挤；其余年份独立成列。
# 每个元素：(显示标签, 起始年, 结束年)。2021 计入该区间但不单独出现。
GANTT_YEAR_BANDS = [
    ("2022年前", 2020, 2022),
    ("2023", 2023, 2023),
    ("2024", 2024, 2024),
    ("2025", 2025, 2025),
    ("2026", 2026, 2026),
]
# 甘特时间线中「产品更新」的「仅重要」过滤（激进版，仅作用于时间线；每日日报仍保留全部）。
# 判定为「次要/不展示」：① 指南/教程/观点类(_GUIDE_KW)
#   ② 点版本号 vX.Y.Z（如 Claude Code v2.1.207 这类频繁点发布）
#   ③ 常规功能增量（含 新增/功能/支持/扩展/升级/更新 等词，且未命中强信号）
# 判定为「重要/保留」的覆盖：强信号(_STRONG_KW：开源/公测/重磅/首发/全球上线/正式发布 等) 一定保留。
# 仅基于【标题】判断，避免摘要里的「功能/支持」造成误杀。
_GUIDE_KW = ["指南","如何","为何","为什么","详解","实战","盘点","教程","一文","解析",
             "案例","最佳实践","测评","评测","横评","对比"]
_PATCH_RE = re.compile(r"v?\d+\.\d+\.\d+")   # 三段式点版本号 → 点发布(视为次要)
_STRONG_KW = ["开源","公测","内测","重磅","首发","首秀","全球上线","正式可用","ga",
              "全新","新模型","新平台","正式发布","亮相"]
_INCR_KW = ["新增","新功能","支持","接入","扩展","能力","功能","增强","优化","改进",
            "多项更新","能力提升","升级","更新"]
def is_minor_product(text):
    t = text or ""
    tl = t.lower()
    if any(k in t for k in _GUIDE_KW):
        return True
    if _PATCH_RE.search(tl):
        return True
    if any(k in tl for k in _STRONG_KW):
        return False
    if any(k in tl for k in _INCR_KW):
        return True
    return False

# 模型发布「次要」识别（保守）：仅把明显的微调/蒸馏/小版本/轻量变体判为次要，
# 默认「仅重要」开启时会过滤掉它们。阈值刻意保守，避免误杀真正的旗舰发布。
_MODEL_MINOR_KW = ["微调", "fine-tune", "finetune", "蒸馏", "distill",
                   "point release", "小版本", "轻量版", "mini 版", "lite 版", "nano 版"]
def is_minor_model(text):
    tl = (text or "").lower()
    return any(k in tl for k in _MODEL_MINOR_KW)

# 重大模型更新识别（时间线红色高亮用）：命中「重大版本号 X.0/X.5」或强发布信号 → 视为重大。
# 刻意不命中三段式点版本（如 v2.1.207）以免误标日常小更新。
_MAJOR_VER_RE = re.compile(r"(?:v|V)?\d+\.(?:0|5)\b")
_MAJOR_KW = ["重磅发布", "正式发布", "全球上线", "首发", "开源", "全新", "重大更新",
             "正式可用", "ga", "重大发布", "亮相", "震撼发布"]
def is_major_model(text):
    tl = (text or "").lower()
    if _MAJOR_VER_RE.search(tl):
        return True
    if any(k in tl for k in _MAJOR_KW):
        return True
    return False

def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def traolid(permalink):
    if not permalink:
        return None
    m = re.search(r"/items/([^/?]+)", permalink)
    return m.group(1) if m else permalink

# ---------- 全文镜像：服务器端抓取原文 HTML 并抽取正文（尊重版权，保留来源与原文链接） ----------
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

try:
    import trafilatura
    _HAVE_TRAF = True
except Exception:
    _HAVE_TRAF = False

def fetch_content(url, cap=15000):
    """抓取原文并抽取正文（纯文本归档：不下载/不内嵌图片）。
    返回纯文本 Markdown 正文；失败返回空串。优先 trafilatura，缺失则降级正则。"""
    if not url:
        return ""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Accept-Language": "zh-CN,zh;q=0.9"})
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as r:
            raw = r.read(5_000_000)  # 上限 5MB，防超大页卡死
            html = raw.decode("utf-8", "ignore")
    except Exception:
        return ""
    # 优先 trafilatura 正文抽取
    text = ""
    if _HAVE_TRAF:
        try:
            res = trafilatura.extract(html, output_format="json", include_images=False, url=url)
            if res:
                d = json.loads(res)
                text = d.get("text") or ""
        except Exception:
            text = ""
    # 降级：粗滤 script/style 后去标签（trafilatura 缺失或抽取为空时仍可用）
    if not text:
        h2 = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", h2)
        text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    return text.strip()[:cap]

IMG_CAP = 6                      # 每篇最多下载前 6 张图
IMG_MAX_BYTES = 2 * 1024 * 1024  # 单图上限 2MB，超出跳过（保留外链）

def _save_img(src, page_url):
    """下载单张图片到本地 assets/img/，返回本地相对路径；失败/超限返回 None。"""
    try:
        import hashlib
        u = src.strip()
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/"):
            u = urllib.parse.urljoin(page_url, u)
        elif not u.lower().startswith("http"):
            return None
        req = urllib.request.Request(u, headers={"User-Agent": _UA, "Referer": page_url, "Accept": "image/*"})
        data = urllib.request.urlopen(req, timeout=20, context=_SSL_CTX).read()
        if len(data) > IMG_MAX_BYTES:
            return None
        if data[:3] == b"\xff\xd8\xff" or data[:2] == b"\xff\xd8":
            ext = "jpg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            ext = "png"
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            ext = "gif"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = "webp"
        else:
            ext = "jpg"
        h = hashlib.md5(u.encode("utf-8")).hexdigest()[:16]
        fn = f"{h}.{ext}"
        img_dir = os.path.join(OUT_DIR, "assets", "img")
        os.makedirs(img_dir, exist_ok=True)
        # 复用已存在的同名文件（按 md5(url) 命名），避免重复下载
        existing = [f for f in os.listdir(img_dir) if f.startswith(h + ".")]
        if existing:
            return "assets/img/" + existing[0]
        with open(os.path.join(img_dir, fn), "wb") as f:
            f.write(data)
        return "assets/img/" + fn
    except Exception:
        return None

def backfill_content(arch, workers=8):
    """并发回填缺失全文（纯文本归档，不下载图片）；写入 content。
    具备：单任务超时（避免个别慢站拖垮整体）、每 100 条增量落盘（可断点续传）、不阻塞退出。"""
    todos = []
    for d, rec in arch.items():
        for s in rec.get("sections", []):
            for it in s.get("items", []):
                # 仅回填正文缺失或过短的条目，避免对已镜像内容重复抓取
                if it.get("url") and len((it.get("content") or "").strip()) < 80:
                    todos.append(it)
    if not todos:
        print("[3.5] 全文缓存已齐，无需回填")
        return 0
    print(f"[3.5] 回填全文镜像：{len(todos)} 条（并发 {workers}）...")
    done = 0
    ex = ThreadPoolExecutor(max_workers=workers)
    futs = {ex.submit(fetch_content, it["url"]): it for it in todos}
    try:
        for f in as_completed(futs, timeout=120):
            it = futs[f]
            try:
                new = f.result()
            except Exception:
                new = ""
            # 安全策略：新抓取为空但旧内容尚在时，保留旧内容，避免丢失已镜像全文
            if new:
                it["content"] = new
            done += 1
            if done % 100 == 0:
                save_archive(arch)
                print(f"     {done}/{len(todos)}（已落盘）")
    except _FTimeout:
        print(f"    ! 回填超时（{done}/{len(todos)}），进度已落盘，重跑可续传")
    except Exception as e:
        print(f"    ! 回填异常({e})，进度已落盘")
    save_archive(arch)
    ex.shutdown(wait=False)
    ok = sum(1 for it in todos if len((it.get("content") or "").strip()) >= 80)
    print(f"     完成：已回填正文 {ok}/{len(todos)}")
    return ok

# ---------- 翻译：英文全文 → 中文（保留公司/模型名等英文专有名词） ----------
def ratio_en(s):
    if not s:
        return 0
    letters = [c for c in s if c.isascii() and c.isalpha()]
    nonsp = len([c for c in s if not c.isspace()])
    return (len(letters) / nonsp) if nonsp else 0

# 全局限速：免费端点对高频并发极敏感，统一串行限速可显著降低被限流/拦截概率
_trans_lock = threading.Lock()
_last_req = {"t": 0.0}
_MIN_INTERVAL = 1.0   # 相邻请求最小间隔(秒)

def _gtrans_one(text):
    """调用免费 Google 翻译端点翻译一段文本；失败回退原文。
    关键约束：单条必须快速返回——超时仅 6s、每镜像重试 1 次，
    避免在 CI runner 上因端点不可达而长时间挂起（会拖死整条流水线）。
    总体调度由 translate_archive 的墙钟预算控制。"""
    body = urllib.parse.urlencode({"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text}).encode()
    for host in ("translate.googleapis.com", "clients5.google.com"):
        for attempt in range(2):
            try:
                with _trans_lock:
                    now = time.time()
                    wait = _MIN_INTERVAL - (now - _last_req["t"])
                    if wait > 0:
                        time.sleep(wait)
                    _last_req["t"] = time.time()
                req = urllib.request.Request(
                    f"https://{host}/translate_a/single", data=body,
                    headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(req, timeout=6) as r:
                    data = json.loads(r.read().decode("utf-8"))
                out = "".join(seg[0] for seg in data[0] if seg and seg[0])
                if out.strip():
                    return out
            except Exception:
                time.sleep(min(2 ** attempt, 4))
    return text  # 彻底失败：回退原文（保持 zh=False 待重试语义）

def _gtrans(text):
    """长文本分块翻译（按句切分，避免 URL 过长 / 限流）。"""
    if len(text) <= 1800:
        return _gtrans_one(text)
    parts = re.split(r'(?<=[.!?])\s+', text)
    out, buf = [], ""
    for s in parts:
        if buf and len(buf) + len(s) >= 1800:
            out.append(_gtrans_one(buf))
            buf = ""
        buf = (buf + " " + s) if buf else s
    if buf:
        out.append(_gtrans_one(buf))
    return " ".join(out)

_IMG_RE = re.compile(r'^\s*!\[[^\]]*\]\([^)]*\)\s*$')

def translate_en_zh(text):
    """段落级翻译：图片标记行、纯中文行原样保留；含拉丁字母的段落送翻。"""
    paras = re.split(r'\n{1,}', text)
    out = []
    for p in paras:
        if not p.strip():
            out.append(p)
            continue
        if _IMG_RE.match(p):          # 图片标记，原样保留
            out.append(p)
            continue
        if not re.search(r'[A-Za-z]', p):  # 无英文，原样保留
            out.append(p)
            continue
        out.append(_gtrans(p))
    return "\n".join(out)

def _translate_item(it):
    c = it.get("content") or ""
    if len(c) < 120:
        it["zh"] = True
        return
    if ratio_en(c) <= 0.45:   # 已是中文为主，标记跳过
        it["zh"] = True
        return
    new = translate_en_zh(c)
    # 仅当确实翻出中文才标记完成；否则保留未完成，便于后续重试（避免假完成）
    if new and ratio_en(new) <= 0.45:
        it["content"] = new
        it["zh"] = True
    else:
        it["zh"] = False

def translate_archive(arch, wall=180):
    """将英文为主的全文翻译为中文（保留专有名词）。已翻译(it['zh'])或纯中文跳过。

    关键设计（防卡死）：串行执行 + 硬墙钟预算(wall 秒)。无论翻译端点多慢/是否可用，
    到达预算后立刻停止并返回，绝不阻塞后续「渲染 + 提交」——保证定时任务一定能完成更新。
    若连续失败达到阈值，判定端点不可用，直接放弃本轮（已译内容已落盘，下次续传）。"""
    todos = []
    for d, rec in arch.items():
        for s in rec.get("sections", []):
            for it in s.get("items", []):
                if it.get("zh"):
                    continue
                c = it.get("content") or ""
                if len(c) < 120 or ratio_en(c) <= 0.45:
                    it["zh"] = True
                    continue
                todos.append(it)
    if not todos:
        print("[3.6] 翻译：无待处理条目")
        return 0
    print(f"[3.6] 英文全文→中文：{len(todos)} 条待处理；本轮预算 {wall}s（到点即停，可续传）")
    deadline = time.time() + wall
    done = 0
    consecutive_fail = 0
    for it in todos:
        if time.time() > deadline:
            print(f"    ! 翻译墙钟预算用尽，本轮完成 {done} 条，剩余留待下次续传")
            break
        if consecutive_fail >= 8:
            print(f"    ! 连续 {consecutive_fail} 条翻译失败，判定翻译端点不可用，放弃本轮（已存 {done} 条）")
            break
        try:
            _translate_item(it)
        except Exception:
            pass
        if it.get("zh") is True:
            done += 1
            consecutive_fail = 0
        else:
            consecutive_fail += 1
        if (done + consecutive_fail) % 50 == 0:
            save_archive(arch)
    save_archive(arch)
    print(f"     本轮完成：{done}/{len(todos)}（其余 zh=False，下次续传）")
    return done

def beijing_now():
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)

def beijing_today_str():
    return beijing_now().strftime("%Y-%m-%d")

def weekday_cn(dstr):
    y, m, d = map(int, dstr.split("-"))
    return ["星期一","星期二","星期三","星期四","星期五","星期六","星期日"][datetime.date(y, m, d).weekday()]

def truncate(s, n=60):
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[:n - 1] + "…"

def fallback_lead(sections):
    """当列表接口没有 leadTitle 时，用第一个非空版块的第一个条目标题兜底。"""
    for sec in sections or []:
        if sec.get("items"):
            title = sec["items"][0].get("title", "")
            if title.strip():
                return truncate(title, 90)
    return ""

OUT_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_PATH = os.path.join(OUT_DIR, "archive.json")

def load_archive():
    if os.path.exists(ARCHIVE_PATH):
        try:
            with open(ARCHIVE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_archive(arch):
    with open(ARCHIVE_PATH, "w", encoding="utf-8") as f:
        json.dump(arch, f, ensure_ascii=False)

# ---------- 1. 取全部可用日期列表（含头条标题） ----------
arch = load_archive()
init_live_ratings()   # 载入/刷新 LMArena Elo 评分缓存（--render-only 仅载入，不联网）
if RENDER_ONLY:
    print(f"[1] 仅渲染模式：跳过列表抓取，直接使用本地归档（共 {len(arch)} 期）")
    all_dates = sorted(arch.keys(), reverse=True)
    lead_map = {d: arch[d].get("lead", "") for d in all_dates}
else:
    print(f"[1] 拉取全部可用日报列表 (take={DAILIES_TAKE}) ...")
    try:
        arch_list = http_get_json(f"{BASE}/dailies?take={DAILIES_TAKE}")
        all_dates = [it["date"] for it in arch_list.get("items", [])]
        lead_map = {it["date"]: it.get("leadTitle") or "" for it in arch_list.get("items", [])}
        all_dates.sort(reverse=True)  # 最新在前
        print(f"    共 {len(all_dates)} 期：{all_dates[0]} ... {all_dates[-1]}")
    except Exception as e:
        # 网络抖动：回退到本地已有归档，本次仅重建索引/趋势，不追加新日期
        print(f"    ! 列表拉取失败({e})，回退到本地归档 {len(arch)} 期")
        all_dates = sorted(arch.keys(), reverse=True)
        lead_map = {d: arch[d].get("lead", "") for d in all_dates}

# ---------- 2. 补全近 7 天真实发布时间（仅新生成的日期会用到） ----------
if not RENDER_ONLY:
    print("[2] 拉取 items 补全近 7 天发布时间 ...")
    since = (beijing_now() - datetime.timedelta(days=7)).strftime("%Y-%m-%dT00:00:00Z")
    id2pub = {}
    try:
        cursor = None
        pages = 0
        while True:
            pages += 1
            params = {"mode": "all", "since": since, "take": 100}
            if cursor:
                params["cursor"] = cursor
            data = http_get_json(f"{BASE}/items?" + urllib.parse.urlencode(params))
            for it in data.get("items", []):
                pid = traolid(it.get("permalink")) or it.get("id")
                if pid and it.get("publishedAt"):
                    id2pub[pid] = it["publishedAt"]
            if not data.get("hasNext") or not data.get("nextCursor") or pages >= 20:
                break
            cursor = data.get("nextCursor")
        print(f"    时间映射 {len(id2pub)} 条（翻 {pages} 页）")
    except Exception as e:
        print(f"    ! 时间补全失败({e})，新日期将退化为仅显示日期")

# ---------- 3. 逐日组装（增量：已生成的跳过，只追加新日期） ----------
def build_day_record(date):
    daily = http_get_json(f"{BASE}/daily/{date}")
    ordered = {label: [] for label, _ in SECTIONS}
    seq = 0
    for sec in daily.get("sections", []):
        label = sec.get("label")
        if label not in ordered:
            continue
        for it in sec.get("items", []):
            pid = traolid(it.get("permalink"))
            pub = id2pub.get(pid) if pid else None
            exact = pub is not None
            seq += 1
            item = {
                "seq": seq,
                "title": it.get("title", "").strip(),
                "source": it.get("sourceName", "").strip() or "AI HOT",
                "summary": truncate(it.get("summary", ""), 220),
                "url": it.get("sourceUrl") or it.get("permalink") or "",
                "publishedAt": pub or (date + "T00:00:00.000Z"),
                "exact": exact,
            }
            item["content"] = fetch_content(item["url"])  # 新日期即时抓取全文
            ordered[label].append(item)
    total = sum(len(v) for v in ordered.values())
    present = [{"label": l, "color": c, "items": ordered[l]} for l, c in SECTIONS if ordered[l]]
    meta = {
        "reportDate": date,
        "reportDateHuman": f"{date[:4]}年{int(date[5:7])}月{int(date[8:10])}日",
        "weekday": weekday_cn(date),
        "total": total,
        "source": "AI HOT",
        "sourceUrl": "https://aihot.virxact.com",
        "generatedAt": beijing_now().strftime("%Y年%m月%d日 %H:%M"),
    }
    lead = lead_map.get(date, "") or fallback_lead(present)
    return {"meta": meta, "sections": present, "lead": lead}

new_added = 0
today = beijing_today_str()
if not RENDER_ONLY:
    print("[3] 增量组装（已生成的跳过）...")
    for date in all_dates:
        if date in arch:
            continue
        try:
            arch[date] = build_day_record(date)
            new_added += 1
            print(f"    + {date}: {arch[date]['meta']['total']} 条")
        except Exception as e:
            print(f"    ! {date} 拉取失败: {e}")
    # 稳健兜底：dailies 列表接口偶发 520/限流时会漏掉「非今天」的缺失日期
    # （例如今天已抓到 7/16、但 7/15 因列表失败而漏抓）。改为直接按日期探测
    # daily 接口，回填「今天往前 10 天」内所有缺失日期，列表挂掉也能补齐近期缺口。
    try:
        base = datetime.datetime.strptime(today, "%Y-%m-%d")
        for i in range(10):
            gd = (base - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
            if gd in arch:
                continue
            try:
                probe = http_get_json(f"{BASE}/daily/{gd}")
                if probe.get("sections"):
                    arch[gd] = build_day_record(gd)
                    new_added += 1
                    print(f"    + {gd}（缺口补抓）: {arch[gd]['meta']['total']} 条")
                else:
                    print(f"    · {gd} 日报接口暂无内容，跳过")
            except Exception as e:
                print(f"    ! {gd} 补抓失败: {e}")
    except Exception as e:
        print(f"    ! 缺口补抓异常: {e}")
    save_archive(arch)
    print(f"    新增 {new_added} 期；累计 {len(arch)} 期")

# ---------- 3.5 全文镜像回填（仅补缺失/未本地化图片，已抓取的跳过） ----------
if not RENDER_ONLY and not NO_BACKFILL:
    backfill_content(arch)

# ---------- 3.6 英文全文→中文翻译（保留专有名词；断点续传，硬预算防卡死） ----------
if not RENDER_ONLY and not NO_TRANSLATE:
    translate_archive(arch)

# ---------- 4. 渲染每份日报（仅写缺失/新文件，不重写旧档） ----------
DAY_TPL = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>AI HOT 资讯 · __REPORTDATE__</title>
<style>
  :root{--bg:#f5f6fb;--card:#fff;--ink:#1f2430;--muted:#6b7280;--line:#e8eaf1;
    --shadow:0 1px 3px rgba(16,24,40,.06),0 8px 24px rgba(16,24,40,.05);}
  *{box-sizing:border-box} html{scroll-behavior:smooth}
  body{margin:0;background:var(--bg);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    -webkit-font-smoothing:antialiased;line-height:1.55;}
  a{color:inherit} .wrap{max-width:1180px;margin:0 auto;padding:0 18px}
  .hero{background:linear-gradient(135deg,#4f46e5 0%,#7c3aed 55%,#9333ea 100%);color:#fff;
    padding:42px 0 34px;position:relative;overflow:hidden;}
  .hero::after{content:"";position:absolute;right:-80px;top:-80px;width:280px;height:280px;
    background:radial-gradient(circle,rgba(255,255,255,.18),transparent 70%);border-radius:50%}
  .hero .kicker{font-size:13px;letter-spacing:.18em;text-transform:uppercase;opacity:.85;margin:0 0 6px}
  .hero h1{margin:0;font-size:34px;font-weight:800;letter-spacing:.5px}
  .hero .date-line{margin:8px 0 0;font-size:15px;opacity:.92}
  .stats{display:flex;flex-wrap:wrap;gap:10px;margin-top:22px}
  .stat{background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.22);border-radius:12px;
    padding:10px 14px;min-width:120px;backdrop-filter:blur(4px)}
  .stat .n{font-size:22px;font-weight:800;line-height:1}
  .stat .l{font-size:12.5px;opacity:.9;margin-top:4px}
  .total-pill{display:inline-flex;align-items:center;gap:8px;margin-top:18px;background:rgba(255,255,255,.16);
    padding:8px 16px;border-radius:999px;font-weight:600;font-size:14px}
  .nav{position:sticky;top:0;z-index:20;background:rgba(255,255,255,.92);backdrop-filter:blur(10px);
    border-bottom:1px solid var(--line);box-shadow:0 2px 10px rgba(16,24,40,.04)}
  .nav .wrap{display:flex;gap:8px;overflow-x:auto;padding:10px 18px;scrollbar-width:none}
  .nav .wrap::-webkit-scrollbar{display:none}
  .nav a{white-space:nowrap;text-decoration:none;font-size:13.5px;font-weight:600;color:var(--muted);
    padding:7px 13px;border-radius:999px;border:1px solid var(--line);background:#fff;transition:.15s}
  .nav a:hover{color:var(--ink);border-color:#c9cdfb}
  .nav a.home{background:#4f46e5;color:#fff;border-color:#4f46e5}
  .backhome{position:fixed;right:20px;bottom:20px;z-index:30;background:#4f46e5;color:#fff;text-decoration:none;
    font-size:14px;font-weight:700;padding:12px 18px;border-radius:999px;box-shadow:0 6px 18px rgba(79,70,229,.35);
    display:inline-flex;align-items:center;gap:7px;transition:.15s}
  .backhome:hover{background:#4338ca;transform:translateY(-1px)}
  .nav a .c{display:inline-block;min-width:20px;text-align:center;margin-left:6px;background:#eef0fe;
    color:#4f46e5;border-radius:999px;font-size:12px;padding:0 7px}
  section.block{padding:30px 0 6px}
  .sec-head{display:flex;align-items:center;gap:10px;margin:0 0 16px}
  .sec-dot{width:12px;height:12px;border-radius:4px}
  .sec-head h2{margin:0;font-size:21px;font-weight:800}
  .sec-head .cnt{font-size:13px;color:var(--muted);font-weight:600}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:16px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px 18px 16px;
    box-shadow:var(--shadow);display:flex;flex-direction:column;gap:10px;position:relative;
    border-top:3px solid var(--accent,#4f46e5);transition:transform .15s,box-shadow .15s}
  .card:hover{transform:translateY(-3px);box-shadow:0 6px 14px rgba(16,24,40,.10),0 18px 40px rgba(16,24,40,.08)}
  .card .top{display:flex;align-items:center;justify-content:space-between;gap:8px}
  .seq{display:inline-flex;align-items:center;justify-content:center;min-width:30px;height:30px;padding:0 8px;
    border-radius:9px;background:var(--accent,#4f46e5);color:#fff;font-weight:800;font-size:15px}
  .time{font-size:12.5px;color:var(--muted);font-weight:600;white-space:nowrap}
  .card h3{margin:0;font-size:16.5px;font-weight:700;line-height:1.4}
  .chip{display:inline-block;align-self:flex-start;font-size:12px;font-weight:600;color:var(--accent,#4f46e5);
    background:color-mix(in srgb,var(--accent,#4f46e5) 10%,#fff);border:1px solid color-mix(in srgb,var(--accent,#4f46e5) 22%,#fff);
    padding:3px 10px;border-radius:999px}
  .summary{margin:0;font-size:14px;color:#3b4252;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
  .readmore{margin-top:auto;display:inline-flex;align-items:center;gap:5px;align-self:flex-start;text-decoration:none;
    font-size:13.5px;font-weight:700;color:var(--accent,#4f46e5)}
  .readmore:hover{text-decoration:underline}
  footer{margin-top:36px;padding:26px 0 40px;border-top:1px solid var(--line);color:var(--muted);font-size:13px;text-align:center}
  footer a{color:#4f46e5;text-decoration:none}
  @media (max-width:560px){.hero h1{font-size:27px}.grid{grid-template-columns:1fr}.stat{min-width:0;flex:1 1 40%}}
  /* 站内统一阅读面板 */
  .card{cursor:pointer}
  .reader-overlay{position:fixed;inset:0;z-index:60;background:rgba(18,20,32,.55);backdrop-filter:blur(3px);
    display:flex;align-items:flex-start;justify-content:center;padding:40px 16px;overflow:auto}
  .reader-overlay[hidden]{display:none}
  .reader{background:#fff;border-radius:18px;max-width:840px;width:100%;box-shadow:0 30px 80px rgba(0,0,0,.32);
    display:flex;flex-direction:column;max-height:90vh;overflow:hidden;animation:pop .18s ease}
  @keyframes pop{from{transform:translateY(8px) scale(.98);opacity:0}to{transform:none;opacity:1}}
  .reader-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;
    padding:20px 24px 14px;background:linear-gradient(135deg,#4f46e5,#7c3aed);color:#fff}
  .reader-head h2{margin:0 0 8px;font-size:20px;line-height:1.45;font-weight:800}
  .reader-head .chip{background:rgba(255,255,255,.18);color:#fff;border:1px solid rgba(255,255,255,.3)}
  .reader-x{background:rgba(255,255,255,.2);border:none;color:#fff;font-size:24px;line-height:1;
    width:38px;height:38px;border-radius:10px;cursor:pointer;flex:0 0 auto}
  .reader-x:hover{background:rgba(255,255,255,.34)}
  .reader-body{padding:20px 24px;overflow:auto;color:#2a2f3a;font-size:15.5px;line-height:1.85}
  .reader-body p{margin:0 0 14px}
  .reader-body .r-img-cap{display:inline-block;margin:6px 0;padding:4px 10px;border-radius:8px;background:#f1f3f9;color:#8a93a6;font-size:13px;font-style:italic;border:1px dashed #d7dce8}
  .reader-body .r-empty{color:#6b7280;font-style:italic}
  .reader-body .r-summary{margin:0 0 14px;white-space:pre-wrap}
  .reader-body a.r-link{color:#2563eb;text-decoration:underline;text-underline-offset:2px;word-break:break-all}
  .reader-body a.r-link:hover{color:#1d4ed8}
  .reader-body .r-fallback{background:#f5f3ff;border:1px solid #e4defb;color:#5b21b6;border-radius:10px;
    padding:10px 14px;font-size:13px;line-height:1.7;margin:0 0 16px}
  .reader-body .r-fallback b{color:#6d28d9}
  .reader-foot{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;
    padding:14px 24px 18px;border-top:1px solid var(--line);background:#fafbff}
  .reader-foot .r-note{font-size:12.5px;color:var(--muted);max-width:62%}
  .reader-foot .readmore{margin:0}
</style>
</head>
<body>
  <header class="hero"><div class="wrap">
    <p class="kicker">AI HOT Daily · 晨报</p>
    <h1>AI 资讯 · __REPORTDATEHUMAN__</h1>
    <p class="date-line">__WEEKDAY__ · 数据来源：__SOURCE__</p>
    <div class="total-pill">📊 当日共 <strong style="margin:0 4px">__TOTAL__</strong> 条 AI 动态</div>
    <div class="stats" id="stats"></div>
  </div></header>
  <nav class="nav"><div class="wrap" id="nav"><a href="index.html" class="home">📰 AI资讯杂志</a></div></nav>
  <main class="wrap" id="main"></main>
  <footer>
    <div>本日报共收录 <strong>__TOTAL__</strong> 条动态 · 数据来源：<a href="__SOURCEURL__" target="_blank" rel="noopener noreferrer">__SOURCE__（aihot.virxact.com）</a></div>
    <div style="margin-top:6px">生成于 __GENERATEDAT__ · 时间换算为北京时间 · 点「来源 ↗」跳转原始报道</div>
    <div style="margin-top:8px;opacity:.85">说明：本条新闻的标题、摘要、来源、日期已<b>镜像保存在本页</b>；即便外部链接失效或无法访问境外站点，已存档内容仍可正常查看。</div>
  </footer>
  <a href="index.html" class="backhome" title="返回 AI资讯杂志 首页">📰 返回 AI资讯杂志</a>
  <div class="reader-overlay" id="reader" hidden>
    <div class="reader" role="dialog" aria-modal="true">
      <div class="reader-head">
        <div style="min-width:0">
          <h2 id="readerTitle"></h2>
          <span class="chip" id="readerSource"></span>
        </div>
        <button class="reader-x" onclick="closeReader()" aria-label="关闭">×</button>
      </div>
      <div class="reader-body" id="readerBody"></div>
      <div class="reader-foot">
        <span class="r-note">本文由 <b>AI资讯杂志</b> 整理镜像，原文版权归原作者所有 · 点击右侧按钮查看原始报道</span>
        <a id="readerLink" class="readmore" target="_blank" rel="noopener noreferrer">查看原文 ↗</a>
      </div>
    </div>
  </div>
<script>
const DATA = __DATA__;
function pad(n){return n<10?"0"+n:""+n;}
function toBeijingHuman(iso, ref, exact){
  const d=new Date(iso); const bj=new Date(d.getTime()+8*3600*1000);
  const Y=bj.getUTCFullYear(),M=bj.getUTCMonth()+1,D=bj.getUTCDate();
  const h=bj.getUTCHours(),m=bj.getUTCMinutes(); const hh=pad(h),mm=pad(m);
  if(!exact) return M+"月"+D+"日";
  const [rY,rM,rD]=ref.split("-").map(Number);
  if(Y===rY&&M===rM&&D===rD) return "今天 "+hh+":"+mm;
  const yest=new Date(rY,rM-1,rD); yest.setDate(yest.getDate()-1);
  if(Y===yest.getFullYear()&&M===yest.getMonth()+1&&D===yest.getDate()) return "昨天 "+hh+":"+mm;
  return M+"月"+D+"日 "+hh+":"+mm;
}
const meta=DATA.meta, sections=DATA.sections, ref=meta.reportDate;
const statsEl=document.getElementById("stats");
sections.forEach(s=>{const d=document.createElement("div");d.className="stat";
  d.innerHTML='<div class="n">'+s.items.length+'</div><div class="l">'+s.label+'</div>';statsEl.appendChild(d);});
const navEl=document.getElementById("nav");
sections.forEach((s,i)=>{const a=document.createElement("a");a.href="#sec-"+i;
  a.innerHTML=escapeHtml(s.label)+'<span class="c">'+s.items.length+'</span>';navEl.appendChild(a);});
const mainEl=document.getElementById("main");
sections.forEach((s,i)=>{
  const sec=document.createElement("section");sec.className="block";sec.id="sec-"+i;
  const head=document.createElement("div");head.className="sec-head";
  head.innerHTML='<span class="sec-dot" style="background:'+s.color+'"></span><h2>'+escapeHtml(s.label)+'</h2><span class="cnt">'+s.items.length+' 条</span>';
  sec.appendChild(head);
  const grid=document.createElement("div");grid.className="grid";
  s.items.forEach(it=>{
    const card=document.createElement("article");card.className="card";card.style.setProperty("--accent",s.color);
    const time=toBeijingHuman(it.publishedAt,ref,it.exact);const url=it.url||"#";
    card.innerHTML='<div class="top"><span class="seq">'+it.seq+'</span><span class="time">'+time+'</span></div>'+
      '<h3>'+escapeHtml(it.title)+'</h3><span class="chip">'+escapeHtml(it.source)+'</span>'+
      '<p class="summary">'+escapeHtml(it.summary)+'</p>'+
      '<a class="readmore" href="'+escapeAttr(url)+'" target="_blank" rel="noopener noreferrer" title="外部原文链接，可能需要访问境外站点">来源 ↗</a>';
    card.addEventListener("click", function(){ openReader(it); });
    const rm = card.querySelector(".readmore");
    rm.addEventListener("click", function(e){ e.stopPropagation(); });
    grid.appendChild(card);
  });
  sec.appendChild(grid);mainEl.appendChild(sec);
});
function escapeHtml(s){return (s||"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function escapeAttr(s){return escapeHtml(s);}
// 渲染正文（纯文本归档）：图片不加载远程资源，仅以文字占位符呈现；其余文本按段落转义（防 XSS）
function linkify(escaped){
  if(!escaped) return escaped;
  var re=/(\[[^\]]+\]\((https?:\/\/[^)\s]+)\))|(https?:\/\/[^\s<>"'）】」』，。、；：！？]+)/g;
  return escaped.replace(re, function(m, g1, g2, g3){
    if(g2!==undefined){
      var label=m.substring(m.indexOf('[')+1, m.indexOf(']'));
      return '<a class="r-link" href="'+g2+'" target="_blank" rel="noopener noreferrer">'+label+'</a>';
    }
    if(g3!==undefined){
      return '<a class="r-link" href="'+g3+'" target="_blank" rel="noopener noreferrer">'+g3+'</a>';
    }
    return m;
  });
}
var _IMG_RE=/!\[([^\]]*)\]\(([^)\s]+)\)/g;
function renderRich(text){
  return (text||"").split(/\n{1,}/).map(function(p){
    p=p.trim(); if(!p) return '';
    var out=''; var last=0; var m; _IMG_RE.lastIndex=0;
    while((m=_IMG_RE.exec(p))!==null){
      out+=linkify(escapeHtml(p.slice(last,m.index)));
      var cap=(m[1]||'').trim()||'图片';
      out+='<span class="r-img-cap">🖼 '+escapeHtml(cap)+'</span>';
      last=_IMG_RE.lastIndex;
    }
    out+=linkify(escapeHtml(p.slice(last)));
    return '<p>'+out+'</p>';
  }).join("");
}
function openReader(it){
  const r=document.getElementById("reader");
  const content=(it.content||"").trim();
  const body=document.getElementById("readerBody");
  if(content.length>0){
    body.innerHTML=renderRich(content);
  }else{
    const sum=(it.summary||"").trim();
    if(sum){
      // 来源站点多为付费墙 / 登录墙 / 反爬限制，无法镜像全文；展示已本地存档的中文摘要兜底
      body.innerHTML='<div class="r-fallback">⚠️ 该条新闻的<b>全文镜像暂不可用</b>（来源站点可能为付费墙 / 登录墙 / 反爬限制，或原文已失效）。下方为已<b>本地存档的中文摘要</b>，可正常查看；完整原文请点击右下角「查看原文 ↗」。</div>'+
        '<p class="r-summary">'+linkify(escapeHtml(sum))+'</p>';
    }else{
      body.innerHTML='<p class="r-empty">暂未获取到该条新闻的全文镜像与摘要。'+(it.url?'可点击右下角「查看原文 ↗」前往原始报道。':'')+'</p>';
    }
  }
  document.getElementById("readerTitle").textContent=it.title;
  document.getElementById("readerSource").textContent=it.source;
  const link=document.getElementById("readerLink");
  if(it.url){link.href=it.url;link.style.display="";}else{link.style.display="none";}
  r.hidden=false;document.body.style.overflow="hidden";
}
function closeReader(){const r=document.getElementById("reader");r.hidden=true;document.body.style.overflow="";}
document.getElementById("reader").addEventListener("click",function(e){if(e.target===this)closeReader();});
document.addEventListener("keydown",function(e){if(e.key==="Escape")closeReader();});
</script>
</body></html>
"""

INDEX_TPL = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>AI资讯杂志</title>
<style>
  :root{--bg:#f5f6fb;--card:#fff;--ink:#1f2430;--muted:#6b7280;--line:#e8eaf1;
    --shadow:0 1px 3px rgba(16,24,40,.06),0 8px 24px rgba(16,24,40,.05);}
  *{box-sizing:border-box} html{scroll-behavior:smooth}
  body{margin:0;background:var(--bg);color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    -webkit-font-smoothing:antialiased;line-height:1.55;}
  a{color:inherit;text-decoration:none} .wrap{max-width:1180px;margin:0 auto;padding:0 18px}
  .hero{background:linear-gradient(135deg,#4f46e5 0%,#7c3aed 55%,#9333ea 100%);color:#fff;
    padding:44px 0 36px;position:relative;overflow:hidden;}
  .hero::after{content:"";position:absolute;right:-80px;top:-80px;width:280px;height:280px;
    background:radial-gradient(circle,rgba(255,255,255,.18),transparent 70%);border-radius:50%}
  .hero .kicker{font-size:13px;letter-spacing:.18em;text-transform:uppercase;opacity:.85;margin:0 0 6px}
  .hero h1{margin:0;font-size:34px;font-weight:800}
  .hero .sub{margin:8px 0 0;font-size:15px;opacity:.92}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px;padding:30px 0 10px}
  .day{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px;
    box-shadow:var(--shadow);display:flex;flex-direction:column;gap:10px;transition:transform .15s,box-shadow .15s}
  .day:hover{transform:translateY(-3px);box-shadow:0 6px 14px rgba(16,24,40,.10),0 18px 40px rgba(16,24,40,.08)}
  .day .top{display:flex;align-items:baseline;justify-content:space-between;gap:8px}
  .day .date{font-size:20px;font-weight:800}
  .day .wd{font-size:13px;color:var(--muted);font-weight:600}
  .badge{font-size:11px;font-weight:700;color:#fff;background:#4f46e5;padding:2px 9px;border-radius:999px}
  .lead{font-size:14px;color:#3b4252;margin:0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .chips{display:flex;flex-wrap:wrap;gap:6px}
  .chip{font-size:11.5px;font-weight:600;color:#4f46e5;background:#eef0fe;border-radius:999px;padding:2px 9px}
  .foot{display:flex;align-items:center;justify-content:space-between;margin-top:auto;padding-top:6px}
  .total{font-size:13px;color:var(--muted);font-weight:600}
  .go{font-size:13.5px;font-weight:700;color:#4f46e5}
  footer{margin-top:26px;padding:26px 0 40px;border-top:1px solid var(--line);color:var(--muted);font-size:13px;text-align:center}
  footer a{color:#4f46e5;text-decoration:none}
  .trend-head{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:14px}
  .trend-head h2{margin:0;font-size:22px;font-weight:800}
  .trend-sub{margin:0;font-size:13px;color:var(--muted)}
  .chart-wrap{position:relative;background:var(--card);border:1px solid var(--line);border-radius:16px;
    overflow:hidden;box-shadow:0 1px 3px rgba(16,24,40,.04)}
  .gantt-sticky-years{position:fixed;top:0;left:0;right:0;z-index:15;display:none;
    background:#fff;border-bottom:1px solid var(--line);box-shadow:0 2px 10px rgba(16,24,40,.07)}
  .gantt-sticky-inner{max-width:1180px;margin:0 auto;padding:0 18px}
  .gantt-sticky-inner svg{width:100%;display:block}
    padding:14px 14px 6px;box-shadow:var(--shadow)}
  .gantt{margin:30px 0 6px}
  .gantt-ctrl{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
  .gbtn{font-size:13px;font-weight:600;color:#4b5161;border:1px solid var(--line);background:#fff;
    padding:7px 14px;border-radius:999px;cursor:pointer;user-select:none;transition:.15s}
  .gbtn:hover{border-color:#c9cdfb}
  .gbtn.active{color:#fff}
  .gbtn[data-kind=model].active{background:#4f46e5;border-color:#4f46e5}
  .gbtn[data-mode].active{background:#4f46e5;border-color:#4f46e5}
  .glegend{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;font-weight:700;
    border-radius:8px;padding:6px 13px;user-select:none;cursor:pointer;transition:.15s;
    background:color-mix(in srgb, var(--lc) 12%, #fff);color:var(--lc);
    border:1.5px solid color-mix(in srgb, var(--lc) 28%, #fff)}
  .glegend:hover{background:color-mix(in srgb, var(--lc) 22%, #fff)}
  .glegend.active{background:var(--lc);color:#fff;border-color:var(--lc);
    box-shadow:0 2px 8px color-mix(in srgb, var(--lc) 35%, transparent)}
  .glegend.dim{opacity:.35}
  .glegend .lg-dot{width:8px;height:8px;border-radius:2px;display:inline-block;background:currentColor}
  .gsep{width:1px;background:var(--line);margin:3px 4px}
  .gcap-wrap{display:inline-flex;gap:6px;flex-wrap:wrap;align-items:center}
  .gcap{font-size:12.5px;font-weight:700;color:#4b5161;border:1px solid var(--line);background:#fff;
    padding:6px 12px;border-radius:999px;cursor:pointer;user-select:none;transition:.15s}
  .gcap:hover{border-color:#c9cdfb}
  .gcap.active{color:#fff;background:#4f46e5;border-color:#4f46e5}
  #ganttChart{width:100%;height:auto;display:block;cursor:grab;border-radius:12px;overflow:hidden;box-shadow:0 2px 12px rgba(16,24,40,.05)}
  #ganttChart:active{cursor:grabbing}
  #ganttChart .gev{cursor:pointer}
  #ganttChart .gev:hover{filter:drop-shadow(0 0 5px rgba(31,36,48,.32))}
  #ganttChart .grow{transition:fill .12s}
  #ganttChart .grow:hover{fill:color-mix(in srgb, var(--mc) 11%, #ffffff)}
  #ganttChart .gtag{cursor:default}
  #ganttChart .gtag rect{transition:fill .12s}
  #ganttChart .gtag text{transition:fill .12s}
  #ganttChart .gtag:hover rect{fill:#e6e8ef}
  #ganttChart .gtag:hover text{fill:#374151}
  #ganttChart .ccname{transition:filter .12s}
  #ganttChart .ccname:hover{filter:brightness(0.82)}
  #ganttChart .gtoday{font-size:9px;font-weight:800;fill:#fff}
  #ganttTip{position:absolute;display:none;pointer-events:none;background:#1f2430;color:#fff;
    font-size:12px;line-height:1.55;padding:8px 10px;border-radius:10px;box-shadow:0 6px 18px rgba(16,24,40,.25);
    z-index:5;max-width:300px}
  #ganttTip b{font-weight:700}
  .year-h{font-size:26px;font-weight:800;margin:30px 0 4px;color:#1f2430;letter-spacing:.5px}
  .idx-head{margin:26px 0 2px}
  .idx-head h2{margin:0;font-size:22px;font-weight:800}
  .idx-head .trend-sub{margin:6px 0 0;font-size:12.5px}
  /* 年份索引按钮栏（吸顶） */
  .year-tabs{position:sticky;top:0;z-index:6;display:flex;flex-wrap:wrap;gap:8px;align-items:center;
    margin:14px 0 6px;padding:10px 12px;background:rgba(255,255,255,.92);backdrop-filter:blur(6px);
    border:1px solid var(--line);border-radius:12px}
  .year-tabs .yt-label{font-size:12.5px;color:var(--muted);font-weight:600;margin-right:2px}
  .year-tabs .yt-btn{font-size:13px;font-weight:700;color:#4b5163;background:#f1f3f9;border:1px solid transparent;
    border-radius:999px;padding:5px 14px;cursor:pointer;transition:all .15s}
  .year-tabs .yt-btn:hover{background:#e7eafb;color:#3730a3}
  .year-tabs .yt-btn.active{background:#4f46e5;color:#fff;border-color:#4f46e5}
  /* 每月一行：左侧月份，右侧横滑卡片窗口 + 下方拖动条 */
  .month-row{display:flex;align-items:stretch;gap:18px;padding:18px 0;border-bottom:1px solid var(--line)}
  .month-head{flex:0 0 88px;display:flex;flex-direction:column;justify-content:center;gap:6px}
  .month-name{font-size:24px;font-weight:800;color:#1f2430;line-height:1}
  .month-cnt{font-size:12px;font-weight:700;color:#4f46e5;background:#eef0fe;padding:3px 0;border-radius:999px;text-align:center}
  .month-carousel{flex:1;min-width:0}
  .month-track{display:flex;gap:12px;overflow-x:auto;scroll-behavior:smooth;
    scrollbar-width:none;-ms-overflow-style:none;padding:16px 2px 4px}
  .month-track::-webkit-scrollbar{display:none}
  .day-mini{flex:0 0 212px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:13px 15px;
    text-decoration:none;color:inherit;display:flex;flex-direction:column;gap:8px;transition:border-color .15s,box-shadow .15s;cursor:pointer}
  .day-mini:hover{border-color:#c9cdfb;box-shadow:0 6px 16px rgba(16,24,40,.10)}
  .day-latest{border:2px solid #4f46e5;background:linear-gradient(135deg,#eef0fe,#f8f9ff);box-shadow:0 4px 14px rgba(79,70,229,.18);position:relative}
  .day-latest::after{content:"最新";position:absolute;top:-8px;right:10px;background:#4f46e5;color:#fff;font-size:10px;font-weight:800;padding:1px 8px;border-radius:999px;letter-spacing:.5px;line-height:1.5}
  .day-latest .dm-date{color:#4f46e5}
  .dm-date{font-size:15px;font-weight:800;color:#1f2430}
  .dm-date .wd{font-size:12px;font-weight:600;color:#9aa1b1;margin-left:6px}
  .dm-lead{font-size:12px;color:#6b7280;line-height:1.5;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .dm-chips{display:flex;flex-wrap:wrap;gap:5px}
  .dm-chips .chip{font-size:10.5px;color:#6b7280;background:#f1f3f9;border-radius:6px;padding:2px 7px}
  .dm-total{font-size:11.5px;color:#9aa1b1;margin-top:auto}
  .month-scroll{width:100%;margin-top:12px;accent-color:#4f46e5;cursor:pointer;height:5px}
  @media (max-width:560px){.hero h1{font-size:27px}.grid{grid-template-columns:1fr}
    .month-row{flex-direction:column;gap:10px}.month-head{flex:0 0 auto;flex-direction:row;align-items:baseline;gap:10px}
    .day-mini{flex-basis:168px}}
</style>
</head>
<body>
  <header class="hero"><div class="wrap">
    <p class="kicker">AI HOT Daily · Magazine</p>
    <h1>AI 资讯杂志</h1>
    <p class="sub">__RANGE__ · 共 __NDAYS__ 期 · 数据来源：__SOURCE__</p>
  </div></header>
  <section class="gantt wrap">
    <div class="trend-head">
      <h2>🗓️ 主要 AI 公司 模型发布 / 版本更新 时间线</h2>
      <p class="trend-sub">信息层级（参照 Apple / Linear 风格）：<b>国家为章节（弱化）→ 公司为视觉锚点（左侧品牌色竖线 + Logo 预留）→ 模型为浏览对象</b>。每个模型显示「名称 + Arena Elo + 能力标签」，能力仅作标签、不再设分组标题，阅读更连续。🇺🇸美国 / 🇨🇳中国 / 🇫🇷法国 三阵营分章；横向为日期，🔵蓝=模型版本发布，🟢绿=模型产品更新，🔴红=模型重磅更新（<b>纯模型视角</b>，仅收录模型发布 / 版本更新）。顶部「能力」筛选可只看某一赛道。Arena Elo 为该系列最强公开版本分数（无公开分数者显示「—」），每日自动同步。</p>
    </div>
    <div class="gantt-ctrl">
      <span class="glegend" data-legend="blue" style="--lc:#4f46e5" title="点击仅显示模型版本发布">
        <i class="lg-dot"></i>模型版本发布</span>
      <span class="glegend" data-legend="green" style="--lc:#059669" title="点击仅显示模型产品更新">
        <i class="lg-dot"></i>模型产品更新</span>
      <span class="glegend" data-legend="red" style="--lc:#ef4444" title="点击仅显示模型重磅更新">
        <i class="lg-dot"></i>模型重磅更新</span>
      <span class="gsep"></span>
      <span class="gcap-wrap" id="capFilters"></span>
      <span class="gsep"></span>
      <span style="align-self:center;font-size:12.5px;color:var(--muted)">标记：</span>
      <button class="gbtn active" data-mode="block">▮ 方块</button>
      <button class="gbtn" data-mode="dot">● 圆点</button>
      <button class="gbtn" data-mode="bar">▎ 竖条</button>
      <button class="gbtn" id="ganttReset" style="margin-left:auto">↺ 重置视图</button>
      <span id="ganttRange" style="font-size:12.5px;color:var(--muted);align-self:center"></span>
    </div>
    <div id="ganttStickyYears" class="gantt-sticky-years">
      <div class="gantt-sticky-inner">
        <svg id="ganttYearsSvg"></svg>
      </div>
    </div>
    <div class="chart-wrap">
      <svg id="ganttChart" preserveAspectRatio="xMidYMid meet" role="img" aria-label="主要 AI 公司模型与产品更新时间线"></svg>
      <div id="ganttTip"></div>
    </div>
    <p class="trend-sub" style="margin-top:8px">提示：点击上方彩色图例（模型版本发布 / 模型产品更新 / 模型重磅更新）可单独查看该类事件的甘特图，再点一次或「重置视图」恢复全部；在图上滚动鼠标滚轮可放大/缩小某一时间段，按住拖动可平移时间轴。</p>
  </section>
  <main class="wrap">
    <div class="idx-head">
      <h2>📚 资讯归档（按年 / 月）</h2>
      <p class="trend-sub">每月一行，拖动滑块在当月各日期间快速跳转（左=当月最后一日，右=1日，降序）；松手或点「打开 ↗」进入当日完整日报。</p>
    </div>
    <div id="yearTabs" class="year-tabs"><span class="yt-label">年份：</span></div>
    <div id="archive"></div>
  </main>
  <footer>
    <div>数据来源：<a href="__SOURCEURL__" target="_blank" rel="noopener noreferrer">AI HOT（aihot.virxact.com）</a> · 生成于 __GENERATEDAT__</div>
    <div style="margin-top:6px">点击任意一期查看当日完整日报（含五版块与原文跳转）</div>
    <div style="margin-top:8px;opacity:.85">说明：本页已把每条新闻的标题、摘要、来源、日期<b>镜像保存到本地</b>；外部「原文」链接仅作溯源，即使链接失效或无法访问境外站点，已存档内容仍可正常查看。</div>
  </footer>
<script>
const DAYS = __DAYS__;
const GROUPS = __GROUPS__;
const GANTT = __GANTT__;
const GANTT_BANDS = __GANTT_BANDS__;   // [[label,y0,y1], ...] 年份分组（含合并规则）
// ---- 按 年→月 分组：每月一行 = 左月份 + 右横滑卡片窗口（3-4 张可见）+ 下方拖动条 ----
// 卡片按日期降序排列：左=月末，右=1日；拖动下方滑块从月末向 1 日翻动
const ARCHIVE=document.getElementById("archive");
const MGAP=12; // 与 CSS .month-track gap 一致
let latestDone=false;   // 标记最新一期卡片（仅第一张=全局最新）
function renderMonths(){
  GROUPS.forEach(g=>{
    const ysec=document.createElement("section"); ysec.className="year"; ysec.dataset.year=g.year;
    const yh=document.createElement("h2"); yh.className="year-h"; yh.textContent=g.year+" 年"; ysec.appendChild(yh);
    g.months.forEach(mo=>{
      const days=mo.days; if(!days.length) return;
      const row=document.createElement("div"); row.className="month-row";
      const head=document.createElement("div"); head.className="month-head";
      const nm=document.createElement("div"); nm.className="month-name"; nm.textContent=mo.month+" 月";
      const ct=document.createElement("div"); ct.className="month-cnt"; ct.textContent=days.length+" 期";
      head.append(nm,ct);
      const car=document.createElement("div"); car.className="month-carousel";
      const track=document.createElement("div"); track.className="month-track";
      days.forEach(d=>{
        const a=document.createElement("a"); a.className="day-mini"; a.href=d.file;
        if(!latestDone){ a.classList.add("day-latest"); latestDone=true; }
        const chips=(d.sections||[]).slice(0,3).map(s=>'<span class="chip">'+escapeHtml(s.label)+' '+s.count+'</span>').join("");
        a.innerHTML=
          '<div class="dm-date">'+d.meta.reportDateHuman.replace(/^\d+年/,'')+'<span class="wd">'+d.meta.weekday+'</span></div>'+
          (d.lead?'<div class="dm-lead">'+escapeHtml(d.lead.slice(0,46))+'</div>':'')+
          '<div class="dm-chips">'+chips+'</div>'+
          '<div class="dm-total">共 '+d.meta.total+' 条</div>';
        track.appendChild(a);
      });
      const scroll=document.createElement("input"); scroll.type="range"; scroll.className="month-scroll";
      scroll.min=0; scroll.value=0;
      car.append(track,scroll);
      row.append(head,car);
      ysec.appendChild(row);
      // 横滑：滑块控制 track 的 scrollLeft（原生滚动，左=月末、右=1日，降序）
      const sync=()=>{
        const max=Math.max(0, track.scrollWidth - track.clientWidth);
        scroll.max=max; scroll.step=Math.max(1, Math.round(max/100));
        scroll.style.display = max>4 ? "block" : "none";
        scroll.value=0; track.scrollLeft=0;
      };
      requestAnimationFrame(sync);
      scroll.addEventListener("input",()=>{ track.scrollLeft=parseInt(scroll.value,10); });
      track.addEventListener("scroll",()=>{ scroll.value=track.scrollLeft; });
      window.addEventListener("resize",sync);
    });
    ARCHIVE.appendChild(ysec);
  });
}
renderMonths();
// ---- 年份索引按钮：按数据实际存在的年份动态生成（如 2025/2027 有数据则自动出现） ----
function buildYearTabs(){
  const tabs=document.getElementById("yearTabs");
  if(!tabs || !GROUPS.length) return;
  const years=GROUPS.map(g=>g.year);
  const onPick=(y)=>{
    tabs.querySelectorAll(".yt-btn").forEach(x=>x.classList.remove("active"));
    let first=null;
    document.querySelectorAll("#archive .year").forEach(sec=>{
      const show=(y===""||sec.dataset.year===y);
      sec.style.display=show?"":"none";
      if(show && !first) first=sec;
    });
    const btn=[...tabs.querySelectorAll(".yt-btn")].find(b=>(b.dataset.year||"")===y);
    if(btn) btn.classList.add("active");
    if(first) first.scrollIntoView({behavior:"smooth",block:"start"});
  };
  const make=(label,y,active)=>{
    const b=document.createElement("button"); b.className="yt-btn"+(active?" active":"");
    b.textContent=label; b.dataset.year=y;
    b.addEventListener("click",()=>onPick(y)); return b;
  };
  tabs.appendChild(make("全部","",true));
  years.forEach(y=> tabs.appendChild(make(y+" 年",String(y),false)));
}
buildYearTabs();
function escapeHtml(s){return (s||"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}

// ---------- 主要 AI 公司 模型/产品 更新时间线（甘特式 + 缩放/拖动，纯 SVG） ----------
(function(){
  const G=GANTT; if(!G.regions || !G.regions.length) return;
  const svg=document.getElementById("ganttChart");
  const W=960,L=200,R=12,T=18,B=12,rowH=40;   // R 收敛为右侧留白；行高加大以容纳「模型名+Elo」与「能力标签」两行
  // 国家仅作章节（弱化），公司才是视觉锚点；品牌色只点缀（3px 竖线 / Hover / Logo 描边）
  const REGION_LABEL={us:"🇺🇸 美国",cn:"🇨🇳 中国",eu:"🇫🇷 法国"};
  const FLAG={us:"🇺🇸",cn:"🇨🇳",eu:"🇫🇷"};
  const headerH=44;                            // 国家章节头：标题(18px) + 浅灰副标题(模型/公司数)
  const compH=28;                              // 公司头：品牌色 3px 竖线 + 16px Logo 占位 + 名称 + 极细分割线
  // 能力维度：仅作为「模型标签」+ 顶部筛选，不再作为分组标题（避免打断阅读节奏）
  const CAP_ORDER=(G.caps_defs||[]).map(c=>c.key);
  const CAP_MAP={}; (G.caps_defs||[]).forEach(c=>CAP_MAP[c.key]=c);
  const ACCENT="#4f46e5";                      // 统一品牌蓝（Hover / 选中 / 筛选激活 / 能力标签 Hover）
  function eloSort(a,b){ return ((b.rating==null?-1:b.rating)-(a.rating==null?-1:a.rating)) || a.name.localeCompare(b.name); }
  const rows=[];
  G.regions.forEach(reg=>{
    const nComp=new Set(reg.models.map(m=>m.company)).size;
    rows.push({type:"h",region:reg.region,nModels:reg.models.length,nCompanies:nComp});
    const byComp={};
    reg.models.forEach(m=>{ (byComp[m.company]=byComp[m.company]||[]).push(m); });
    // 公司按「最强模型评分」降序
    const compNames=Object.keys(byComp).sort((a,b)=> bestRating(byComp[b]) - bestRating(byComp[a]));
    compNames.forEach(comp=>{
      const all=byComp[comp].slice().sort(eloSort);
      rows.push({type:"c",company:comp,color:(all[0]||{}).color||"#888",all:all,models:all,region:(all[0]||{}).region});
      // 模型直接挂在公司下（不再渲染能力标题）；仍按主能力聚类、组内按 Elo 降序，
      // 使同类模型相邻、阅读连续，能力仅作为模型标签出现。
      const byCap={};
      all.forEach(m=>{ const c=m.main_cap||(m.caps&&m.caps[0])||"Chat"; (byCap[c]=byCap[c]||[]).push(m); });
      CAP_ORDER.filter(c=>byCap[c]).forEach(cap=>{
        byCap[cap].slice().sort(eloSort).forEach(m=> rows.push({type:"m",m}));
      });
    });
  });
  let plotH=T; rows.forEach(r=> plotH += (r.type==="h"?headerH:(r.type==="c"?compH:rowH)));
  const H=plotH+B;
  svg.setAttribute("viewBox",`0 0 ${W} ${H}`);
  const full0=new Date(G.range[0]+"T00:00:00Z").getTime();
  const full1=new Date(G.range[1]+"T00:00:00Z").getTime();
  const DAY=86400000;
  const plotW=W-L-R;
  const minYear=new Date(full0).getUTCFullYear();
  const maxYear=new Date(full1).getUTCFullYear();
  const numYears=Math.max(1,maxYear-minYear+1);
  // 时间轴采用「内容加权列宽」：每个年份分组占用的像素宽度 ∝ 组内事件数 + 基准权重，
  // 稀疏分组（如 2022年前 仅 2 次）被压缩成细条，密集分组（如 2026 有数百次）获得更多空间。
  // 年份分组由 GANTT_BANDS 指定：2021 不单列；2020/2021/2022 合并为「2022年前」。
  const RATE_BASE=12;  // 每个分组最小内容权重，保证稀疏分组仍有可见细条与标签
  const yearCounts={};
  G.regions.forEach(reg=> reg.models.forEach(m=> m.events.forEach(e=>{
    const y=e.date.slice(0,4); yearCounts[y]=(yearCounts[y]||0)+1;
  })));
  // 构建实际生效的分组：仅保留与数据范围 [minYear,maxYear] 有交集的分组，并裁剪到数据范围
  const BANDS=GANTT_BANDS
    .filter(b=> b[1]<=maxYear && b[2]>=minYear)
    .map(b=>{ const y0=Math.max(b[1],minYear), y1=Math.min(b[2],maxYear); return {label:b[0], y0, y1}; })
    .sort((a,b)=>a.y0-b.y0);
  const RECENT_BOOST=2.6;   // 最新（最右）分组横向加权倍数：使其占满大部分轨道，事件点得以展开；后续更新越多占比越大
  const bandUnits={}; let totalUnits=0; const cumBeforeB={};
  BANDS.forEach((b,i)=>{
    cumBeforeB[b.label]=totalUnits;
    let cnt=0; for(let y=b.y0;y<=b.y1;y++) cnt += (yearCounts[String(y)]||0);
    let w=cnt+RATE_BASE;
    if(i===BANDS.length-1) w = cnt*RECENT_BOOST + RATE_BASE;   // 最新分组（如 2026）占满轨道，点分散展开
    bandUnits[b.label]=w; totalUnits+=w;
  });
  // 前置分组（如「2022年前」）标签较长，强制最小像素宽度，避免其标签与下一分组（如「2023」）标签重叠；
  // 多出的权重从最末（最新）分组扣除，整体占比几乎不变。
  const MIN_FIRST_BW=66;
  if(BANDS.length>1){
    const fLab=BANDS[0].label, lLab=BANDS[BANDS.length-1].label;
    const need=MIN_FIRST_BW/plotW*totalUnits;
    if(bandUnits[fLab]<need){
      const diff=need-bandUnits[fLab];
      bandUnits[fLab]=need;
      bandUnits[lLab]=Math.max(bandUnits[lLab]-diff, need);
      let acc=0; BANDS.forEach(b=>{ cumBeforeB[b.label]=acc; acc+=bandUnits[b.label]; }); totalUnits=acc;
    }
  }
  // 视图：viewStart=内容空间起点(单位)，viewUnits=可见内容跨度(单位)
  let viewStart=0, viewUnits=totalUnits;
  const minView=Math.max(2, totalUnits/12);
  function bandOf(y){ for(const b of BANDS){ if(y>=b.y0 && y<=b.y1) return b; } return BANDS[0]; }
  // 事件 → 内容空间坐标：落在其所在分组的 [组起点, 组终点) 区间内按比例定位
  const todayMs=new Date(G.range[1]+"T00:00:00Z").getTime();
  function posOf(ms){ const dt=new Date(ms); const y=dt.getUTCFullYear(); const b=bandOf(y);
    const bs=Date.UTC(b.y0,0,1), be=Math.min(Date.UTC(b.y1+1,0,1), todayMs);
    const frac=(ms-bs)/(be-bs); return cumBeforeB[b.label] + frac*bandUnits[b.label]; }
  function xOfUnits(u){ return L + (u-viewStart)/viewUnits*plotW; }
  function xAt(ms){ return xOfUnits(posOf(ms)); }
  const xAtDate=s=> xAt(new Date(s+"T00:00:00Z").getTime());
  function bandLabelAtUnits(u){ u=Math.max(0,Math.min(totalUnits,u));
    for(let i=BANDS.length-1;i>=0;i--){ if(u>=cumBeforeB[BANDS[i].label]) return BANDS[i].label; } return BANDS[0].label; }
  const kindColor={model:"#4f46e5",product:"#059669"};
  const BLUE="#4f46e5", GREEN="#059669", MAJOR="#ef4444";   // 蓝=大版本 绿=子版本 红=里程碑
  const MILESTONE_FAMS=new Set(["GPT","Gemini","Claude","Llama","DeepSeek 系列","Grok","Seedance"]);
  // 版本层级：从标题解析首个版本号，x.y（y≠0）视为子版本→绿，整数或 x.0 视为大版本→蓝/红
  function versionTier(title){ const m=String(title||"").match(/(\d+)(?:\.(\d+))?/); if(!m) return "major"; const minor=m[2]?parseInt(m[2],10):0; return minor!==0?"minor":"major"; }
  // 事件颜色分类（与图例一致）：green=子版本 / red=里程碑 / blue=大版本
  function eventColorKey(e,m){ const tier=versionTier(e.title);
    if(tier==="minor") return "green"; return MILESTONE_FAMS.has(m.name) ? "red" : "blue"; }
  const LEGEND_COLOR={blue:"#4f46e5",green:"#059669",red:"#ef4444"};
  let legendFilter=null;    // null=全部；"blue"/"green"/"red"=仅显示该图例对应事件与所在模型行
  let capFilter=null;       // null=全部；能力 key（如 "Chat"）= 仅显示具备该能力的模型
  const kindText={model:"模型发布",product:"产品更新"};
  const show={model:true,product:true};
  let markerMode="block";   // block（方块）| dot（圆点）| bar（竖条）
  let majorOnly=true;       // 常开：过滤次要模型微调（无切换按钮，时间线仅保留重要事件）
  const tip=document.getElementById("ganttTip");
  const rangeLabel=document.getElementById("ganttRange");
  const stickyYears=document.getElementById("ganttStickyYears");
  const stickyYearsSvg=document.getElementById("ganttYearsSvg");
  const RATE_MIN=1250, RATE_MAX=1580;   // 评分条色阶范围（LMArena Elo，每日自动刷新）
  function bestRating(arr){ let r=-1; arr.forEach(m=>{ if(m.rating!=null && m.rating>r) r=m.rating; }); return r; }

  function visibleEvents(c){
    return c.events.filter(e=> show[e.kind] && !(majorOnly && e.minor)
      && (!legendFilter || eventColorKey(e,c)===legendFilter)
      && (!capFilter || (c.caps||[]).includes(capFilter)));
  }

  function monthTicks(){
    const out=[];
    for(let y=minYear;y<=maxYear;y++){ for(let mo=1;mo<12;mo++){ const ms=Date.UTC(y,mo,1); if(ms>=full0&&ms<=full1) out.push(ms); } }
    return out;
  }
  function clampView(){
    if(viewUnits>totalUnits) viewUnits=totalUnits;
    if(viewUnits<minView) viewUnits=minView;
    if(viewStart<0) viewStart=0;
    if(viewStart+viewUnits>totalUnits) viewStart=totalUnits-viewUnits;
  }
  function render(){
    clampView();
    let h=""; const rowY={}; let y=T;
    // 图例筛选：预扫描出「在当前筛选下仍有事件」的模型 / 公司 / 阵营，用于隐藏空行
    const filtering=!!(legendFilter || capFilter);
    const modelHasVis={}, compHasVis={}, regHasVis={};
    if(filtering){
      rows.forEach(r=>{ if(r.type!=="m") return;
        const has=visibleEvents(r.m).length>0;
        modelHasVis[r.m.company+"|"+r.m.name]=has;
        if(has){ compHasVis[r.m.company]=true; regHasVis[r.m.region]=true; }
      });
    }
    // 1) 区域带 + 公司分组头 + 模型行（每公司下展开各自模型）
    //    国家板块视觉增强：阵营头=国家色实底白字条；左侧粗竖色条贯穿整个板块；圆点/文字右移避让竖条
    let overlay="";                 // 板块装饰层（竖色条），最后叠加到最上层确保可见
    let curCompany=null, compStartY=T, compColor="#888";
    const flushCompany=(endY)=>{
      if(curCompany && endY>compStartY+0.5){
        overlay+=`<rect x="0" y="${compStartY.toFixed(1)}" width="3" height="${(endY-compStartY).toFixed(1)}" fill="${compColor}" opacity="0.9"/>`;
        overlay+=`<line x1="0" y1="${endY.toFixed(1)}" x2="${W}" y2="${endY.toFixed(1)}" stroke="#eceef3" stroke-width="1"/>`;
      }
    };
    rows.forEach(r=>{
      if(r.type==="h"){
        flushCompany(y); curCompany=null;
        if(filtering && !regHasVis[r.region]) return;
        // 国家章节头：弱化显示（18px 近黑 + 浅灰副标题），无背景色块
        const lbl=REGION_LABEL[r.region]||r.region;
        h+=`<text x="14" y="${(y+22).toFixed(1)}" font-size="18" font-weight="700" fill="#374151">${escapeHtml(lbl)}</text>`;
        h+=`<text x="14" y="${(y+40).toFixed(1)}" font-size="11.5" font-weight="500" fill="#9aa1b1">${r.nModels} 个模型 · ${r.nCompanies} 家公司</text>`;
        h+=`<line x1="0" y1="${(y+headerH).toFixed(1)}" x2="${W}" y2="${(y+headerH).toFixed(1)}" stroke="#eceef3" stroke-width="1"/>`;
        y+=headerH;
      } else if(r.type==="c"){
        flushCompany(y);
        if(filtering && !compHasVis[r.company]){ curCompany=null; return; }
        curCompany=r.company; compStartY=y; compColor=r.color;
        const comp=r.company;
        // 公司视觉锚点：右侧 16px 虚线 Logo 占位（品牌色描边 + 首字母）+ 公司名（品牌色）
        const lx=W-34, ly=(y+compH/2-8).toFixed(1);
        h+=`<rect x="${lx}" y="${ly}" width="16" height="16" rx="5" fill="#ffffff" stroke="${r.color}" stroke-width="1.25" stroke-dasharray="2.5 2"/>`;
        h+=`<text x="${(lx+8).toFixed(1)}" y="${(y+compH/2+4).toFixed(1)}" text-anchor="middle" font-size="9.5" font-weight="800" fill="${r.color}">${escapeHtml(comp.slice(0,1))}</text>`;
        h+=`<text class="ccname" x="16" y="${(y+compH/2+5).toFixed(1)}" font-size="15" font-weight="800" fill="${r.color}">${escapeHtml(comp)}</text>`;
        y+=compH;
      } else {
        const m=r.m, y0=y;
        if(filtering && !modelHasVis[m.company+"|"+m.name]) return;
        h+=`<rect class="grow" style="--mc:${m.color}" x="0" y="${y0.toFixed(1)}" width="${W}" height="${rowH}" fill="#ffffff"/>`;
        const vis=visibleEvents(m);
        // 模型名（左）+ Arena Elo（同行靠右，品牌色；无评分显示「—」）
        h+=`<text x="16" y="${(y0+17).toFixed(1)}" font-size="13" font-weight="600" fill="#1f2430">${escapeHtml(m.name)}</text>`;
        h+=`<text x="${(L-12).toFixed(1)}" y="${(y0+17).toFixed(1)}" text-anchor="end" font-size="13" font-weight="800" fill="${m.color}">${m.rating==null?'—':m.rating}</text>`;
        // 能力标签：统一浅灰胶囊（第二行），Hover 变深灰；最多 2 个
        let _tx=16;
        (m.caps||["Chat"]).slice(0,2).forEach(cap=>{
          const cd=CAP_MAP[cap]||{label:cap};
          const _lab=cd.label;
          const _w=_lab.length*7.2+14;
          if(_tx+_w > L-14) return;
          h+=`<g class="gtag">`+
             `<rect x="${_tx.toFixed(1)}" y="${(y0+24).toFixed(1)}" width="${_w.toFixed(1)}" height="15" rx="7.5" fill="#f1f3f7"/>`+
             `<text x="${(_tx+_w/2).toFixed(1)}" y="${(y0+35).toFixed(1)}" text-anchor="middle" font-size="10" font-weight="600" fill="#6b7280">${escapeHtml(_lab)}</text>`+
             `</g>`;
          _tx += _w+6;
        });
        rowY[m.company+"|"+m.name]=y0;
        y+=rowH;
      }
    });
    flushCompany(y);
    const plotBottom=y;
    h+=overlay;
    // 左侧标签栏与绘图区分隔线（极淡）
    h+=`<line x1="${L.toFixed(1)}" y1="${T}" x2="${L.toFixed(1)}" y2="${plotBottom.toFixed(1)}" stroke="#e9ebf1" stroke-width="1"/>`;

    // 2) 时间轴：内容加权列宽（稀疏年细、密集年宽）+ 顶部年份标签
    h+=`<rect x="0" y="0" width="${W}" height="${T}" fill="#ffffff"/>`;
    h+=`<line x1="0" y1="${T}" x2="${W}" y2="${T}" stroke="#e4e7ef" stroke-width="1"/>`;
    BANDS.forEach((b,i)=>{
      const x0=xOfUnits(cumBeforeB[b.label]);            // 该分组列左边界
      const x1=xOfUnits(cumBeforeB[b.label]+bandUnits[b.label]); // 右边界
      if(x1<L-1 || x0>W-R+1) return;                      // 完全在视野外
      if(x0>=L-1) h+=`<line x1="${x0.toFixed(1)}" y1="${T}" x2="${x0.toFixed(1)}" y2="${plotBottom.toFixed(1)}" stroke="#e3e6ee"/>`;
      const wpx=x1-x0;
      if(wpx>10){                                  // 列足够宽才显示标签，避免拥挤
        const fs = 11;                             // 所有年份标签统一字号（含「2022年前」），与后续年份一致
        const anchor = (i===0) ? "start" : "middle";   // 前置长标签左对齐，避免越界压到下一分组
        const xc = (i===0) ? (x0+3) : (x0+x1)/2;
        h+=`<text x="${xc.toFixed(1)}" y="${(T-4).toFixed(1)}" text-anchor="${anchor}" font-size="${fs}" font-weight="800" fill="#6b7280">${b.label}</text>`;
      }
    });
    // 月份细线：仅在放大到足够窄（≤约半个内容跨度，≈3 年）时显示，避免拥挤
    if(viewUnits<=totalUnits*0.5){
      monthTicks().forEach(ms=>{ const x=xAt(ms); if(x<L-0.5||x>W-R+0.5) return;
        h+=`<line x1="${x.toFixed(1)}" y1="${T}" x2="${x.toFixed(1)}" y2="${plotBottom.toFixed(1)}" stroke="#f3f5f9"/>`;
      });
    }
    // 季度淡分隔线（仅深度放大时显示，辅助时间定位）
    if(viewUnits<=totalUnits*0.32){
      for(let y=minYear;y<=maxYear;y++){ for(let q=1;q<=4;q++){
        const ms=Date.UTC(y,(q-1)*3,1); if(ms<full0||ms>full1) continue;
        const x=xAt(ms); if(x<L-0.5||x>W-R+0.5) continue;
        h+=`<line x1="${x.toFixed(1)}" y1="${T}" x2="${x.toFixed(1)}" y2="${plotBottom.toFixed(1)}" stroke="#e9edf4"/>`;
      }}
    }
    // “今天”参考线（橙色虚线 + 顶部标签），与右边界（统计截止日）同源，作为时间锚点
    {
      const tx=xAtDate(G.range[1]);
      if(tx>=L-0.5 && tx<=W-R+0.5){
        const txc=Math.max(L+18, Math.min(tx, W-R-18));   // 贴边时胶囊左移，避免压到评分栏
        h+=`<line x1="${tx.toFixed(1)}" y1="${T}" x2="${tx.toFixed(1)}" y2="${plotBottom.toFixed(1)}" stroke="#f97316" stroke-width="1.5" stroke-dasharray="4 3" opacity="0.85"/>`;
        h+=`<polygon points="${tx.toFixed(1)},${T} ${(tx-4).toFixed(1)},${(T-6).toFixed(1)} ${(tx+4).toFixed(1)},${(T-6).toFixed(1)}" fill="#f97316" opacity="0.9"/>`;
        h+=`<rect x="${(txc-16).toFixed(1)}" y="2" width="32" height="14" rx="7" fill="#f97316" opacity="0.95"/>`;
        h+=`<text class="gtoday" x="${txc.toFixed(1)}" y="13" text-anchor="middle">今天</text>`;
      }
    }
    // 3) 事件标记：每个点按真实发布日期（年/月/日）在时间轴上定位，突出发布时间先后
    rows.forEach(r=>{ if(r.type!=="m") return;
      const m=r.m, y0=rowY[m.company+"|"+m.name], cy=y0+rowH/2;
      const mH = markerMode==="dot" ? 0 : (markerMode==="bar" ? Math.round(rowH*0.6) : 15);
      const vis=visibleEvents(m).slice().sort((a,b)=> a.date<b.date?-1:(a.date>b.date?1:0));
      if(!vis.length) return;
      // 模型活跃跨度条：贯穿「最早→最晚」事件的淡色圆角带，强化甘特图的连续区间语义
      if(vis.length>1){
        const xs=xAtDate(vis[0].date), xe=xAtDate(vis[vis.length-1].date);
        const sx=Math.max(L,Math.min(xs,xe)), ex=Math.min(W-R,Math.max(xs,xe));
        const bw=ex-sx;
        if(bw>0.5) h+=`<rect x="${sx.toFixed(1)}" y="${(y0+rowH/2-5).toFixed(1)}" width="${bw.toFixed(1)}" height="10" rx="5" fill="${m.color}" opacity="0.12"/>`;
      }
      vis.forEach(e=>{
        let x = xAt(new Date(e.date+"T00:00:00Z").getTime());
        if(x<L-8 || x>W-R+8) return;               // 视野外跳过
        const tier = versionTier(e.title);
        const col = tier==="minor" ? GREEN : (MILESTONE_FAMS.has(m.name) ? MAJOR : BLUE);
        const j=JSON.stringify({t:e.title,d:e.date,k:e.kind,s:e.source,f:e.file,
          comp:m.company, mcap:m.main_cap, melo:(m.rating==null?'—':m.rating), mreg:m.region, mcol:m.color, major:!!e.major})
          .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
        if(markerMode==="dot"){
          h+=`<g class="gev" data-j="${j}" style="cursor:pointer">`+
             `<circle cx="${x.toFixed(1)}" cy="${cy.toFixed(1)}" r="4.5" fill="${col}"/>`+
             `<circle cx="${x.toFixed(1)}" cy="${cy.toFixed(1)}" r="8.5" fill="transparent"/>`+
             `</g>`;
        } else if(markerMode==="bar"){
          const ry=(cy-mH/2).toFixed(1);
          h+=`<g class="gev" data-j="${j}" style="cursor:pointer">`+
             `<rect x="${(x-1.5).toFixed(1)}" y="${ry}" width="3" height="${mH}" rx="1.5" fill="${col}"/>`+
             `<rect x="${(x-5).toFixed(1)}" y="${(cy-mH/2-4).toFixed(1)}" width="10" height="${mH+8}" rx="4" fill="transparent"/>`+
             `</g>`;
        } else {
          const rx=(x-5.5).toFixed(1), ry=(cy-mH/2).toFixed(1);
          h+=`<g class="gev" data-j="${j}" style="cursor:pointer">`+
             `<rect x="${rx}" y="${ry}" width="11" height="${mH}" rx="3.5" fill="${col}"/>`+
             `<rect x="${(x-10.5).toFixed(1)}" y="${(cy-mH/2-5).toFixed(1)}" width="21" height="${mH+10}" rx="6" fill="transparent"/>`+
             `</g>`;
        }
      });
    });
    // 筛选时行数变化，按实际内容高度更新画布，避免底部留白
    svg.setAttribute("viewBox",`0 0 ${W} ${(plotBottom+B).toFixed(1)}`);
    // 整体外框：圆角矩形包住「年份轴 + 全部国家板块 + 右侧评分栏」，作为统一视觉边界（最后绘制，确保压在内容边缘之上）
    const fH=plotBottom+B;
    h+=`<rect x="0.75" y="0.75" width="${(W-1.5).toFixed(1)}" height="${(fH-1.5).toFixed(1)}" rx="10" fill="none" stroke="#c2cadb" stroke-width="1.5"/>`;
    svg.innerHTML=h;
    // 同步渲染 sticky 年份浮层（与主 SVG 年份标签完全一致，pan/zoom 时跟随更新）
    if(stickyYearsSvg){
      let yh=`<rect x="0" y="0" width="${W}" height="${T}" fill="#ffffff"/>`;
      yh+=`<line x1="0" y1="${T}" x2="${W}" y2="${T}" stroke="#e4e7ef" stroke-width="1"/>`;
      BANDS.forEach((b,i)=>{
        const x0=xOfUnits(cumBeforeB[b.label]);
        const x1=xOfUnits(cumBeforeB[b.label]+bandUnits[b.label]);
        if(x1<L-1 || x0>W-R+1) return;
        const wpx=x1-x0;
        if(wpx>10){
          const fs=11;
          const anchor=(i===0)?"start":"middle";
          const xc=(i===0)?(x0+3):(x0+x1)/2;
          yh+=`<text x="${xc.toFixed(1)}" y="${(T-4).toFixed(1)}" text-anchor="${anchor}" font-size="${fs}" font-weight="800" fill="#6b7280">${b.label}</text>`;
        }
      });
      stickyYearsSvg.setAttribute("viewBox",`0 0 ${W} ${T}`);
      stickyYearsSvg.innerHTML=yh;
    }
    if(rangeLabel) rangeLabel.textContent=`可见 ${bandLabelAtUnits(viewStart)} – ${bandLabelAtUnits(viewStart+viewUnits)} · 约 ${(viewUnits/totalUnits*numYears).toFixed(1)} 年（按内容量分配列宽）`;
    svg.querySelectorAll(".gev").forEach(g=>{
      const j=JSON.parse(g.getAttribute("data-j"));
      g.addEventListener("mouseenter",()=>{
        if(dragging) return;
        const capLbl=(G.caps_defs||[]).find(c=>c.key===j.mcap);
        const capTxt = capLbl ? (capLbl.emoji+" "+capLbl.label) : (j.mcap||"—");
        const regLbl = REGION_LABEL[j.mreg]||j.mreg||"—";
        const kindLbl = (kindText[j.k]||j.k) + (j.major?' · 🔴重大更新':'');
        tip.innerHTML=
          `<div style="font-weight:700;margin-bottom:3px">${escapeHtml(j.t)}</div>`+
          `<div style="opacity:.82">${kindLbl} · 📅 ${j.d}</div>`+
          `<div style="opacity:.82;margin-top:2px">来源：${escapeHtml(j.s)}</div>`+
          `<div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:4px 12px;font-size:11.5px">`+
            `<span>🌐 ${escapeHtml(regLbl)}</span>`+
            `<span style="color:${j.mcol};font-weight:700">🏢 ${escapeHtml(j.comp)}</span>`+
            `<span>🎯 ${escapeHtml(capTxt)}</span>`+
            `<span style="color:${j.mcol};font-weight:700">📊 Arena Elo ${j.melo==='—'?'—':j.melo}</span>`+
          `</div>`+
          `<div style="margin-top:5px;color:#9aa1b1">点击查看当日日报 →</div>`;
        tip.style.display="block";
      });
      g.addEventListener("mousemove",ev=>{
        if(dragging) return;
        const rect=svg.getBoundingClientRect();
        let tx=ev.clientX-rect.left+14, ty=ev.clientY-rect.top+14;
        const tw=tip.offsetWidth, th=tip.offsetHeight;
        if(tx+tw>rect.width) tx=ev.clientX-rect.left-tw-14;
        if(ty+th>rect.height) ty=ev.clientY-rect.top-th-14;
        tip.style.left=tx+"px"; tip.style.top=ty+"px";
      });
      g.addEventListener("mouseleave",()=>{tip.style.display="none";});
      g.addEventListener("click",()=>{ if(moved) return; if(j.f) window.open(j.f,"_blank","noopener"); });
    });
  }

  // 缩放（滚轮，围绕光标位置，保持锚点不动）
  svg.addEventListener("wheel",e=>{
    const rect=svg.getBoundingClientRect();
    const vx=(e.clientX-rect.left)/rect.width*W;
    if(vx<L||vx>W-R) return;
    e.preventDefault();
    const anchorU = viewStart + (vx-L)/plotW*viewUnits;   // 光标处内容坐标
    const factor=e.deltaY>0 ? 1.25 : 1/1.25;
    viewUnits=Math.max(minView, Math.min(totalUnits, viewUnits*factor));
    viewStart = anchorU - (vx-L)/plotW*viewUnits;          // 保持锚点不动
    clampView();
    render();
  },{passive:false});

  // 拖动（平移时间轴）
  let dragging=false, lastX=0, moved=false;
  svg.addEventListener("mousedown",e=>{ dragging=true; moved=false; lastX=e.clientX; tip.style.display="none"; });
  window.addEventListener("mouseup",()=>{ dragging=false; });
  svg.addEventListener("mousemove",e=>{
    if(!dragging) return;
    const rect=svg.getBoundingClientRect();
    const dxPx=e.clientX-lastX; lastX=e.clientX;
    if(Math.abs(dxPx)>2) moved=true;
    const dxUnits = dxPx*(rect.width/W)/plotW*viewUnits;
    viewStart -= dxUnits; clampView(); render();
  });
  svg.addEventListener("mouseleave",()=>{ if(!dragging) tip.style.display="none"; });

  // 年份浮层：滚动时主 SVG 年份标签消失 → 浮层自动出现；滚回顶部或离开图表 → 自动隐藏
  function updateStickyYears(){
    if(!stickyYears) return;
    const rect=svg.getBoundingClientRect();
    const Tpx=T*(rect.width/W);       // 年份标签高度（像素）
    if(rect.top<-Tpx && rect.bottom>Tpx+30){
      stickyYears.style.display="block";
    } else {
      stickyYears.style.display="none";
    }
  }
  window.addEventListener("scroll",updateStickyYears,{passive:true});
  window.addEventListener("resize",updateStickyYears);

  // 筛选（模型/产品）
  document.querySelectorAll(".gbtn[data-kind]").forEach(b=>{
    b.addEventListener("click",()=>{ const k=b.getAttribute("data-kind");
      show[k]=!show[k]; b.classList.toggle("active",show[k]); render(); });
  });
  // 标记样式切换（方块 / 圆点 / 细线）
  document.querySelectorAll(".gbtn[data-mode]").forEach(b=>{
    b.addEventListener("click",()=>{
      markerMode=b.getAttribute("data-mode");
      document.querySelectorAll(".gbtn[data-mode]").forEach(x=>x.classList.remove("active"));
      b.classList.add("active"); render();
    });
  });
  // 图例点击筛选：点亮某图例 → 甘特图仅显示该层级事件及其所在模型行；再点取消
  const legendEls=document.querySelectorAll(".glegend[data-legend]");
  function syncLegend(){
    legendEls.forEach(el=>{ const k=el.getAttribute("data-legend");
      el.classList.toggle("active", legendFilter===k);
      el.classList.toggle("dim", legendFilter!==null && legendFilter!==k);
    });
  }
  legendEls.forEach(el=>{
    el.addEventListener("click",()=>{
      const k=el.getAttribute("data-legend");
      legendFilter = (legendFilter===k) ? null : k;
      syncLegend(); render();
    });
  });
  // 能力筛选：顶部按钮（全部 / 各能力），点击仅显示具备该能力的模型
  const capWrap=document.getElementById("capFilters");
  let capAllBtn=null;
  if(capWrap){
    capAllBtn=document.createElement("button");
    capAllBtn.className="gcap active"; capAllBtn.dataset.cap=""; capAllBtn.textContent="全部";
    capWrap.appendChild(capAllBtn);
    (G.caps_defs||[]).forEach(c=>{
      const b=document.createElement("button");
      b.className="gcap"; b.dataset.cap=c.key; b.textContent=c.label;
      capWrap.appendChild(b);
    });
    capWrap.querySelectorAll(".gcap").forEach(b=>{
      b.addEventListener("click",()=>{
        capFilter = b.dataset.cap || null;
        capWrap.querySelectorAll(".gcap").forEach(x=>x.classList.remove("active"));
        b.classList.add("active");
        render();
      });
    });
  }
  function syncCapFilter(){ if(capWrap) capWrap.querySelectorAll(".gcap").forEach(x=>x.classList.toggle("active", (x.dataset.cap||"")===(capFilter||""))); }
  // 重置视图（同时清除图例与能力筛选）
  const rb=document.getElementById("ganttReset");
  if(rb) rb.addEventListener("click",()=>{ viewStart=0; viewUnits=totalUnits; legendFilter=null; capFilter=null; syncLegend(); syncCapFilter(); render(); });
  render();
  updateStickyYears();
})();
</script>
</body></html>
"""

def render_day(day):
    meta = day["meta"]
    data = {"meta": meta, "sections": day["sections"]}
    return (DAY_TPL
        .replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("<","\\u003c").replace(">","\\u003e"))
        .replace("__REPORTDATE__", meta["reportDate"])
        .replace("__REPORTDATEHUMAN__", meta["reportDateHuman"])
        .replace("__WEEKDAY__", meta["weekday"])
        .replace("__SOURCE__", meta["source"])
        .replace("__SOURCEURL__", meta["sourceUrl"])
        .replace("__TOTAL__", str(meta["total"]))
        .replace("__GENERATEDAT__", meta["generatedAt"]))

# 甘特图安全过滤：命中以下明显「非发布」关键词的标题直接剔除（不进时间线）
_GANT_SKIP_KW = ["融资", "收购", "并购", "财报", "上市", "诉讼", "监管", "处罚",
                 "招聘", "离职", "人事变动", "获奖", "榜单", "排名", "论坛", "大会", "协会"]

# 「纯模型发布 / 版本更新」严格判定：daily-feed 自动抽取时只接纳真正的模型发布，
# 排除「模型登陆/上架某平台」「发布技术报告/论文」「合作/评测」等非发布类头条。
# 仅在 AI HOT 日报「模型发布/更新」版块内使用，进一步收紧以杜绝污染。
_MODEL_RELEASE_ACT = ["发布", "推出", "开源", "上线", "首发", "正式可用", "ga", "release"]
_MODEL_RELEASE_HARD = [
    "登录", "登陆", "上架", "接入", "落地", "技术报告", "研究报告", "论文",
    "白皮书", "合作", "联合", "评测", "基准", "benchmark", "榜单", "排名",
    "融资", "收购", "获奖", "大会", "论坛", "开源周", "直播", "教程",
]
_MODEL_VERSION_RE = re.compile(r"(?:v|V)?\d+\.\d+(?:\.\d+)?|\d+\.\d+\s*(?:版本|版)|"
                               r"(?:gpt|claude|gemini|llama|grok|glm|kimi|qwen|ernie|mixtral|mistral|nova|titan|abab|baichuan|spark|deepseek)[- ]?\d", re.I)
_MODEL_TYPE_KW = ["大模型", "模型", "moE", "moe", "基座", "多模态", "推理模型", "语言模型",
                  "开源模型", "视频模型", "图像模型", "声音模型", "语音模型", "文生", "图生"]
# 标题级黑名单：含以下短语者即便命中“模型/发布”也非「模型发布/版本更新」（产品 App / 智能体 / 博文等）
_MODEL_RELEASE_BLACKLIST = ["chatgpt work", "最强模型与最佳博文", "最佳博文",
                            "app 上线", "app上线", "智能体 app", "agent app"]
def is_pure_model_release(title):
    t = title or ""
    tl = t.lower()
    if any(k in tl for k in _MODEL_RELEASE_BLACKLIST):
        return False
    if any(k in t for k in _GANT_SKIP_KW):      # 融资/收购/榜单…
        return False
    if any(k in tl for k in _MODEL_RELEASE_HARD):
        return False                            # 登陆平台/技术报告/论文…
    if not any(a in tl for a in _MODEL_RELEASE_ACT):
        return False                            # 必须含「发布/推出/开源…」动作
    has_ver = bool(_MODEL_VERSION_RE.search(tl))
    has_type = any(k in tl for k in _MODEL_TYPE_KW)
    comp_hit = any(k in tl for _, _, kws, _ in COMPANIES for k in kws)
    return has_ver or has_type or comp_hit


def compute_gantt(arch=None, top_n=GANTT_TOP_N):
    """甘特式时间线：纯「模型发布 / 版本更新」视角。
    数据来源 = 人工核实的 MILESTONES（2020–2024 历史基线，仅取 k=="model"）+
               AI HOT 每日日报「模型发布/更新」版块（2025 起自动抽取，is_pure_model_release 严格过滤）。
    两者合并去重，按 (公司 → 模型) 逐模型分行；命中「重大版本/发布」高亮（major=True，前端红色块）。
    不含融资/合作/技术报告/模型登陆平台/产品 App 等非发布类事件。
    返回 {range:[最早,最晚], regions:[{region,label,tint,tag, models:[{company,name,color,events}]}]}，
    regions 按阵营分块（美国在上、中国居中、🇫🇷法国置底）；区域内按 公司→事件数 排序；
    models 即每行一个模型。events: {date, kind(model/product), title, source, file, minor, major}"""
    groups = {}      # key=(company,family) -> {company,name,color,region,events:[]}
    seen = set()     # (company, date, title) 去重
    def _add(comp, ccolor, cregion, fam, date, kind, title, source, file_, minor, major):
        key = (comp, fam)
        g = groups.get(key)
        if not g:
            g = {"company": comp, "name": fam, "color": ccolor,
                 "region": cregion, "events": [], "rating": resolve_rating(fam),
                 "mtype": FAM_TYPE.get(fam, "文本"),
                 "caps": _caps_of(fam), "main_cap": _caps_of(fam)[0]}
            groups[key] = g
        g["events"].append({
            "date": date, "kind": kind, "title": title,
            "source": source, "file": file_, "minor": minor, "major": bool(major),
        })
    # ── 来源一：人工核实的历史里程碑（仅「模型发布」类型，剔除产品 App）──
    for mst in MILESTONES:
        if mst.get("k") != "model":
            continue
        comp = mst["c"]
        if comp not in COMP_MAP:
            continue
        ccolor, cregion = COMP_MAP[comp]
        fam = FAMILY.get(mst["m"], mst["m"])
        _add(comp, ccolor, cregion, fam, mst["d"], mst["k"], mst["t"],
             mst.get("src", "历史资料"), "", False, bool(mst.get("major")))
    # ── 来源二：每日日报「模型发布/更新」版块（自动更新，严格过滤）──
    arch = arch or load_archive()
    for d in sorted(arch.keys()):
        rec = arch[d]
        for sec in rec.get("sections", []):
            if sec.get("label") != "模型发布/更新":
                continue
            for it in sec.get("items", []):
                title = (it.get("title") or "").strip()
                if not is_pure_model_release(title):
                    continue
                text = (title + " " + (it.get("summary") or "")).lower()
                # 公司识别
                comp = None
                for name, _, kws, _ in COMPANIES:
                    if any(k in text for k in kws):
                        comp = name
                        break
                if not comp:
                    continue
                ccolor, cregion = COMP_MAP[comp]
                # 具体模型识别（命中某系列则归族，否则归入公司名系列）
                model = comp
                for mname, mcomp, mkws in MODELS:
                    if mcomp == comp and any(k in text for k in mkws):
                        model = mname
                        break
                fam = FAMILY.get(model, model)
                sig = (comp, d, title)
                if sig in seen:
                    continue
                seen.add(sig)
                _add(comp, ccolor, cregion, fam, d, "model", title,
                     it.get("source") or "AI HOT", f"ai-daily-{d}.html",
                     is_minor_model(title), is_major_model(title))
    regions = []
    _REGION_META = {
        "us": ("🇺🇸 美国公司", "#f3f5ff", "#4f46e5"),
        "eu": ("🇫🇷 法国公司", "#f0fff4", "#059669"),
        "cn": ("🇨🇳 中国公司", "#fff5f6", "#e11d48"),
    }
    for region in ("us", "cn", "eu"):
        models = [g for g in groups.values() if g["region"] == region]
        models.sort(key=lambda m: (m["company"], -len(m["events"])))
        if not models:
            continue
        label, tint, tag = _REGION_META.get(region, (region, "#f6f7fb", "#6b7280"))
        regions.append({
            "region": region,
            "label": label,
            "tint": tint,
            "tag": tag,
            "models": models,
        })
    caps_defs = [{"key": k, "emoji": e, "label": l, "color": c} for (k, e, l, c) in CAP_DEFS]
    # 时间线范围：左=最早里程碑/日报，右=当天日期（每天更新自动延伸到今天，作为坐标最右端）
    mdates = [m["d"] for m in MILESTONES if m.get("k") == "model" and m["c"] in COMP_MAP]
    alld = sorted(set(mdates + list(arch.keys())))
    return {"range": [alld[0], max(alld[-1], today)], "regions": regions, "caps_defs": caps_defs}

def render_index(days):
    idx_days = []
    for d in days:
        idx_days.append({
            "file": f"ai-daily-{d['meta']['reportDate']}.html",
            "date": d["meta"]["reportDate"],
            "meta": d["meta"],
            "sections": [{"label": s["label"], "count": len(s["items"])} for s in d["sections"]],
            "lead": d.get("lead", "") or fallback_lead(d.get("sections", [])),
            "isToday": d["meta"]["reportDate"] == today,
        })
    newest = days[0]["meta"]["reportDateHuman"]
    oldest = days[-1]["meta"]["reportDateHuman"]
    arch_like = {d["meta"]["reportDate"]: d for d in days}
    gantt = compute_gantt(arch_like)
    # 按 年→月 分组（倒序：最新年/月在上），每月下列出各日期（倒序）
    by_year = {}
    for d in idx_days:
        y = d["meta"]["reportDate"][:4]
        m = int(d["meta"]["reportDate"][5:7])
        by_year.setdefault(y, {}).setdefault(m, []).append(d)
    groups = []
    for y in sorted(by_year.keys(), reverse=True):
        months = [{"month": m, "days": by_year[y][m]} for m in sorted(by_year[y].keys(), reverse=True)]
        groups.append({"year": y, "months": months})
    return (INDEX_TPL
        .replace("__DAYS__", json.dumps(idx_days, ensure_ascii=False).replace("<","\\u003c").replace(">","\\u003e"))
        .replace("__GROUPS__", json.dumps(groups, ensure_ascii=False).replace("<","\\u003c").replace(">","\\u003e"))
        .replace("__GANTT__", json.dumps(gantt, ensure_ascii=False).replace("<","\\u003c").replace(">","\\u003e"))
        .replace("__GANTT_BANDS__", json.dumps(GANTT_YEAR_BANDS, ensure_ascii=False))
        .replace("__RANGE__", f"{oldest} – {newest}")
        .replace("__NDAYS__", str(len(days)))
        .replace("__SOURCE__", "AI HOT")
        .replace("__SOURCEURL__", "https://aihot.virxact.com")
        .replace("__GENERATEDAT__", beijing_now().strftime("%Y年%m月%d日 %H:%M")))

print("[4] 写出全部日报文件（从本地 archive 重渲染，模板变更即时生效）...")
written = 0
for date, rec in arch.items():
    path = os.path.join(OUT_DIR, f"ai-daily-{date}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_day(rec))
    written += 1
print(f"    写入 {written} 个日报文件")

# 索引页：全部日期倒序
days_sorted = [arch[d] for d in sorted(arch.keys(), reverse=True)]
index_path = os.path.join(OUT_DIR, "index.html")
with open(index_path, "w", encoding="utf-8") as f:
    f.write(render_index(days_sorted))
print(f"    {index_path}（共 {len(days_sorted)} 期）")

# 旧入口 ai-daily.html 重定向到索引页（兼容已有书签）
redirect = ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
    '<meta http-equiv="refresh" content="0;url=index.html">'
    '<title>AI资讯杂志</title></head><body style="font-family:sans-serif;text-align:center;margin-top:48px">'
    '正在跳转到 <a href="index.html">AI资讯杂志</a>…</body></html>')
with open(os.path.join(OUT_DIR, "ai-daily.html"), "w", encoding="utf-8") as f:
    f.write(redirect)
print(f"    {os.path.join(OUT_DIR, 'ai-daily.html')} (重定向到 index.html)")
print(f"[完成] 累计 {len(arch)} 期日报全部保留 · 本次新增 {new_added} 期 · 索引页 + 重定向入口已更新")

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
deepclean_trafilatura.py — 对「AI HOT 无正文、仍用旧 trafilatura」的条目做一轮更强的 chrome 清理。

背景：
  refetch_aihot.py 已对历史正文做「优先 AI HOT 已清洗正文」回填：531 条检查中 60 条被 AI HOT
  干净正文替换、148 条保留已干净译文、323 条因 AI HOT 无正文而沿用旧 trafilatura 抓取内容。
  trafilatura 直抓源站的内容虽经 generate_archive.reformat_content 清理，仍可能残留：
    (1) 站点页脚/导航墙（如 OpenAI 博客 "Skip to main content Research Products Business
        Developers Company Foundation (opens in a new window) Log in Try ChatGPT … OpenAI ©
        2015–2026 Your privacy choices …" 整块被 trafilatura 带进正文尾部）；
    (2) 图片版权/来源署名行（"Image Credits:Google (screenshot)"、"图源：" 等）；
    (3) 孤立的广告/赞助标签行（"Advertisement"、"Sponsored" 单独成行）；
    (4) 残留的 Markdown 图片行（"![](url)"、"[]()"），渲染时会变成字面噪声。
  本脚本针对这四类「reformat_content 漏网的残噪」做精准、保守的二次清理。

识别策略（精确对齐「那 323 条」）：
  对每个条目取 item id（permalink 或按标题匹配 AI HOT 日报），重新拉取 AI HOT 正文；
  若 AI HOT 仍无正文(body 为空) → 属「那 323 条」，对其已存 trafilatura 正文应用 deep_clean_extra；
  若 AI HOT 已有正文 → 不属该集合，跳过（保持其当前内容，不做无关改动）。
  （注：若距上次 refetch 后 AI HOT 为某条目新增了正文，该条目便不再属「无正文」集合，自然被排除。）

安全护栏：
  - 所有规则仅删除高置信 chrome；删除后若正文长度 < 原长 50% 且原长 > 200，则回退保留原文（防误伤）。
  - 写盘前自动备份 archive.json -> archive.json.bak。
  - 支持 --dry-run 只统计将改变多少条、不写盘。

用法：
  python deepclean_trafilatura.py --dry-run     # 只统计，不写盘
  python deepclean_trafilatura.py               # 清理并写回
"""
import sys, os, re, json, ssl, argparse, datetime, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ARCH_PATH = os.path.join(HERE, "archive.json")
BASE = "https://aihot.virxact.com/api/public"

_AIOHOT_CHALLENGE_CK = "__tst_status=3086345129#; EO_Bot_Ssid=1406074880;"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_BODY_TAG = re.compile(r"<(?:li|p|h[1-6]|ul|ol|blockquote|pre|table|img|code|strong|em|a|br|div|span)[ >]", re.I)


# ───────────────────────── AI HOT 正文抓取（与 refetch_aihot.py 同源，独立自包含）──
def traolid(p):
    if not p:
        return None
    m = re.search(r"/items/([^/?#]+)", p)
    return m.group(1) if m else p


def fetch_aihot_body(pid, timeout=25):
    if not pid or not re.match(r"^[A-Za-z0-9_-]+$", pid):
        return None
    url = f"https://aihot.virxact.com/items/{pid}"
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA, "Cookie": _AIOHOT_CHALLENGE_CK,
        "Accept-Language": "zh-CN,zh;q=0.9"})
    try:
        raw = urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX).read().decode("utf-8", "ignore")
    except Exception:
        return None
    if "EO_Bot_Ssid" in raw and "__next_f" not in raw:
        return None
    chunks = re.findall(r"self\.__next_f\.push\(([\s\S]*?)\)\s*</script>", raw)
    strs = []
    for c in chunks:
        try:
            arr = json.loads(c)
        except Exception:
            m = re.match(r'\[\s*\d+\s*,\s*("(?:[^"\\]|\\.)*")', c)
            if not m:
                continue
            arr = [1, json.loads(m.group(1))]
        big = arr[1] if (len(arr) > 1 and isinstance(arr[1], str)) else ""
        strs += re.findall(r'"((?:[^"\\]|\\.){15,})"', big)
    parts, seen = [], set()
    for s in strs:
        if _BODY_TAG.search(s) and len(s) > 30:
            if s not in seen:
                seen.add(s); parts.append(s)
    if not parts:
        return None
    h = "\n".join(parts)
    h = re.sub(r"<h([1-6])[^>]*>", "\n\n", h, flags=re.I)
    h = re.sub(r"</h[1-6]>", "\n", h, flags=re.I)
    h = re.sub(r"<li[^>]*>", "\n- ", h, flags=re.I)
    h = re.sub(r"</li>", "", h, flags=re.I)
    h = re.sub(r"<(p|div|blockquote)[^>]*>", "\n", h, flags=re.I)
    h = re.sub(r"</(p|div|blockquote)>", "\n", h, flags=re.I)
    h = re.sub(r"<br\s*/?>", "\n", h, flags=re.I)
    h = re.sub(r"<code[^>]*>", "`", h, flags=re.I)
    h = re.sub(r"</code>", "`", h, flags=re.I)
    h = re.sub(r"<[^>]+>", "", h)
    h = re.sub(r"[ \t]+\n", "\n", h)
    h = re.sub(r"\n{3,}", "\n\n", h)
    return h.strip() or None


def http_get_json(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def build_title_pid_map(date):
    try:
        daily = http_get_json(f"{BASE}/daily/{date}")
    except Exception:
        return {}
    m = {}
    for sec in daily.get("sections", []):
        for it in sec.get("items", []):
            t = (it.get("title") or "").strip()
            pid = traolid(it.get("permalink"))
            if t and pid:
                m[t] = pid
    return m


# ───────────────────────── 更强清洗规则（针对 reformat_content 漏网的残噪）─────────
# 站点页脚/导航强信号（trafilatura 直抓源站残留的站点 chrome）
_NAV_FOOTER_START = re.compile(
    r'(?i)'
    r'(?:'
    r'\bOpenAI\s+Skip to main content|\bAnthropic\s+Skip to main content'  # 站点名+跳转
    r'|Your privacy choices'                              # 隐私选择（footer）
    r'|OpenAI ©|Anthropic ©|Google ©|Microsoft ©|Meta ©' # 版权 footer
    r'|©\s*\d{4}\s*(?:–|-)\s*\d{4}\s*(?:OpenAI|Anthropic|Google|Microsoft|Meta)'
    r'|Terms of Use Privacy Policy'                       # footer 法律链接
    r'|Company\s+About Us\s+Our Charter'                  # footer 公司栏
    r'|Contact Sales\s*(?:\(opens in a new window\))?\s*(?:Developers|Apps|SDK|Open Models|Docs|Company)'
    r'|Skip to main content'                             # 通用跳转链接
    r')'
)
_NAV_TOKENS = re.compile(
    r'(?i)(?:research|products|business|developers|company|foundation|log ?in|'
    r'try chatgpt|open models|docs|resources|news|support|help center|careers|'
    r'about us|our charter|privacy|terms|policy|forum|podcast|rss|academy|'
    r'opens in a new window)')
# 图片版权 / 来源署名行（独立成行、短、无完整句子）
_IMG_CREDIT = re.compile(
    r'(?i)^\s*(?:image credits?|image credit|image via|photo by|photograph:|'
    r'screenshot:?|图源[:：]|图片来源[:：]|图[:：]|配图[:：]|via[:：])\b')
# 孤立广告 / 赞助标签行
_AD_LABEL = re.compile(r'(?i)^\s*(?:advertisement|advert|ad|sponsored|promoted|advertorial)\b\s*(?:continue reading)?\s*$')
# 残留 Markdown 图片行 / 空链接行
_MD_IMG = re.compile(r'^\s*!\[[^\]]*\]\([^)]*\)\s*$')
_MD_EMPTY_LINK = re.compile(r'^\s*\[\]\([^)]*\)\s*$')
_WS = re.compile(r'[\u00a0\u200b\u2060]')


def _strip_site_nav_chrome(s):
    """剥离站点页脚 footer 与开头站点导航墙（如 OpenAI 博客「标题 | OpenAI Skip to main
    content <nav> (opens in a new window)」头部 + 尾部整块 footer）。仅截断，前后均保留真实
    文章，不误伤正文。"""
    if not s:
        return s
    L = len(s)
    cut = None
    # 1) 尾部 footer：连续密集的 "(opens in a new window)" 链接块（站点导航墙）。
    #    找最长「相邻 opens 间距 <= max_gap」的连续运行，其起点即 footer 起点；
    #    该运行通常远大于正文内零散的引用链接，可精准切除整块 nav 而不误伤。
    opens = [m.start() for m in re.finditer(r'\(opens in a new window\)', s)]
    if opens:
        best_start = 0
        best_len = 0
        i = 0
        n = len(opens)
        while i < n:
            j = i
            while j + 1 < n and opens[j + 1] - opens[j] <= 140:
                j += 1
            run = j - i + 1
            if run > best_len:
                best_len = run
                best_start = i
            i = j + 1
        # footer 须位于后半部分，且为一长串密集链接（>=6）
        if best_len >= 6 and opens[best_start] >= L * 0.35:
            cut = opens[best_start]
    # 2) HARD 锚点兜底（版权/隐私/法律链接），取最早 >=25%
    _HARD = re.compile(
        r'(?i)(OpenAI ©|Anthropic ©|Google ©|Microsoft ©|Meta ©|'
        r'Your privacy choices|Terms of Use Privacy Policy|'
        r'Company\s+About Us\s+Our Charter)')
    for m in _HARD.finditer(s):
        if m.start() >= L * 0.25:
            cut = m.start() if cut is None else min(cut, m.start())
            break
    if cut is not None:
        s = s[:cut].rstrip()
        s = s.replace("\u2060", " ")
        s = re.sub(r'[ \t]*\|[ \t]*$', '', s)
        s = re.sub(r'[ \t]*learn more\s*$', '', s, flags=re.I)
        s = re.sub(r'[ \t]*openai\s*$', '', s, flags=re.I)
        s = re.sub(r'[ \t]*anthropic\s*$', '', s, flags=re.I)
        s = re.sub(r'[ \t]*keep\s*$', '', s, flags=re.I)
        s = s.rstrip()
    # 3) 开头 leading nav："{标题} | OpenAI Skip to main content <nav> (opens in a new window)"
    #    nav 运行内无句子标点，止于首个 "(opens in a new window)" 之后、且位于首个句子结束标点之前。
    lm = re.search(r'(?i)(?:OpenAI|Anthropic)\s+Skip to main content', s)
    if lm and lm.start() <= L * 0.30:
        first_dot = L
        md = re.search(r'[.!?]\s', s[lm.start():])
        if md:
            first_dot = lm.start() + md.start()
        last_open = -1
        for om in re.finditer(r'\(opens in a new window\)', s[lm.start():first_dot]):
            last_open = lm.start() + om.end()
        if last_open > lm.start():
            prefix = s[:lm.start()].rstrip()
            prefix = re.sub(r'[ \t]*\|[ \t]*$', '', prefix)
            suffix = s[last_open:].lstrip()
            s = (prefix + "\n\n" + suffix) if prefix else suffix
    return s


_NAV_TOKENS = re.compile(
    r'(?i)^(research|researches|index|overview|economic|latest|advancement|'
    r'safety|approach|deployment|product|products|solution|solutions|resource|'
    r'resources|company|about|charter|career|careers|news|support|help|center|'
    r'privacy|policy|policies|term|terms|developer|developers|forum|academy|'
    r'stories|story|podcast|rss|platform|api|login|log|cookie|menu|home|'
    r'contact|service|services|footer|sitemap|language|region|subscribe|'
    r'newsletter|more|related|topics|categories|tags)$')


def _tok_is_nav(tok):
    if not tok:
        return False
    if _NAV_TOKENS.match(tok):
        return True
    if re.match(r'^[A-Z][A-Za-z0-9.\-]+$', tok):   # titlecase / 版本号式 (GPT-5.6)
        return True
    if re.match(r'^\d+(?:\.\d+)*$', tok):          # 纯数字 / 版本号 (5.6, 2026)
        return True
    return False


def _strip_plain_nav_tail(s):
    """剥离尾部『无 (opens) 的纯文本站点导航』（OpenAI 博客 footer 前的
    'Research Index Research Overview Economic Research Latest Advancements
    GPT-5.6 GPT-5.5 GPT-5. 4 Safety Approach Deployment Safety' 之类）。
    保守：仅当尾部存在一段 >=6 个连续 nav token、其中 >=2 个命中已知导航词、
    且位于后半部分、无句末标点 / 无 CJK 时切除。"""
    if not s:
        return s
    L = len(s)
    start = max(0, int(L * 0.55))
    tail = s[start:]
    toks = tail.split()
    # 从末尾向前找最长连续 nav token 运行
    i = len(toks) - 1
    run_start = i + 1
    while i >= 0:
        if _tok_is_nav(toks[i]):
            run_start = i
            i -= 1
        else:
            break
    run = toks[run_start:]
    if len(run) < 6:
        return s
    curated = sum(1 for t in run if _NAV_TOKENS.match(t))
    if curated < 2:
        return s
    nav_text = " ".join(run)
    if re.search(r'[\u4e00-\u9fff]', nav_text):
        return s
    idx = tail.rfind(nav_text)
    if idx == -1:
        return s
    cut = start + idx
    s = s[:cut].rstrip()
    s = re.sub(r'[ \t]*\|[ \t]*$', '', s)
    s = s.rstrip()
    return s


_REL_SECTION = re.compile(
    r'(?i)\b(company|research|safety|global affairs|economic|products?|news|'
    r'policy|policies|trust|education|developers?|frontier|social|integrations?|'
    r'engineering|media|communications|approach|deployment|overview|index)\s+'
    r'[A-Z][a-z]{2,9} \d{1,2}, \d{4}')


def _strip_related_stories(s):
    """剥离尾部『相关阅读 / More Stories』widget（OpenAI/Anthropic 博客文末的
    '标题 栏目 月 日, 年' 重复条目，如 'A scorecard for the AI age Company Jul 17,
    2026 Why teens deserve access to safe AI Safety Jul 16, 2026 ...'）。
    保守：仅当尾部存在 >=2 个『栏目+日期』条目且相距 < 700 字时切除，并向前吞掉
    可能的 'Keep reading / More / Related' 前导标签。"""
    if not s:
        return s
    L = len(s)
    region_start = int(L * 0.45)
    region = s[region_start:]
    ms = list(_REL_SECTION.finditer(region))
    if len(ms) < 2:
        return s
    d1, d2 = ms[-2], ms[-1]
    if d2.start() - d1.start() > 700:
        return s
    # widget 起点：第一个条目『栏目』之前的故事标题起点
    seg = region[:d1.start()]
    window = seg[-260:] if len(seg) >= 260 else seg
    m = list(re.finditer(r'[\n|]|\.\s|\.\n|\!\s|\?\s', window))
    if m:
        boundary_off = (len(seg) - len(window)) + m[-1].end()
        cut_in_region = boundary_off
    else:
        cut_in_region = max(0, d1.start() - 200)
    # 再向前吞掉可能的 "Keep reading / More / Related / View all" 前导标签
    pre = region[:cut_in_region]
    mlab = re.search(
        r'(?:^|[\n|])\s*(keep reading|more stories|more|related|you may also like|'
        r'view all|read more|see all|keep exploring)\s*[:|\-]?\s*$', pre, flags=re.I)
    if mlab:
        cut_in_region = mlab.start()
    cut = region_start + cut_in_region
    s = s[:cut].rstrip()
    s = re.sub(r'[ \t]*\|[ \t]*$', '', s)
    s = s.rstrip()
    return s


def _strip_image_credit(s):
    """删除图片版权/来源署名行（"Image Credits:Google (screenshot)" / "图源：台积电" 等）。
    判定：短行(<=200)、无句末标点、且整行去除信用词后仅剩短来源标识(<=60字) → 视为署名行删除。
    长行（如内嵌图注"▲ 图源：台积电 对于今年…"）保留，避免误删正文。"""
    if not s:
        return s
    out = []
    for ln in s.split("\n"):
        t = ln.strip()
        if not t:
            out.append(ln); continue
        if (_IMG_CREDIT.search(t) and len(t) <= 200
                and not re.search(r'[.!?。！？]', t)):
            stripped = _IMG_CREDIT.sub('', t)
            stripped = _WS.sub('', stripped).strip(' :：\t')
            if len(stripped) <= 60:
                continue
        out.append(ln)
    return "\n".join(out)


def _strip_junk_lines(s):
    """删除孤立广告/赞助标签行与残留 Markdown 图片/空链接行。"""
    if not s:
        return s
    out = []
    for ln in s.split("\n"):
        t = ln.strip()
        if not t:
            out.append(ln); continue
        if _AD_LABEL.match(t) and len(t) <= 60:
            continue
        if _MD_IMG.match(t) or _MD_EMPTY_LINK.match(t):
            continue
        out.append(ln)
    return "\n".join(out)


def _normalize_text(s):
    """零宽/不换行空格归一、合并多余空行。不删标签（避免误伤代码中的 < >）。"""
    if not s:
        return s
    s = _WS.sub(lambda m: ' ' if m.group(0) == '\u00a0' else '', s)
    s = re.sub(r'[ \t]+\n', '\n', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    return s.strip()


def deep_clean_extra(s):
    """对 trafilatura 残留正文做更强清理（站点页脚/导航墙、图片署名、广告/Markdown 图片行、零宽字符）。
    幂等、保守；删除后若正文长度 < 原长 50% 且原长 > 200，则回退保留原文以防误伤。"""
    if not s or not s.strip():
        return s
    orig = s
    s = _strip_site_nav_chrome(s)
    s = _strip_plain_nav_tail(s)
    s = _strip_related_stories(s)
    s = _strip_image_credit(s)
    s = _strip_junk_lines(s)
    s = _normalize_text(s)
    if len(orig) > 200 and len(s) < 0.5 * len(orig):
        return orig   # 疑似误伤，回退保留原文
    return s


# ───────────────────────── 主流程 ─────────────────────────
def main():
    ap = argparse.ArgumentParser(description="对 trafilatura 残留正文做更强 chrome 清理（离线、幂等、安全）")
    ap.add_argument("--dry-run", action="store_true", help="只统计将改变的条数，不写盘")
    args = ap.parse_args()

    arch = json.load(open(ARCH_PATH, encoding="utf-8"))
    print(f"载入 archive.json：{len(arch)} 期")

    # 离线、幂等清理：对全部含正文条目应用 deep_clean_extra。
    # 已干净条目（含此前 148 条译文 / 60 条 AI HOT 正文）为 no-op；
    # 仅真正残留 chrome 的条目（即「那 323 条」中仍带站点导航/署名/广告噪声者）被改写。
    stats = {"checked": 0, "cleaned": 0, "unchanged": 0, "examples": []}
    for date in sorted(arch.keys(), reverse=True):
        rec = arch[date]
        for sec in rec.get("sections", []):
            for it in sec.get("items", []):
                old = it.get("content") or ""
                if not old.strip():
                    continue
                stats["checked"] += 1
                new = deep_clean_extra(old)
                if new != old:
                    it["content"] = new
                    stats["cleaned"] += 1
                    if len(stats["examples"]) < 25:
                        stats["examples"].append(
                            (date, sec.get("label", ""), it.get("title", "")[:40], len(old), len(new)))
                else:
                    stats["unchanged"] += 1

    print(f"[清理] 检查含正文条目 {stats['checked']} | "
          f"已更强清理 {stats['cleaned']} | 原本已干净 {stats['unchanged']}")
    for d, lab, t, lo, ln in stats["examples"]:
        print(f"    · {d} [{lab}] {t}  ({lo}→{ln} 字)")

    if args.dry_run:
        print("[dry-run] 未写盘。")
        return

    if stats["cleaned"] == 0:
        print("无条目需要清理，跳过写盘。")
        return

    # 备份 + 写盘
    import shutil
    bak = ARCH_PATH + ".bak"
    shutil.copy2(ARCH_PATH, bak)
    print(f"已备份 -> {bak}")
    json.dump(arch, open(ARCH_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("已写回 archive.json")
    print("下一步：运行 `python generate_archive.py --render-only` 重新渲染全部日报。")


if __name__ == "__main__":
    main()

"""Job: 首都经济贸易大学（首经贸 / CUEB）近期新闻，重要性由大模型判断。

分工：

    代码做确定性的事 —— 抓取、判新闻页、时效硬过滤、同事件归并、
                       把候选压成紧凑清单、解析并校验模型的 JSON、排序、渲染。
    模型做它擅长的事 —— 判断"哪条更重要"。

模型拿到结构化候选（标题 / 来源 / 日期 / 报道家数 / 摘要），按写死的评分细则
打 0–100 并给一句理由。代码只负责收分、夹区间、按分排序——排序的可解释性来自
模型给的理由，不是权重表。

两条保险：
1. 模型调用失败或 JSON 解析不出来 → 自动降级到启发式打分，结果里标注原因；
2. 模型只评了部分条目 → 缺的用启发式补齐，同样标注。
"""

import json
import re
from datetime import datetime
from difflib import SequenceMatcher

from slife2.job import call, call_tool

_OFFICIAL = ("cueb.edu.cn", "cueb.edu")
_MAJOR = (
    "people.com.cn", "xinhuanet.com", "news.cn", "gmw.cn", "chinadaily.com.cn",
    "jyb.cn", "eol.cn", "thepaper.cn", "caixin.com", "yicai.com", "21jingji.com",
    "ce.cn", "cnr.cn", "cctv.com", "chinanews.com.cn", "stcn.com", "cs.com.cn",
)
_PORTAL = (
    "sina.com.cn", "sina.cn", "sohu.com", "163.com", "qq.com", "ifeng.com",
    "baidu.com", "toutiao.com", "zhihu.com", "douyin.com", "bilibili.com",
)
_ALIASES = ("首都经济贸易大学", "首经贸", "CUEB", "cueb")
_EVENT_WORDS = (
    "举行", "召开", "举办", "发布", "揭牌", "签约", "启动", "开幕", "闭幕",
    "入选", "获奖", "荣获", "获批", "成立", "共建", "大会", "论坛", "峰会",
    "研讨", "讲座", "调研", "座谈", "交流", "合作", "周年",
)
_NON_ARTICLE_PATH = re.compile(
    r"(/(index|default|main)\.(?:html?|php|aspx?|jsp)$)|(/(zzjs|szmpx|zsxx|rcpy|xygk|xkgk)/)",
    re.IGNORECASE,
)
_NON_ARTICLE_TITLE = re.compile(
    r"(学院|大学|英文网|学校简介|招生|专业介绍|师资|教师|队伍|联系我们|人才培养|科学研究)$"
)

_RUBRIC = """你是高校新闻编辑，要为一个"首都经济贸易大学动态简报"挑选头条。

给每条候选打 0-100 分，衡量的是「对关心这所学校的人有多重要」，按以下细则加权：

- 事件量级（40 分）：全校性/校级重大事件（校庆、重大改革、重要人事、重大平台成立）
  > 跨学院或多部门协同 > 单个学院的常规活动 > 日常学术讲座。
- 战略相关度（30 分）：是否涉及学科建设、科研突破、人才引进、重大合作、招生就业、
  国际化、AI/数字化等学校核心议题；纯礼仪性、纯内部事务得分低。
- 媒体层级（20 分）：中央媒体 > 北京市媒体/全国性专业媒体 > 校内媒体 > 自媒体转载。
- 传播面（10 分）：被多家媒体报道的得分高（候选里给了"报道家数"）。

硬性要求：
- 只输出 JSON 数组，不要任何解释性文字、不要 markdown 代码块。
- 每个元素形如 {"id": 编号整数, "score": 整数0到100, "why": "不超过40字的中文理由"}。
- 每个候选都要有一条，id 用我给的原编号，不要改写。
"""


def _domain(link: str) -> str:
    m = re.match(r"https?://([^/]+)", link or "")
    host = m.group(1).lower() if m else ""
    return host[4:] if host.startswith("www.") else host


def _path(link: str) -> str:
    m = re.match(r"https?://[^/]+(/.*)?$", link or "")
    return (m.group(1) or "/") if m else "/"


def _is_article(link: str, title: str) -> bool:
    path = _path(link)
    if path in ("", "/"):
        return False
    if _NON_ARTICLE_PATH.search(path):
        return False
    t = (title or "").strip()
    if len(t) < 8 or _NON_ARTICLE_TITLE.search(t):
        return False
    return True


def _parse_date(text: str):
    if not text:
        return None
    text = text.strip()
    for fmt in ("%b %d, %Y", "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def _fingerprint(title: str) -> str:
    return re.sub(r"[\s\W_]+", "", (title or ""))[:40].lower()


def _same_event(a: dict, b: dict) -> bool:
    fa, fb = _fingerprint(a["title"]), _fingerprint(b["title"])
    if fa and fa == fb:
        return True
    sa, sb = a.get("snippet", ""), b.get("snippet", "")
    if len(sa) >= 40 and len(sb) >= 40:
        return SequenceMatcher(None, sa[:120], sb[:120]).ratio() >= 0.6
    return False


def _heuristic(link: str, title: str, snippet: str, date_text: str, reports: int) -> int:
    d = _domain(link)
    if any(d.endswith(x) for x in _OFFICIAL):
        src = 20.0
    elif any(d.endswith(x) for x in _MAJOR):
        src = 14.0
    elif any(d.endswith(x) for x in _PORTAL):
        src = 4.0
    else:
        src = 7.0
    published = _parse_date(date_text)
    fresh = 15.0 if published is None else max(0.0, 15.0 - (datetime.now() - published).days)
    keyword = 20.0 if any(a in (title or "") for a in _ALIASES) else 5.0
    event = min(20.0, sum(1 for w in _EVENT_WORDS if w in (title or "") + " " + (snippet or "")) * 7.0)
    spread = min(25.0, 10.0 * max(0, reports - 1))
    return int(max(0, min(100, round(src + fresh + keyword + event + spread))))


async def _fetch(keyword: str) -> tuple:
    warnings, raw = [], []

    async def serper(q):
        result = await call_tool(
            "serper__google_search", {"q": q, "gl": "cn", "hl": "zh-cn", "num": 8}
        )
        if not result.ok:
            warnings.append(f"serper({q}): {result.text}")
            return
        try:
            payload = json.loads(result.text)
        except Exception:
            warnings.append("serper: 返回不是合法 JSON")
            return
        for item in payload.get("organic") or []:
            raw.append({
                "title": item.get("title") or "",
                "url": item.get("link") or "",
                "snippet": re.sub(r"\s+", " ", item.get("snippet") or "")[:180],
                "date": item.get("date") or "",
            })

    for q in (f"{keyword} 新闻", f"{keyword} 最新动态", f"{keyword} site:cueb.edu.cn"):
        await serper(q)

    ds = await call_tool(
        "deepseek-search__webSearch",
        {"query": f"{keyword} 近期新闻", "count": 8, "need_content": False},
    )
    if not ds.ok:
        warnings.append(f"deepseek: {ds.text}")
    else:
        for block in re.split(r"\n(?=Title:)", ds.text or ""):
            title = re.search(r"Title:\s*(.+)", block)
            link = re.search(r"URL:\s*(\S+)", block)
            if not (title and link):
                continue
            body = block.split("Content:", 1)[-1].split("URL:", 1)[0].strip()
            meta = re.search(r"Meta:\s*(.+)", block)
            date_text = ""
            if meta:
                d = re.search(r"发布于\s*([\d年月日\-/.]+)", meta.group(1))
                if d:
                    date_text = d.group(1)
            raw.append({
                "title": title.group(1).strip(),
                "url": link.group(1).strip(),
                "snippet": re.sub(r"\s+", " ", body)[:180],
                "date": date_text,
            })
    return raw, warnings


def _collect(raw: list, days: int) -> tuple:
    dropped_page = dropped_old = 0
    bucket = []
    for item in raw:
        title, link = item.get("title", ""), item.get("url", "")
        if not title or not link:
            continue
        if not _is_article(link, title):
            dropped_page += 1
            continue
        published = _parse_date(item.get("date", ""))
        if published is not None and (datetime.now() - published).days > days:
            dropped_old += 1
            continue
        entry = {
            "title": title,
            "url": link,
            "snippet": item.get("snippet", ""),
            "date": item.get("date", ""),
            "source": _domain(link),
            "reports": 1,
        }
        for kept in bucket:
            if _same_event(kept, entry):
                kept["reports"] += 1
                # 换一条更有信息量的摘要/更好的来源
                if len(entry["snippet"]) > len(kept["snippet"]):
                    kept["snippet"] = entry["snippet"]
                break
        else:
            bucket.append(entry)
    return bucket, dropped_page, dropped_old


def _extract_json(text: str):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.MULTILINE).strip()
    start, end = t.find("["), t.rfind("]")
    if start != -1 and end > start:
        try:
            parsed = json.loads(t[start:end + 1])
            if isinstance(parsed, list):
                return parsed
        except Exception:
            pass
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(t[start:end + 1])
            if isinstance(parsed, dict):
                for key in ("items", "data", "results", "news"):
                    if isinstance(parsed.get(key), list):
                        return parsed[key]
        except Exception:
            pass
    return None


async def _judge(items: list) -> tuple:
    listing = []
    for i, item in enumerate(items):
        spread = f"，{item['reports']} 家报道" if item["reports"] > 1 else ""
        listing.append(
            f"[{i}] 标题：{item['title']}\n"
            f"    来源：{item['source'] or '未知'}｜日期：{item['date'] or '未标注'}{spread}\n"
            f"    摘要：{item['snippet'][:120] or '（无）'}"
        )
    prompt = "候选新闻如下：\n\n" + "\n\n".join(listing) + "\n\n按细则逐条打分，只给 JSON 数组。"

    try:
        result = await call(
            "openai-completions",
            "stream_chat",
            {
                "provider": "deepseek",
                "model": "deepseek-flash",
                "messages": [
                    {"role": "system", "content": _RUBRIC},
                    {"role": "user", "content": prompt},
                ],
                "tools": [],
            },
        )
    except Exception as exc:
        return {}, f"模型调用异常：{exc}"

    if not result.ok:
        return {}, f"模型调用失败：{result.text}"

    parsed = _extract_json(result.text)
    if parsed is None:
        return {}, "模型返回无法解析为 JSON"

    scored = {}
    for row in parsed:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("id"))
            score = float(row.get("score"))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(items)):
            continue
        why = str(row.get("why") or "").strip()[:60]
        scored[idx] = (int(max(0, min(100, round(score)))), why)
    return scored, ""


async def cueb_news(keyword: str = "首都经济贸易大学", days: int = 30, top: int = 10,
                    use_model: bool = True) -> str:
    """查找首都经济贸易大学（首经贸）的近期新闻，重要性由大模型判断后排序返回。

    Args:
        keyword: 检索关键词，默认“首都经济贸易大学”，可换成“首经贸”等别名。
        days: 时效窗口天数；发布日期早于此的条目会被直接丢弃。
        top: 返回条数上限。
        use_model: 是否用大模型判断重要性；关掉则退回启发式打分（用于对照或模型不可用时）。
    """
    raw, warnings = await _fetch(keyword)
    items, dropped_page, dropped_old = _collect(raw, days)

    if not items:
        detail = "；".join(warnings) if warnings else "两条检索通道都没返回可用条目"
        return f"没有取到“{keyword}”的近期新闻。原因：{detail}"

    notes = []
    judged, err = ({}, "")
    if use_model:
        judged, err = await _judge(items)
        if err:
            notes.append(f"{err}，已降级为启发式打分")
        elif len(judged) < len(items):
            notes.append(f"模型只评了 {len(judged)}/{len(items)} 条，其余用启发式补齐")
    else:
        notes.append("按参数关闭模型判断，使用启发式打分")

    ranked = []
    for idx, item in enumerate(items):
        if idx in judged:
            score, why, judge = judged[idx][0], judged[idx][1], "模型"
        else:
            score = _heuristic(item["url"], item["title"], item["snippet"],
                               item["date"], item["reports"])
            why, judge = "来源+时效+关键词+事件+报道面", "启发式"
        ranked.append({**item, "score": score, "why": why, "judge": judge})

    ranked.sort(key=lambda x: (-x["score"], x["title"]))
    ranked = ranked[:top]

    mode = "大模型判断" if any(r["judge"] == "模型" for r in ranked) else "启发式打分"
    lines = [f"## {keyword} · 近期新闻（重要性：{mode}，共 {len(ranked)} 条）", ""]
    for i, item in enumerate(ranked, 1):
        meta = [item["source"] or "来源不明"]
        if item["date"]:
            meta.append(item["date"])
        if item["reports"] > 1:
            meta.append(f"{item['reports']} 家报道")
        lines.append(f"**{i}. {item['title']}**")
        lines.append(f"   - {' · '.join(meta)} · {item['score']} 分")
        if item["why"]:
            lines.append(f"   - 理由（{item['judge']}）：{item['why']}")
        if item["snippet"]:
            lines.append(f"   - {item['snippet']}")
        lines.append(f"   - {item['url']}")
        lines.append("")

    if dropped_page:
        notes.append(f"滤掉 {dropped_page} 条非新闻页")
    if dropped_old:
        notes.append(f"滤掉 {dropped_old} 条超期旧闻（早于 {days} 天）")
    if warnings:
        notes.append("；".join(warnings))
    if notes:
        lines.append("> " + "；".join(notes))

    return "\n".join(lines).strip()

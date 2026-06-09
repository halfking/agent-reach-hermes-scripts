#!/usr/bin/env python3
"""
github_trending.py - GitHub Trending 抓取器

抓取 GitHub Trending 页面（daily/weekly/monthly），返回项目列表。
比 API 更准确：trending 反映"今日增速"而非总 stars。

用法:
    from github_trending import fetch_trending, score_repo

    repos = fetch_trending(since='daily', language='', spoken_language='')
    repos_zh = fetch_trending(since='daily', spoken_language='zh')

    # 中文偏好评分
    for r in repos:
        score = score_repo(r)
        print(f"{score:.0f}  {r['name']}  +{r['stars_today']}/day")
"""
import re
import json
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Optional


TRENDING_URL = "https://github.com/trending"


def fetch_trending(
    since: str = "daily",  # daily / weekly / monthly
    language: str = "",     # python, javascript, rust, ...
    spoken_language: str = "",  # zh, en, ja, ...
    timeout: int = 20,
) -> list[dict]:
    """
    抓取 GitHub Trending 页面。

    Returns:
        [{
            "name": "owner/repo",
            "owner": "owner",
            "repo": "repo",
            "url": "https://github.com/owner/repo",
            "description": "...",
            "language": "Python",
            "stars_total": 1234,         # 总 stars（trending 列表里展示的累计）
            "forks_total": 567,
            "stars_period": 658,          # 本周期增量（today/this week/this month）
            "period": "daily",
            "rank": 1,
        }, ...]
    """
    params = {"since": since}
    if spoken_language:
        params["spoken_language_code"] = spoken_language
    url = TRENDING_URL
    if language:
        url = f"{TRENDING_URL}/{urllib.parse.quote(language)}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        html = resp.read().decode("utf-8")

    articles = html.split('<article class="Box-row"')[1:]  # 去掉头部
    repos = []
    for i, a in enumerate(articles, 1):
        # repo name: 第一个 <a href="/owner/repo">
        name_m = re.search(r'<h2[^>]*>\s*<a[^>]*href="/([^"]+)"', a)
        if not name_m:
            continue
        full_name = name_m.group(1).strip()
        if "/" not in full_name:
            continue
        owner, repo = full_name.split("/", 1)

        # description
        desc_m = re.search(r'<p class="col-9[^"]*">\s*([^<]+)', a)
        desc = desc_m.group(1).strip() if desc_m else ""
        desc = desc.replace("&amp;", "&").replace("&#39;", "'")

        # language
        lang_m = re.search(r'<span[^>]*itemprop="programmingLanguage">\s*([^<]+)', a)
        lang = lang_m.group(1).strip() if lang_m else ""

        # stars total（trending 行里显示的累计）
        # 类似 <a href="/owner/repo/stargazers">1,234</a>
        total_stars_m = re.search(
            r'href="/' + re.escape(full_name) + r'/stargazers"[^>]*>\s*([\d,]+)',
            a
        )
        stars_total = int(total_stars_m.group(1).replace(",", "")) if total_stars_m else 0

        # forks
        forks_m = re.search(
            r'href="/' + re.escape(full_name) + r'/forks"[^>]*>\s*([\d,]+)',
            a
        )
        forks_total = int(forks_m.group(1).replace(",", "")) if forks_m else 0

        # stars period（today/this week/this month 增量）
        period_m = re.search(r'(\d[\d,]*)\s*stars\s*(today|this week|this month)', a)
        stars_period = int(period_m.group(1).replace(",", "")) if period_m else 0

        repos.append({
            "name": full_name,
            "owner": owner,
            "repo": repo,
            "url": f"https://github.com/{full_name}",
            "description": desc,
            "language": lang,
            "stars_total": stars_total,
            "forks_total": forks_total,
            "stars_period": stars_period,
            "period": since,
            "rank": i,
        })

    return repos


def is_chinese_text(text: str) -> bool:
    """检测文本是否含中文字符"""
    if not text:
        return False
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff":
            return True
    return False


def score_repo(repo: dict, prefer_chinese: bool = True) -> float:
    """
    热度评分公式（中文市场偏好版）：
      base       = stars_total * 0.3       # 总量基础分
      momentum   = stars_period * 50       # 增速分（关键）
      chinese    = +10000 if 描述含中文     # 中文加分
      new_bonus  = +5000 if stars < 5000   # 新项目加分（增速 > 总量时偏好）
    """
    score = repo.get("stars_total", 0) * 0.3
    score += repo.get("stars_period", 0) * 50

    desc = repo.get("description", "")
    if prefer_chinese and is_chinese_text(desc):
        score += 10000

    # 新项目偏好：累计 < 5K 但 today/week 增速高，说明是爆款新项目
    if repo.get("stars_total", 0) < 5000 and repo.get("stars_period", 0) > 200:
        score += 5000

    return round(score, 1)


def fetch_trending_multi(
    spoken_languages: list[str] = None,
    languages: list[str] = None,
    since: str = "daily",
) -> list[dict]:
    """
    多维度抓取 trending（中文优先 + 全语言 + 各编程语言）。
    去重后按热度评分排序。
    """
    if spoken_languages is None:
        spoken_languages = ["", "zh"]  # 全部 + 中文
    if languages is None:
        languages = [""]  # 全部语言，可加 "python", "javascript", "rust"

    seen = {}
    for sl in spoken_languages:
        for lang in languages:
            try:
                repos = fetch_trending(since=since, language=lang, spoken_language=sl)
                for r in repos:
                    key = r["name"]
                    # 中文版优先（覆盖）
                    if key not in seen or (sl == "zh" and is_chinese_text(r["description"])):
                        seen[key] = r
            except Exception as e:
                print(f"[trending] {sl}/{lang}/{since} 抓取失败: {e}")

    # 评分排序
    results = list(seen.values())
    for r in results:
        r["score"] = score_repo(r)
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def save_ranking(repos: list[dict], output_dir: str = None) -> str:
    """
    持久化榜单到 ~/.agent-reach/rankings/daily/YYYY-MM-DD.json
    返回文件路径。
    """
    if output_dir is None:
        output_dir = str(Path.home() / ".agent-reach" / "rankings" / "daily")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    path = Path(output_dir) / f"{today}.json"

    data = {
        "date": today,
        "generated_at": datetime.now().isoformat(),
        "source": "github-trending",
        "total": len(repos),
        "repos": repos,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return str(path)


if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "daily"

    if cmd in ("daily", "weekly", "monthly"):
        print(f"=== GitHub Trending ({cmd}) ===")
        repos = fetch_trending_multi(since=cmd)

        print(f"\n📊 共 {len(repos)} 个项目（已按中文偏好评分排序）\n")
        for i, r in enumerate(repos[:15], 1):
            zh_mark = "🇨🇳" if is_chinese_text(r["description"]) else "  "
            print(f"{i:2d}. {zh_mark} {r['name']:<45} ⭐+{r['stars_period']:>4}/{cmd[:1]}  total:{r['stars_total']:>5}  score:{r['score']:>7.0f}")
            if r["description"]:
                print(f"        {r['description'][:90]}")
            print()

        # 持久化
        path = save_ranking(repos)
        print(f"✅ 榜单已保存: {path}")

    elif cmd == "chinese":
        # 仅看中文项目
        repos = fetch_trending(since="daily", spoken_language="zh")
        print(f"=== 中文 GitHub Trending (daily) — {len(repos)} 个 ===\n")
        for i, r in enumerate(repos[:10], 1):
            print(f"{i:2d}. {r['name']:<45} ⭐+{r['stars_period']}/day")
            if r["description"]:
                print(f"    {r['description'][:90]}")
            print()

    else:
        print("Usage: github_trending.py {daily|weekly|monthly|chinese}")
        sys.exit(1)

#!/usr/bin/env python3
"""
leaderboard.py - GitHub 热门项目榜单查询工具

用法:
    leaderboard.py today          # 今日榜单（先 fetch）
    leaderboard.py 2026-06-09     # 指定日期榜单
    leaderboard.py history        # 列出所有历史榜单
    leaderboard.py top --days 7   # 近 7 天 Top 10（按累计 score）
    leaderboard.py chinese        # 中文项目专属榜单
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict


RANKING_DIR = Path.home() / ".agent-reach" / "rankings" / "daily"


def load_ranking(date_str: str) -> dict:
    """加载某天的榜单 JSON"""
    path = RANKING_DIR / f"{date_str}.json"
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def list_history():
    """列出所有历史榜单文件"""
    if not RANKING_DIR.exists():
        print("📋 暂无历史榜单")
        return
    files = sorted(RANKING_DIR.glob("*.json"), reverse=True)
    print(f"📋 共 {len(files)} 个历史榜单：\n")
    for f in files[:30]:
        date = f.stem
        with open(f) as fp:
            data = json.load(fp)
        total = data.get("total", 0)
        print(f"  {date}  ({total} 个项目)")


def show_ranking(date_str: str = "today"):
    """显示某天的榜单"""
    if date_str == "today":
        date_str = datetime.now().strftime("%Y-%m-%d")

    data = load_ranking(date_str)
    if not data:
        print(f"❌ {date_str} 无榜单数据")
        print(f"   提示: 运行 'python3 github_trending.py daily' 抓取今日数据")
        return

    print(f"📊 {date_str} GitHub Trending 榜单 ({data.get('total', 0)} 个)\n")
    print(f"{'排名':<4} {'标识':<3} {'项目':<48} {'增量':<12} {'语言':<12} {'分数'}")
    print("-" * 100)

    for i, r in enumerate(data.get("repos", [])[:20], 1):
        is_zh = "🇨🇳" if any("\u4e00" <= ch <= "\u9fff" for ch in r.get("description", "")) else "  "
        period = f"+{r.get('stars_period', 0)}/{r.get('period', '?')[:1]}"
        name = r["name"][:46]
        lang = r.get("language", "")[:10]
        score = r.get("score", 0)
        print(f"{i:<4} {is_zh:<3} {name:<48} {period:<12} {lang:<12} {score:>7.0f}")
        desc = r.get("description", "")[:80]
        if desc:
            print(f"     {desc}")
        print()


def top_n_recent(days: int = 7):
    """近 N 天累计 score 最高的项目（项目级排行榜）"""
    project_scores = defaultdict(lambda: {"score": 0, "appearances": 0, "max_period": 0, "data": None})

    for offset in range(days):
        date = (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
        data = load_ranking(date)
        for r in data.get("repos", []):
            key = r["name"]
            project_scores[key]["score"] += r.get("score", 0)
            project_scores[key]["appearances"] += 1
            project_scores[key]["max_period"] = max(
                project_scores[key]["max_period"],
                r.get("stars_period", 0)
            )
            project_scores[key]["data"] = r

    if not project_scores:
        print(f"❌ 近 {days} 天无榜单数据")
        return

    # 按累计 score 排序
    sorted_projects = sorted(project_scores.items(), key=lambda x: x[1]["score"], reverse=True)

    print(f"🏆 近 {days} 天 GitHub Top 20（按累计 score 排序）\n")
    print(f"{'排名':<4} {'标识':<3} {'项目':<48} {'登榜次数':<10} {'累计分数'}")
    print("-" * 90)

    for i, (name, info) in enumerate(sorted_projects[:20], 1):
        r = info["data"]
        is_zh = "🇨🇳" if any("\u4e00" <= ch <= "\u9fff" for ch in r.get("description", "")) else "  "
        print(f"{i:<4} {is_zh:<3} {name[:46]:<48} {info['appearances']}/{days:<8} {info['score']:>7.0f}")
        desc = r.get("description", "")[:80]
        if desc:
            print(f"     {desc}")
        print()


def chinese_only():
    """仅显示中文项目榜单"""
    today = datetime.now().strftime("%Y-%m-%d")
    data = load_ranking(today)
    if not data:
        print(f"❌ {today} 无榜单数据")
        return

    chinese_repos = [
        r for r in data.get("repos", [])
        if any("\u4e00" <= ch <= "\u9fff" for ch in r.get("description", ""))
    ]

    print(f"🇨🇳 {today} 中文 GitHub 项目榜（{len(chinese_repos)} 个）\n")
    for i, r in enumerate(chinese_repos[:15], 1):
        print(f"{i:2d}. {r['name']:<45} ⭐+{r.get('stars_period', 0)}/{r.get('period', 'd')[:1]}  score:{r.get('score', 0):.0f}")
        if r.get("description"):
            print(f"    {r['description']}")
        print()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "today"

    if cmd == "today":
        show_ranking("today")
    elif cmd == "history":
        list_history()
    elif cmd == "chinese":
        chinese_only()
    elif cmd == "top":
        days = 7
        if "--days" in sys.argv:
            days = int(sys.argv[sys.argv.index("--days") + 1])
        top_n_recent(days)
    elif len(cmd) == 10 and cmd[4] == "-":  # YYYY-MM-DD
        show_ranking(cmd)
    else:
        print(__doc__)
        sys.exit(1)

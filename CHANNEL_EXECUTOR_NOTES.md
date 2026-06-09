备忘：TrendRadar Channel Executor 注册方式（184 server）
1. 文件路径：/opt/trendaradar/mcp_server/orchestration/channel_X_executor.py
2. 三处注册：TYPE_CHECKING import → __init__ 参数+实例化 → dispatch_run() 路由
3. Agent Reach 已注册：agent.reach.github_trending/leaderboard/daily_post
4. Agent Reach scripts: /opt/trendaradar/agent_reach/scripts/
5. Daily ranking JSON: /opt/trendaradar/agent_reach/rankings/daily/YYYY-MM-DD.json
6. 容器抓 GitHub Trending 有 IncompleteRead 问题——已用 partial bytes 容错
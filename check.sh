#!/usr/bin/env bash
# 手动跑一次检查。双击或 ./check.sh 均可。
# 只跑确定性采集（秀动）+ 并入 research/inbox 里待处理的调研文件。
# 大麦 / KPop / 舆情的联网调研由 Claude 每日任务负责，见 AGENT_TASK.md。
set -euo pipefail
cd "$(dirname "$0")"
python3 monitor.py check
python3 monitor.py status
echo
echo "打开页面：open site/index.html"

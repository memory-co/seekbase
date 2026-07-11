#!/usr/bin/env bash
# 跑两个方案的中文模糊检索 POC。
# 设了 QWEN_KEY 就用真·中文向量,否则用确定性 hash embedder(仅验证链路)。
#   QWEN_KEY=sk-xxx ./run.sh
set -euo pipefail
cd "$(dirname "$0")"
PY="${PY:-../.venv/bin/python}"

echo "########## 方案 A:lance 扩展 ##########"
"$PY" a_lance_ext.py 2>&1 | grep -v -E "WARN|INFO" || true
echo
echo "########## 方案 B:vss + fts ##########"
"$PY" b_vss_fts.py 2>&1 | grep -v -E "WARN|INFO" || true

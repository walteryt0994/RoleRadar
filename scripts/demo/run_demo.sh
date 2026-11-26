#!/usr/bin/env bash
# Start the RoleRadar demo on this computer only (http://127.0.0.1:<port>), on a folder made by prepare_demo.py.
#
#   scripts/demo/run_demo.sh <demo folder> [port, default 8850]
#
# - Serves only 127.0.0.1 (the app refuses any other address) with the development sign-in of the synthetic users.
# - Starts the app with an empty environment (env -i): no API key, real source or approval set in your shell can reach
#   it, so every analysis, exploration and search runs on the local simulation and the synthetic sources.
# - On macOS it also runs inside sandbox-exec allowing outgoing connections to localhost only, as the project's own
#   previews do. Elsewhere it says so and relies on the empty environment.
# - Stop it with Ctrl-C. The folder is kept as it is; to start over, prepare a new folder (nothing is deleted).
# - Set ROLERADAR_PYTHON to use another Python than <repo>/.venv/bin/python. Set ROLERADAR_DEMO_DELAY (seconds, 0-60)
#   to make each simulated step wait, e.g. to film the progress page; the default is 0 (no artificial wait).
set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ]; then
  echo "用法：scripts/demo/run_demo.sh <演示目录> [端口，默认 8850]" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="${ROLERADAR_PYTHON:-${ROOT}/.venv/bin/python}"
PORT="${2:-8850}"
DELAY="${ROLERADAR_DEMO_DELAY:-0}"

if [ ! -d "$1" ]; then
  echo "无法启动：找不到演示目录 ${1}（先运行 scripts/demo/prepare_demo.py）。" >&2
  exit 2
fi

DEMO_DIR="$(cd "$1" && pwd)"

if [ ! -f "${DEMO_DIR}/demo-manifest.json" ] || [ ! -f "${DEMO_DIR}/library.db" ]; then
  echo "无法启动：${DEMO_DIR} 不是 prepare_demo.py 准备的演示目录（缺 demo-manifest.json 或 library.db）。" >&2
  exit 2
fi

if [ ! -x "${PYTHON}" ]; then
  echo "无法启动：找不到 Python（${PYTHON}）。先按 README 建好虚拟环境，或用 ROLERADAR_PYTHON 指定。" >&2
  exit 2
fi

case "${PORT}" in
  ''|*[!0-9]*) echo "无法启动：端口要是数字（收到 ${PORT}）。" >&2; exit 2 ;;
esac

if ! "${PYTHON}" -c 'import socket, sys
s = socket.socket()
# The same address reuse as the server itself, so a port a stopped server left waiting is not taken for busy.
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)
finally:
    s.close()' "${PORT}"; then
  # The suggested command quoted for the shell with the standard shlex.quote, so it can be copied as it is.
  SUGGESTED="$("${PYTHON}" -c 'import shlex, sys; print(" ".join(shlex.quote(part) for part in sys.argv[1:]))' "$0" "${DEMO_DIR}" "$((PORT + 1))")"
  echo "无法启动：127.0.0.1:${PORT} 已被占用。换一个端口，例如：${SUGGESTED}" >&2
  exit 2
fi

ARGS=(-m app.web_main --db "${DEMO_DIR}/library.db" --files "${DEMO_DIR}/files" --dev-login --port "${PORT}")

if [ "${DELAY}" != "0" ]; then
  ARGS+=(--simulation-delay "${DELAY}")
fi

CLEAN_ENV=(env -i "HOME=${HOME}" "PATH=/usr/bin:/bin" "PYTHONDONTWRITEBYTECODE=1" "LANG=${LANG:-en_US.UTF-8}")

echo "RoleRadar 演示：http://127.0.0.1:${PORT}  （目录 ${DEMO_DIR}）"
echo "合成资料 + 本机模拟服务；只监听本机；按 Ctrl-C 停止。"
cd "${ROOT}/backend"

if [ "$(uname -s)" = "Darwin" ] && command -v sandbox-exec >/dev/null 2>&1; then
  exec "${CLEAN_ENV[@]}" sandbox-exec -p '(version 1)(allow default)(deny network-outbound)(allow network-outbound (remote ip "localhost:*"))' "${PYTHON}" "${ARGS[@]}"
fi

echo "提示：这台电脑没有 sandbox-exec，只用空环境隔离（不会读到真实服务的配置）。"
exec "${CLEAN_ENV[@]}" "${PYTHON}" "${ARGS[@]}"

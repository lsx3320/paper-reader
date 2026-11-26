#!/usr/bin/env bash
# 把 paper-reader 发布到现有 VPS。
#
# 本机（通常是 Mac，arm64）只同步源码。镜像一律在 VPS（amd64）上
# `docker compose build`，绝不把本机镜像拷过去。
#
# 不会覆盖 VPS 的 .env（生产密钥、HOST_PORT=127.0.0.1:18010、
# DATA_HOST_DIR=/opt/paper-reader-data 都留在服务器上）。
# 不会碰 sing-box、代理端口 443/udp、8443/tcp、2083/tcp、proxy-sub、
# ufw 既有规则、证书或 Cloudflare。
#
#   ./scripts/deploy.sh --dry-run     # 只打印步骤，不连接、不改任何文件
#   ./scripts/deploy.sh --self-test   # 本机自检备份与 .env 保护，不接触 VPS
#   ./scripts/deploy.sh               # 正式部署
#   ./scripts/deploy.sh --host root@203.0.113.10
#
# 可选环境变量：
#   PAPER_READER_SSH          默认 root@23.27.0.190
#   PAPER_READER_REMOTE_DIR   默认 /opt/paper-reader
#   PAPER_READER_DATA_DIR     默认 /opt/paper-reader-data
#   PAPER_READER_HEALTH_URL   默认 http://127.0.0.1:18010/api/health
#   PAPER_READER_ALLOW_NO_DB  设为 1 才允许数据目录里没有 paper_reader.db

set -euo pipefail

SSH_OPTS=(-o ConnectTimeout=20 -o ServerAliveInterval=30 -o ServerAliveCountMax=20 -o StrictHostKeyChecking=accept-new)

RSYNC_EXCLUDES=(
  --exclude=.env
  --exclude=.git/
  --exclude=.git
  --exclude=data/
  --exclude=.venv/
  --exclude=venv/
  --exclude=__pycache__/
  --exclude='*.pyc'
  --exclude=tools/out/
  --exclude=.DS_Store
  --exclude='*.bak-*'
)

REMOTE_DIR=""
DATA_DIR=""
HEALTH_URL=""
PREV_DIR=""
PRESERVE=""
STATE_FILE=""
MARKER=""
TS=""
ENV_SHA=""
EARLY_DB=""
RESTORE_DB=""
IN_ROLLBACK=0

usage() {
  sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
  echo "deploy: $*" >&2
  exit 1
}

load_remote_config() {
  REMOTE_DIR=${PAPER_READER_REMOTE_DIR:-/opt/paper-reader}
  DATA_DIR=${PAPER_READER_DATA_DIR:-/opt/paper-reader-data}
  HEALTH_URL=${PAPER_READER_HEALTH_URL:-http://127.0.0.1:18010/api/health}
  PREV_DIR="${REMOTE_DIR}.prev"
  PRESERVE="${REMOTE_DIR}.env.preserve"
  STATE_FILE="${DATA_DIR}/backups/deploy-state.env"
  MARKER="${DATA_DIR}/backups/deploy-started-new"
  case "${REMOTE_DIR}${DATA_DIR}${HEALTH_URL}" in
    *[[:space:]]*|*"'"*)
      die "路径或健康检查 URL 不能包含空格或单引号"
      ;;
  esac
}

env_sha() {
  local f="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$f" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$f" | awk '{print $1}'
  else
    python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$f"
  fi
}

write_state() {
  umask 077
  mkdir -p "$(dirname "$STATE_FILE")"
  cat > "$STATE_FILE" <<EOF
TS=${TS}
ENV_SHA=${ENV_SHA}
EARLY_DB=${EARLY_DB}
RESTORE_DB=${RESTORE_DB}
EOF
  chmod 600 "$STATE_FILE"
}

read_state() {
  TS=""
  ENV_SHA=""
  EARLY_DB=""
  RESTORE_DB=""
  [[ -f "$STATE_FILE" ]] || return 0
  local line key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" == *=* ]] || continue
    key=${line%%=*}
    val=${line#*=}
    case "$key" in
      TS) TS=$val ;;
      ENV_SHA) ENV_SHA=$val ;;
      EARLY_DB) EARLY_DB=$val ;;
      RESTORE_DB) RESTORE_DB=$val ;;
    esac
  done < "$STATE_FILE"
}

# 用 SQLite backup API 把 src 复制到 dest。src 可以是正在写入的 WAL 库。
# 不 cp 主库 / -wal / -shm。
python_sqlite_backup() {
  python3 - "$1" "$2" <<'PY'
import os, sqlite3, sys
src_p, dest_p = sys.argv[1], sys.argv[2]
os.makedirs(os.path.dirname(dest_p), exist_ok=True)
src = sqlite3.connect(src_p, timeout=30)
dst = sqlite3.connect(dest_p)
try:
    src.backup(dst)
    chk = dst.execute("PRAGMA integrity_check").fetchone()[0]
finally:
    dst.close()
    src.close()
if chk != "ok":
    try:
        os.remove(dest_p)
    except OSError:
        pass
    raise SystemExit("integrity_check failed: %s" % chk)
PY
}

sqlite_cli_backup() {
  local src="$1" dest="$2" chk
  mkdir -p "$(dirname "$dest")"
  # .backup / .restore 走的是 SQLite online backup API，不是文件拷贝。
  sqlite3 "$src" ".backup ${dest}"
  chk=$(sqlite3 "$dest" "PRAGMA integrity_check;")
  if [[ "$chk" != "ok" ]]; then
    rm -f "$dest"
    echo "deploy: integrity_check failed: $chk" >&2
    return 1
  fi
}

container_sqlite_backup() {
  local dest="$1" base py chk
  base=$(basename "$dest")
  py=$(mktemp /tmp/pr-sqlite-backup.XXXXXX.py)
  cat > "$py" <<'PY'
import os, sqlite3, sys
src_p, dest_p = sys.argv[1], sys.argv[2]
src = sqlite3.connect(src_p, timeout=30)
dst = sqlite3.connect(dest_p)
try:
    src.backup(dst)
    chk = dst.execute("PRAGMA integrity_check").fetchone()[0]
finally:
    dst.close()
    src.close()
if chk != "ok":
    try:
        os.remove(dest_p)
    except OSError:
        pass
    raise SystemExit("integrity_check failed: %s" % chk)
PY
  docker cp "$py" paper-reader:/tmp/pr-sqlite-backup.py
  docker exec paper-reader python /tmp/pr-sqlite-backup.py /data/paper_reader.db "/tmp/${base}"
  mkdir -p "$(dirname "$dest")"
  docker cp "paper-reader:/tmp/${base}" "$dest"
  docker exec paper-reader rm -f "/tmp/${base}" /tmp/pr-sqlite-backup.py || true
  rm -f "$py"
  if command -v python3 >/dev/null 2>&1; then
    chk=$(python3 -c 'import sqlite3,sys; print(sqlite3.connect(sys.argv[1]).execute("PRAGMA integrity_check").fetchone()[0])' "$dest")
    [[ "$chk" == "ok" ]] || return 1
  fi
}

docker_ok() {
  command -v docker >/dev/null 2>&1
}

container_running() {
  [[ "${PAPER_READER_SKIP_DOCKER:-0}" == "1" ]] && return 1
  docker_ok || return 1
  [[ "$(docker inspect -f '{{.State.Running}}' paper-reader 2>/dev/null || echo false)" == "true" ]]
}

# 返回 0 成功，2 表示库文件不存在，1 表示备份失败。
backup_live_db() {
  local dest="$1" src="$DATA_DIR/paper_reader.db"
  if [[ ! -f "$src" ]]; then
    echo "deploy: 没有 $src" >&2
    return 2
  fi
  rm -f "$dest"
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sqlite3' >/dev/null 2>&1; then
    python_sqlite_backup "$src" "$dest" || return 1
  elif command -v sqlite3 >/dev/null 2>&1; then
    sqlite_cli_backup "$src" "$dest" || return 1
  elif container_running; then
    container_sqlite_backup "$dest" || return 1
  else
    echo "deploy: 没有 python3 / sqlite3，容器也没在跑。拒绝用 cp 拷贝正在使用的数据库。" >&2
    return 1
  fi
  [[ -f "$dest" ]] || return 1
  chmod 600 "$dest" || true
  echo "deploy: SQLite backup API -> $dest" >&2
}

enable_dotglob() {
  DOTGLOB_WAS=0
  NULLGLOB_WAS=0
  shopt -q dotglob && DOTGLOB_WAS=1
  shopt -q nullglob && NULLGLOB_WAS=1
  shopt -s dotglob nullglob
}

restore_dotglob() {
  [[ "${DOTGLOB_WAS:-0}" -eq 1 ]] || shopt -u dotglob
  [[ "${NULLGLOB_WAS:-0}" -eq 1 ]] || shopt -u nullglob
}

snapshot_code() {
  local ent base
  rm -rf "$PREV_DIR"
  mkdir -p "$PREV_DIR"
  enable_dotglob
  for ent in "$REMOTE_DIR"/*; do
    [[ -e "$ent" ]] || continue
    base=$(basename "$ent")
    # 生产数据在项目外。即便目录里误放了 data/，也不进回滚副本。
    [[ "$base" == "data" ]] && continue
    cp -a "$ent" "$PREV_DIR/"
  done
  restore_dotglob
  chmod 700 "$PREV_DIR" || true
  if [[ -f "$PREV_DIR/.env" ]]; then
    chmod 600 "$PREV_DIR/.env" || true
  fi
}

restore_code() {
  local ent base saved now expect
  [[ -d "$PREV_DIR" ]] || die "没有上一份代码 $PREV_DIR，无法回滚"
  if [[ ! -f "$REMOTE_DIR/.env" ]]; then
    if [[ -f "$PRESERVE" ]]; then
      install -m 600 "$PRESERVE" "$REMOTE_DIR/.env"
      echo "deploy: .env 丢失，已从保留副本放回（没有用别的内容覆盖）" >&2
    else
      die ".env 丢失，且没有保留副本，停止回滚以免留下空配置"
    fi
  fi
  umask 077
  saved=$(mktemp "${REMOTE_DIR}.env.held.XXXXXX")
  cp -a "$REMOTE_DIR/.env" "$saved"
  enable_dotglob
  for ent in "$REMOTE_DIR"/*; do
    [[ -e "$ent" ]] || continue
    base=$(basename "$ent")
    [[ "$base" == ".env" ]] && continue
    rm -rf "$ent"
  done
  for ent in "$PREV_DIR"/*; do
    [[ -e "$ent" ]] || continue
    base=$(basename "$ent")
    # 回滚代码时也不从 .prev 覆盖 .env，生产配置以保留副本为准。
    [[ "$base" == ".env" ]] && continue
    cp -a "$ent" "$REMOTE_DIR/"
  done
  restore_dotglob
  install -m 600 "$saved" "$REMOTE_DIR/.env"
  rm -f "$saved"
  if [[ -f "$PRESERVE" ]]; then
    now=$(env_sha "$REMOTE_DIR/.env")
    expect=$(env_sha "$PRESERVE")
    if [[ "$now" != "$expect" ]]; then
      install -m 600 "$PRESERVE" "$REMOTE_DIR/.env"
      now=$(env_sha "$REMOTE_DIR/.env")
      [[ "$now" == "$expect" ]] || die ".env 回滚后校验仍失败，已停止"
    fi
  fi
  echo "deploy: 代码已回到上一份，$REMOTE_DIR/.env 未改" >&2
}

ensure_env_intact() {
  local now expect
  [[ -f "$PRESERVE" ]] || die "缺少 .env 保留副本 $PRESERVE，拒绝继续"
  if [[ ! -f "$REMOTE_DIR/.env" ]]; then
    install -m 600 "$PRESERVE" "$REMOTE_DIR/.env"
  fi
  now=$(env_sha "$REMOTE_DIR/.env")
  expect=$(env_sha "$PRESERVE")
  if [[ "$now" != "$expect" || ( -n "$ENV_SHA" && "$now" != "$ENV_SHA" ) ]]; then
    echo "deploy: .env 校验和不匹配，正在从保留副本恢复，并且不会继续发布" >&2
    install -m 600 "$PRESERVE" "$REMOTE_DIR/.env"
    now=$(env_sha "$REMOTE_DIR/.env")
    [[ "$now" == "$expect" ]] || die ".env 恢复失败"
    return 1
  fi
  echo "deploy: .env 未变化" >&2
  return 0
}

stop_app() {
  [[ "${PAPER_READER_SKIP_DOCKER:-0}" == "1" ]] && return 0
  if container_running; then
    docker stop paper-reader >/dev/null
  fi
}

retag_previous() {
  local prev_id latest_id
  docker image inspect paper-reader:prev >/dev/null 2>&1 || return 1
  prev_id=$(docker image inspect -f '{{.Id}}' paper-reader:prev)
  if docker image inspect paper-reader:latest >/dev/null 2>&1; then
    latest_id=$(docker image inspect -f '{{.Id}}' paper-reader:latest)
    if [[ "$latest_id" != "$prev_id" ]]; then
      docker tag paper-reader:latest "paper-reader:failed-${TS:-manual}" || true
    fi
  fi
  docker tag paper-reader:prev paper-reader:latest
}

start_previous() {
  if docker image inspect paper-reader:prev >/dev/null 2>&1; then
    retag_previous || true
    docker compose --project-directory "$REMOTE_DIR" up -d --no-build --force-recreate
  else
    echo "deploy: 没有 paper-reader:prev，用上一份代码在 VPS 上重新构建" >&2
    docker compose --project-directory "$REMOTE_DIR" up -d --build --force-recreate
  fi
}

# 把备份快照写回 paper_reader.db。调用方必须已经停掉容器。
# 源文件是 backup API 生成的静止快照，不是正在写的 WAL。
restore_db() {
  local backup="$1" db wal shm staging
  if [[ -z "$backup" || ! -f "$backup" ]]; then
    echo "deploy: 没有可恢复的备份，跳过数据库回滚" >&2
    return 0
  fi
  stop_app
  db="$DATA_DIR/paper_reader.db"
  wal="${db}-wal"
  shm="${db}-shm"
  staging="${db}.restoring"
  rm -f "$staging"
  if command -v python3 >/dev/null 2>&1 && python3 -c 'import sqlite3' >/dev/null 2>&1; then
    python_sqlite_backup "$backup" "$staging"
  elif command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$staging" ".restore ${backup}"
    local chk
    chk=$(sqlite3 "$staging" "PRAGMA integrity_check;")
    [[ "$chk" == "ok" ]] || die "恢复后的数据库 integrity_check=$chk（原库未替换，备份仍在 $backup）"
  else
    die "无法恢复数据库：没有 python3 或 sqlite3。备份仍在 $backup，容器保持停止。"
  fi
  rm -f "$wal" "$shm"
  mv -f "$staging" "$db"
  rm -f "$wal" "$shm"
  if ! chown 10001:10001 "$db"; then
    if [[ "$(id -u)" -eq 0 ]]; then
      die "chown 10001:10001 $db 失败。备份仍在 $backup"
    fi
    echo "deploy: 当前不是 root，跳过 chown 10001:10001（生产环境请用 root 执行）" >&2
  fi
  echo "deploy: 已从 $backup 恢复数据库" >&2
}

rollback_before_up() {
  IN_ROLLBACK=1
  restore_code
  if docker_ok; then
    retag_previous || echo "deploy: 没有上一版镜像可指回" >&2
  fi
  echo "deploy: 已回滚代码和镜像标签。新容器没启动，数据库保持原样。" >&2
}

rollback_after_up() {
  IN_ROLLBACK=1
  read_state
  stop_app
  restore_code
  if docker_ok; then
    retag_previous || echo "deploy: 没有上一版镜像，回滚后会用旧代码重新构建" >&2
  fi
  if [[ -f "$MARKER" ]]; then
    restore_db "$RESTORE_DB"
    rm -f "$MARKER"
  fi
  if docker_ok; then
    start_previous
  fi
  echo "deploy: 已回滚到上一版代码和镜像。" >&2
}

cmd_rollback_auto() {
  IN_ROLLBACK=1
  read_state
  if [[ -f "$MARKER" ]]; then
    rollback_after_up
  else
    rollback_before_up
  fi
}

on_remote_err() {
  local rc=$?
  if [[ "$IN_ROLLBACK" -eq 1 ]]; then
    exit "$rc"
  fi
  echo "deploy: 远端步骤意外失败 (rc=$rc)，开始回滚" >&2
  cmd_rollback_auto || true
  exit "$rc"
}

cmd_prepare() {
  local rc
  [[ -d "$REMOTE_DIR" ]] || die "VPS 上没有代码目录 $REMOTE_DIR"
  [[ -f "$REMOTE_DIR/docker-compose.yml" ]] || die "$REMOTE_DIR 里没有 docker-compose.yml"
  [[ -f "$REMOTE_DIR/.env" ]] || die "VPS 上没有 $REMOTE_DIR/.env，拒绝部署（不会新建或覆盖生产配置）"
  umask 077
  install -m 600 "$REMOTE_DIR/.env" "$PRESERVE"
  ENV_SHA=$(env_sha "$REMOTE_DIR/.env")
  TS=$(date +%Y%m%d-%H%M%S)
  mkdir -p "$DATA_DIR/backups"
  EARLY_DB="$DATA_DIR/backups/paper_reader-deploy-${TS}.db"
  RESTORE_DB=""
  set +e
  backup_live_db "$EARLY_DB"
  rc=$?
  set -e
  if [[ "$rc" -eq 2 ]]; then
    EARLY_DB=""
    if [[ "${PAPER_READER_ALLOW_NO_DB:-0}" != "1" ]]; then
      die "没有 $DATA_DIR/paper_reader.db，拒绝在未备份的情况下部署。确认是空库安装时可设 PAPER_READER_ALLOW_NO_DB=1。"
    fi
    echo "deploy: 跳过数据库备份" >&2
  elif [[ "$rc" -ne 0 ]]; then
    die "部署前备份失败，尚未同步代码，也没有改 .env。"
  fi
  if docker image inspect paper-reader:latest >/dev/null 2>&1; then
    docker tag paper-reader:latest paper-reader:prev
  else
    echo "deploy: 警告: 没有 paper-reader:latest。失败时只能用上一份代码在 VPS 上重新构建。" >&2
  fi
  snapshot_code
  write_state
  echo "deploy: prepare ok ts=$TS backup=${EARLY_DB:-none}" >&2
}

cmd_release() {
  local rc dest
  trap on_remote_err ERR
  read_state
  [[ -n "$TS" ]] || die "没有部署状态 $STATE_FILE，请先跑 prepare"
  if ! ensure_env_intact; then
    rollback_before_up
    exit 10
  fi
  if ! docker compose --project-directory "$REMOTE_DIR" build; then
    echo "deploy: 构建失败，回滚代码和镜像标签（不重启仍在服务的旧容器，不动数据库）" >&2
    rollback_before_up
    exit 10
  fi
  # 构建期间旧容器仍在写库。启动新版本前再做一次 backup API，作为失败时的恢复点。
  dest="$DATA_DIR/backups/paper_reader-deploy-${TS}-pre-up.db"
  set +e
  backup_live_db "$dest"
  rc=$?
  set -e
  if [[ "$rc" -eq 0 ]]; then
    RESTORE_DB="$dest"
    write_state
  elif [[ "$rc" -eq 2 && "${PAPER_READER_ALLOW_NO_DB:-0}" == "1" ]]; then
    RESTORE_DB=""
    write_state
  else
    echo "deploy: 启动前备份失败，不启动新容器" >&2
    rollback_before_up
    exit 10
  fi
  # 标记从这里开始：新容器可能写过库，失败时要恢复 RESTORE_DB。
  : > "$MARKER"
  chmod 600 "$MARKER" || true
  if ! docker compose --project-directory "$REMOTE_DIR" up -d; then
    echo "deploy: 启动失败，回滚代码、镜像，并恢复启动前备份" >&2
    rollback_after_up
    exit 11
  fi
  if ! health_check; then
    echo "deploy: 健康检查失败，回滚代码、镜像，并恢复启动前备份" >&2
    rollback_after_up
    exit 11
  fi
  rm -f "$MARKER" "$PRESERVE"
  trap - ERR
  echo "deploy: 发布成功 ts=$TS" >&2
}

health_body_ok() {
  local body="$1"
  if command -v python3 >/dev/null 2>&1; then
    printf '%s' "$body" | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if d.get("ok") is True else 1)' && return 0
    return 1
  fi
  printf '%s' "$body" | grep -Eq '"ok"[[:space:]]*:[[:space:]]*true'
}

health_check() {
  local i=0 body
  while [[ "$i" -lt 30 ]]; do
    i=$((i + 1))
    body=""
    if command -v curl >/dev/null 2>&1; then
      body=$(curl -fsS --max-time 4 "$HEALTH_URL" 2>/dev/null) || body=""
    elif command -v python3 >/dev/null 2>&1; then
      body=$(python3 -c 'import sys,urllib.request
try:
    sys.stdout.write(urllib.request.urlopen(sys.argv[1], timeout=4).read().decode())
except Exception:
    raise SystemExit(1)
' "$HEALTH_URL" 2>/dev/null) || body=""
    else
      die "VPS 上没有 curl 或 python3，无法做健康检查"
    fi
    if [[ -n "$body" ]] && health_body_ok "$body"; then
      echo "deploy: 健康检查通过 $HEALTH_URL" >&2
      return 0
    fi
    if [[ "$i" -eq 1 || $((i % 5)) -eq 0 ]]; then
      echo "deploy: 等待健康检查 ($i/30) $HEALTH_URL" >&2
    fi
    sleep 2
  done
  echo "deploy: 健康检查失败 $HEALTH_URL" >&2
  return 1
}

remote_main() {
  umask 077
  load_remote_config
  case "${1:-}" in
    prepare) cmd_prepare ;;
    release) cmd_release ;;
    rollback-auto) cmd_rollback_auto ;;
    *) die "未知远端阶段: ${1:-}" ;;
  esac
}

print_dry_run() {
  local root="$1" remote="$2"
  cat <<EOF
deploy: dry-run（不连接服务器，不修改任何文件，不读取、不上传、不覆盖 .env）

源码:     $root
目标:     ${remote}:${REMOTE_DIR}
数据:     $DATA_DIR
健康检查: $HEALTH_URL
架构:     只在 VPS (amd64) 上 docker compose build。不会从本机 docker save / 拷贝镜像。
          本机若是 Mac (arm64)，这一点是必须的。

不会碰 sing-box、代理端口 443/udp、8443/tcp、2083/tcp、proxy-sub、ufw 既有规则、证书、Cloudflare。
正式部署需要本机有 ssh 和 rsync。SSH 目标可用 --host 或 PAPER_READER_SSH 覆盖。

计划步骤:
  1. SSH 到 VPS，确认 ${REMOTE_DIR}/.env 存在。
     复制到 ${PRESERVE}（权限 600）并记录 sha256。不打印 .env 内容。
  2. 用 SQLite backup API 备份 ${DATA_DIR}/paper_reader.db
     -> ${DATA_DIR}/backups/paper_reader-deploy-<时间>.db
     不 cp 正在使用的主库、-wal、-shm。没有这只库就中止，除非 PAPER_READER_ALLOW_NO_DB=1。
  3. docker tag paper-reader:latest paper-reader:prev
  4. 把代码目录复制到 ${PREV_DIR}（不含 data/）。.env 只留在 VPS 上。
  5. rsync 源码到 VPS，--delete，并排除:
     .env  .git  data/  .venv/  venv/  __pycache__/  *.pyc  tools/out/  .DS_Store  *.bak-*
     排除项不会被 --delete 删掉，所以 VPS 上的 .env 既不会被上传覆盖，也不会被删。
     同步前再用 rsync -ani 预检，若列表里出现 .env 就中止。
  6. 比对 .env 的 sha256 与保留副本。不一致则从保留副本恢复、代码回滚，并且不构建。
  7. 在 VPS 上 docker compose build（旧容器继续服务）。
     构建成功后、启动新容器前，再做一次 SQLite backup API:
     -> ${DATA_DIR}/backups/paper_reader-deploy-<时间>-pre-up.db
     这是失败时自动恢复用的那一份（含构建期间旧版本的写入）。
  8. 在 VPS 上 docker compose up -d
  9. 在 VPS 上轮询 ${HEALTH_URL}（最多约 60 秒），JSON 里 ok 必须为 true。
     不走 nginx，也不走 Cloudflare。

失败时:
  - 新容器还没起来（同步失败、构建失败、启动前备份失败）:
    代码从 ${PREV_DIR} 换回，镜像标签指回 paper-reader:prev。
    不重启仍在服务的旧容器，不恢复数据库。
  - 新容器已经尝试启动，或健康检查失败:
    docker stop paper-reader（只停这个容器），
    换回代码和 paper-reader:prev，
    用启动前那份 backup API 快照恢复 paper_reader.db（并删掉残留的 -wal/-shm），
    再 docker compose up -d --no-build。
    本次构建出的镜像会另外标成 paper-reader:failed-<时间>，方便排查。
  - SSH 在发布中途断开: 再执行一次远端 rollback-auto。
    只有 ${MARKER} 存在（新容器已经尝试启动）时才会动数据库备份。

成功后删除 ${PRESERVE} 和启动标记。${PREV_DIR} 与 backups/ 里的快照留在 VPS 上，供人工再滚一次。
人工再滚数据库时：先 docker stop paper-reader，再用 sqlite3 .restore 或 python
Connection.backup() 把 paper_reader-deploy-<时间>-pre-up.db 写回，然后 chown 10001:10001，
删掉 -wal/-shm，最后 up -d --no-build。不要 cp 一个还开着 WAL 的库。
EOF
}

rsync_preview_safe() {
  local remote="$1" root="$2" preview bad
  preview=$(rsync -ani --delete "${RSYNC_EXCLUDES[@]}" "$root/" "${remote}:${REMOTE_DIR}/") || die "rsync 预检失败"
  bad=$(printf '%s\n' "$preview" | awk '
    NF == 0 { next }
    {
      path = $NF
      if (path == ".env" || path ~ /(^|\/)\.env$/) print
    }
  ')
  if [[ -n "$bad" ]]; then
    echo "$bad" >&2
    die "rsync 预检会改动 .env，已中止，没有同步"
  fi
}

ssh_worker() {
  local phase="$1" script_path="$2" remote="$3" remote_cmd
  remote_cmd=$(printf 'PAPER_READER_REMOTE_DIR=%q PAPER_READER_DATA_DIR=%q PAPER_READER_HEALTH_URL=%q bash -s -- --remote-worker %q' \
    "$REMOTE_DIR" "$DATA_DIR" "$HEALTH_URL" "$phase")
  # remote_cmd 已用 printf %q 转义，这里就是要在本机展开后再交给 ssh。
  # shellcheck disable=SC2029
  ssh "${SSH_OPTS[@]}" "$remote" "$remote_cmd" < "$script_path"
}

self_test() {
  local tmp root remote data src dest sha_before sha_after body wal
  tmp=$(mktemp -d)
  # 路径在设置 trap 时就写死，避免函数返回后 local 变量消失。
  # shellcheck disable=SC2064
  trap "rm -rf $(printf '%q' "$tmp")" EXIT
  export PAPER_READER_SKIP_DOCKER=1
  root="$tmp/src"
  remote="$tmp/opt/paper-reader"
  data="$tmp/opt/paper-reader-data"
  mkdir -p "$root" "$remote" "$data/backups" "$root/app"
  echo "old" > "$remote/app.txt"
  echo "SECRET_SENTINEL=keep" > "$remote/.env"
  chmod 600 "$remote/.env"
  echo "compose" > "$remote/docker-compose.yml"
  export PAPER_READER_REMOTE_DIR="$remote"
  export PAPER_READER_DATA_DIR="$data"
  export PAPER_READER_HEALTH_URL="http://127.0.0.1:9/api/health"
  load_remote_config
  # 最后一个连接关闭时 SQLite 会 checkpoint 并删掉 -wal。
  # 所以要留一个连接开着，已提交的行才只存在于 WAL 里。
  cat > "$tmp/hold.py" <<'PY'
import os, sqlite3, sys, time
db, flag = sys.argv[1], sys.argv[2]
c = sqlite3.connect(db)
c.execute("PRAGMA journal_mode=WAL")
c.execute("PRAGMA wal_autocheckpoint=0")
c.execute("CREATE TABLE t(v INTEGER)")
c.execute("INSERT INTO t VALUES (1)")
c.commit()
open(flag + ".ready", "w").close()
while not os.path.exists(flag):
    time.sleep(0.05)
c.close()
PY
  python3 "$tmp/hold.py" "$data/paper_reader.db" "$tmp/hold" &
  holder_pid=$!
  # pid 和临时目录都在此刻展开。
  # shellcheck disable=SC2064
  trap "kill ${holder_pid} >/dev/null 2>&1 || true; rm -rf $(printf '%q' "$tmp")" EXIT
  i=0
  while [[ ! -f "$tmp/hold.ready" ]]; do
    i=$((i + 1))
    [[ "$i" -gt 100 ]] && die "self-test: 数据库持有进程没有就绪"
    sleep 0.05
  done
  wal="$data/paper_reader.db-wal"
  [[ -s "$wal" ]] || die "self-test: 预期存在未 checkpoint 的 WAL"
  # 只复制主库文件时看不到 WAL 里已提交的行。backup API 必须看得到。
  python3 - "$data/paper_reader.db" "$data/backups/main-only.db" <<'PY'
import shutil, sqlite3, sys
src, dest = sys.argv[1], sys.argv[2]
shutil.copy(src, dest)
c = sqlite3.connect(dest)
try:
    vals = list(c.execute("select v from t"))
except sqlite3.OperationalError:
    raise SystemExit(0)
if vals == [(1,)]:
    raise SystemExit("main-file copy saw the uncheckpointed row")
PY
  EARLY_DB="$data/backups/snap.db"
  backup_live_db "$EARLY_DB" >/dev/null
  python3 - "$EARLY_DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
vals = [r[0] for r in c.execute("select v from t")]
chk = c.execute("PRAGMA integrity_check").fetchone()[0]
if chk != "ok" or vals != [1]:
    raise SystemExit("backup contents %s integrity %s" % (vals, chk))
PY
  touch "$tmp/hold"
  wait "$holder_pid"
  python3 - "$data/paper_reader.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute("INSERT INTO t VALUES (2)")
c.commit()
c.close()
PY
  restore_db "$EARLY_DB" >/dev/null
  python3 - "$data/paper_reader.db" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
vals = [r[0] for r in c.execute("select v from t")]
if vals != [1]:
    raise SystemExit("restore got %s" % vals)
PY
  echo "self-test ok: backup API 读到未 checkpoint 的 WAL，恢复后丢掉备份之后的写入"

  install -m 600 "$remote/.env" "$PRESERVE"
  ENV_SHA=$(env_sha "$remote/.env")
  sha_before=$ENV_SHA
  snapshot_code
  echo "SECRET_SENTINEL=overwritten" > "$remote/.env"
  echo "new" > "$remote/app.txt"
  echo "extra" > "$remote/extra.txt"
  if ensure_env_intact; then
    die "self-test: 改写 .env 后应该检测失败"
  fi
  sha_after=$(env_sha "$remote/.env")
  [[ "$sha_after" == "$sha_before" ]] || die "self-test: .env 没有从保留副本恢复"
  restore_code >/dev/null
  [[ "$(cat "$remote/app.txt")" == "old" ]] || die "self-test: 代码没有回到上一份"
  [[ ! -f "$remote/extra.txt" ]] || die "self-test: 回滚后仍留着新文件"
  sha_after=$(env_sha "$remote/.env")
  [[ "$sha_after" == "$sha_before" ]] || die "self-test: 回滚代码时改动了 .env"
  echo "self-test ok: .env 被改动能恢复，代码回滚不会覆盖 .env"

  if command -v rsync >/dev/null 2>&1; then
    src="$tmp/rsync-src"
    dest="$tmp/rsync-dest"
    mkdir -p "$src/app" "$dest"
    echo "SECRET_SENTINEL=dest" > "$dest/.env"
    chmod 600 "$dest/.env"
    echo "gone" > "$dest/old.txt"
    echo "ship" > "$src/app/main.py"
    rsync -a --delete "${RSYNC_EXCLUDES[@]}" "$src/" "$dest/"
    [[ "$(cat "$dest/.env")" == "SECRET_SENTINEL=dest" ]] || die "self-test: rsync 覆盖了 .env"
    [[ -f "$dest/app/main.py" ]] || die "self-test: rsync 没有同步代码"
    [[ ! -f "$dest/old.txt" ]] || die "self-test: rsync --delete 没有删掉旧文件"
    echo "self-test ok: rsync 排除 .env，同时仍会 --delete 其它旧文件"
  else
    echo "self-test: 本机没有 rsync，跳过同步排除检查" >&2
  fi

  body='{"ok": true, "llm_ready": false}'
  health_body_ok "$body" || die "self-test: ok=true 应通过"
  body='{"ok": false}'
  if health_body_ok "$body"; then
    die "self-test: ok=false 不应通过"
  fi
  echo "self-test ok: 健康检查只认 JSON ok=true"

  if grep -E '^[^#]*\bcp\b[^#]*paper_reader\.db' "$0" >/dev/null 2>&1; then
    die "self-test: 脚本里出现了针对 paper_reader.db 的 cp"
  fi
  echo "self-test ok: 没有用 cp 复制数据库文件"
  echo "self-test passed"
}

main() {
  local dry=0 self=0 remote root script_path rc
  remote=${PAPER_READER_SSH:-root@23.27.0.190}
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) dry=1; shift ;;
      --self-test) self=1; shift ;;
      --host)
        [[ $# -ge 2 ]] || die "--host 需要 user@host"
        remote=$2
        shift 2
        ;;
      --host=*) remote=${1#*=}; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "未知参数: $1（可用 --help）" ;;
    esac
  done
  if [[ "$self" -eq 1 ]]; then
    self_test
    exit 0
  fi
  load_remote_config
  root=$(cd "$(dirname "$0")/.." && pwd)
  script_path=$(cd "$(dirname "$0")" && pwd)/$(basename "$0")
  [[ -f "$root/docker-compose.yml" ]] || die "找不到 $root/docker-compose.yml"
  [[ -f "$root/Dockerfile" ]] || die "找不到 $root/Dockerfile"
  if git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if [[ -n "$(git -C "$root" status --porcelain)" ]]; then
      echo "deploy: 警告: 工作区有未提交改动，正式部署会按工作区内容同步" >&2
    fi
  fi
  if [[ -f "$root/.env" ]]; then
    echo "deploy: 本机 .env 不会上传，也不会用来覆盖 VPS" >&2
  fi
  if [[ "$dry" -eq 1 ]]; then
    print_dry_run "$root" "$remote"
    exit 0
  fi
  command -v ssh >/dev/null 2>&1 || die "本机没有 ssh"
  command -v rsync >/dev/null 2>&1 || die "本机没有 rsync"
  echo "deploy: 开始发布 -> ${remote}:${REMOTE_DIR}" >&2
  ssh_worker prepare "$script_path" "$remote"
  rsync_preview_safe "$remote" "$root"
  if ! rsync -az --delete "${RSYNC_EXCLUDES[@]}" "$root/" "${remote}:${REMOTE_DIR}/"; then
    echo "deploy: rsync 失败，回滚 VPS 上的代码目录" >&2
    ssh_worker rollback-auto "$script_path" "$remote" || true
    exit 1
  fi
  set +e
  ssh_worker release "$script_path" "$remote"
  rc=$?
  set -e
  case "$rc" in
    0) echo "deploy: 完成。请再跑一遍 AGENT_ONBOARDING.md 第 8.2 节的人工检查。" >&2 ;;
    10)
      echo "deploy: 发布在新容器启动前失败，代码和镜像标签已回滚，数据库未恢复。" >&2
      exit 1
      ;;
    11)
      echo "deploy: 发布在新容器启动后失败，已回滚代码、镜像，并恢复启动前的数据库备份。" >&2
      exit 1
      ;;
    *)
      echo "deploy: 远端发布没有正常返回 (rc=$rc)，再执行一次自动回滚。" >&2
      ssh_worker rollback-auto "$script_path" "$remote" || true
      exit 1
      ;;
  esac
}

if [[ "${1:-}" == "--remote-worker" ]]; then
  remote_main "${2:-}"
  exit $?
fi

main "$@"

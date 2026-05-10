#!/usr/bin/env bash
# corlinman remote-server bootstrap.
#
# Usage on the target server (Linux + Docker preinstalled):
#   1. scp deploy/server-bootstrap.sh + deploy/config.toml.template +
#      deploy/.env.template to /root/corlinman-deploy/
#   2. ssh in, cd /root/corlinman-deploy
#   3. ./server-bootstrap.sh
#
# This script is idempotent — re-running it brings up the latest image
# without wiping volumes (.napcat session, ~/.corlinman/data, evolution
# proposals).

set -euo pipefail

REPO_ROOT=${REPO_ROOT:-/root/corlinman}
DEPLOY_DIR=${DEPLOY_DIR:-/root/corlinman-deploy}
DATA_DIR=${DATA_DIR:-$HOME/.corlinman}
BRANCH=${BRANCH:-main}
COMPOSE_PROFILE=${COMPOSE_PROFILE:-qq}

log() {
  printf '\033[1;36m[bootstrap]\033[0m %s\n' "$*"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "missing required command: $1" >&2
    exit 1
  }
}

# --- preflight --------------------------------------------------------------
log "preflight: checking docker + git + curl"
require_cmd git
require_cmd curl
require_cmd docker
docker compose version >/dev/null || {
  echo "docker compose plugin not found; install docker-compose-plugin" >&2
  exit 1
}

# --- repo clone / update ----------------------------------------------------
if [ -d "$REPO_ROOT/.git" ]; then
  log "repo exists at $REPO_ROOT — fetching $BRANCH"
  git -C "$REPO_ROOT" fetch origin "$BRANCH" --quiet
  git -C "$REPO_ROOT" checkout "$BRANCH" --quiet
  git -C "$REPO_ROOT" reset --hard "origin/$BRANCH" --quiet
else
  log "cloning corlinman to $REPO_ROOT"
  git clone --depth 1 --branch "$BRANCH" \
    https://github.com/ymylive/corlinman.git "$REPO_ROOT"
fi

# --- data dir ---------------------------------------------------------------
mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR"

# --- config materialise -----------------------------------------------------
if [ ! -f "$DATA_DIR/config.toml" ]; then
  if [ -f "$DEPLOY_DIR/config.toml.template" ]; then
    log "writing $DATA_DIR/config.toml from template"
    cp "$DEPLOY_DIR/config.toml.template" "$DATA_DIR/config.toml"
    chmod 600 "$DATA_DIR/config.toml"
  else
    echo "config.toml.template not found in $DEPLOY_DIR — abort" >&2
    exit 1
  fi
else
  log "$DATA_DIR/config.toml already present — leaving untouched (manual edit OK)"
fi

# --- .env materialise -------------------------------------------------------
if [ ! -f "$REPO_ROOT/.env" ]; then
  if [ -f "$DEPLOY_DIR/.env.template" ]; then
    log "writing $REPO_ROOT/.env from template"
    cp "$DEPLOY_DIR/.env.template" "$REPO_ROOT/.env"
    chmod 600 "$REPO_ROOT/.env"
  else
    echo ".env.template not found in $DEPLOY_DIR — abort" >&2
    exit 1
  fi
  cat <<HINT

  ⚠️  Edit $REPO_ROOT/.env before first start:
       - OPENAI_API_KEY  (or)  GEMINI_API_KEY  (at least one required)
       - ADMIN_BOOTSTRAP_TOKEN  (rotate immediately after first /admin login)

HINT
fi

# --- compose up -------------------------------------------------------------
cd "$REPO_ROOT/docker/compose"

# corlinman image (`ghcr.io/ymylive/corlinman:dev` in docker-compose.yml) is
# a placeholder that hasn't been pushed; we always build locally. NapCat
# image IS published — pull only that one.
log "pulling napcat image"
docker compose -f docker-compose.yml -f docker-compose.qq.yml \
  --profile "$COMPOSE_PROFILE" pull napcat || true

log "building corlinman image (~10-20 min on first run; cached after)"
docker compose -f docker-compose.yml -f docker-compose.qq.yml \
  --profile "$COMPOSE_PROFILE" build corlinman

log "starting services (profile=$COMPOSE_PROFILE)"
docker compose -f docker-compose.yml -f docker-compose.qq.yml \
  --profile "$COMPOSE_PROFILE" up -d

# --- post-up hints ----------------------------------------------------------
PUB_IP=$(curl -fsS https://ipinfo.io/ip 2>/dev/null || echo "<server-ip>")
log "services up. quick-checks:"
cat <<INFO

  • Health         : curl http://${PUB_IP}:6005/health
  • Admin UI       : http://${PUB_IP}:6005/admin
                     bootstrap token = value of ADMIN_BOOTSTRAP_TOKEN in $REPO_ROOT/.env
  • NapCat WebUI   : http://${PUB_IP}:6099  (scan QR there to log in)
  • OneBot WS      : ws://${PUB_IP}:3001    (corlinman dials it internally)

  Tail logs:
    docker logs -f corlinman
    docker logs -f corlinman-napcat
INFO

log "done"

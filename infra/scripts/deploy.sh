#!/usr/bin/env bash
#
# infra/scripts/deploy.sh
#
# Run this LOCALLY on the developer's machine (not on the EC2 instance).
# It does NOT ssh in — the EC2 instance's Security Group has no port 22
# and no key pair (see openspec/changes/deploy-django-app/design.md,
# decision 7). Instead it builds a remote shell script and ships it to the
# instance via `aws ssm send-command` (document AWS-RunShellScript), then
# polls `aws ssm get-command-invocation` until the command reaches a
# terminal status, printing the remote stdout/stderr and mirroring the
# remote outcome in this script's own exit code.
#
# Remote-side logic (executed ON the EC2 instance by SSM) — rewritten per
# design.md decision 5/7 (task 10.2): EC2 no longer needs git, a repo
# checkout, or any GitHub credentials. It only pulls a pre-built image from
# GHCR (built+pushed by GitHub Actions, see .github/workflows/build-push.yml
# / task 9.1) and runs it.
#   1. Fail fast if /opt/jiu-sync-backend/.env does not exist. Secrets are
#      created manually by a human via a one-off SSM session (design.md
#      decision 6) — this script only checks for the file, it never creates
#      it or fills in defaults. (Unchanged from the original git-based
#      version, previously verified live in task 5.1.)
#   2. Write infra/docker/docker-compose.prod.yml's *current local* content
#      to /opt/jiu-sync-backend/docker-compose.prod.yml on the instance, via
#      base64 embedded in the SSM command (design.md decision 5, same
#      technique used for .env in an earlier manual step) — no git involved.
#      NOTE: the source file's `env_file: [../../.env]` entries assume the
#      compose file sits two directories below the repo root (as it does
#      locally, at infra/docker/). Since it is shipped here flat, directly
#      into /opt/jiu-sync-backend/ alongside .env, that relative path is
#      rewritten to `.env` in transit (local sed, below) — the file on disk
#      in this repo is never modified. Verified locally with `docker compose
#      config`: the unmodified `../../.env` path resolves relative to the
#      flat remote location and fails with "env file ... not found"; the
#      rewritten `.env` path resolves correctly.
#   3. `docker login ghcr.io` using GHCR_USERNAME/GHCR_TOKEN, read from the
#      two new lines a human adds to the EC2-side .env (design.md decision
#      6/task 10.3 — not created by this script). Extracted with grep/cut
#      rather than `source`-ing the whole .env, so values elsewhere in the
#      file containing shell-special characters ($, quotes, backticks — e.g.
#      DATABASE_URL) are never evaluated as shell syntax.
#   4. docker compose pull / up -d / migrate / collectstatic. Re-running
#      this is safe: `docker pull` is a no-op when the digest is unchanged,
#      and `up -d` doesn't restart services with no image/config change
#      (design.md decision 8 — idempotency for free).
#
# Usage: ./infra/scripts/deploy.sh

set -euo pipefail

# --- Local configuration -----------------------------------------------
INSTANCE_ID="i-0f6d5dc974e91bbf6"
REGION="ap-northeast-3"
REPO_DIR="/opt/jiu-sync-backend"          # target dir on the EC2 instance
COMPOSE_FILE_LOCAL="infra/docker/docker-compose.prod.yml"
ENV_FILE="${REPO_DIR}/.env"
SSM_TIMEOUT_SECONDS=900    # no more `docker compose build` on the instance
                           # (moved to GitHub Actions, design.md decision 5)
                           # — pull + up + migrate + collectstatic is far
                           # lighter, but kept generous for a t2.micro.
POLL_INTERVAL_SECONDS=5
POLL_MAX_ATTEMPTS=200      # ~16.5 min at 5s/poll, comfortably above SSM_TIMEOUT_SECONDS

command -v aws >/dev/null 2>&1 || { echo "ERROR: aws CLI not found in PATH." >&2; exit 1; }
command -v jq  >/dev/null 2>&1 || { echo "ERROR: jq not found in PATH." >&2; exit 1; }

if [ ! -f "$COMPOSE_FILE_LOCAL" ]; then
  echo "ERROR: ${COMPOSE_FILE_LOCAL} not found. Run this script from the repo root." >&2
  exit 1
fi

# Rewrite the compose file's env_file entries for the flat remote layout
# (see NOTE in the header comment above). Only touches the two YAML list
# items themselves (`      - ../../.env`), not the prose in the file's
# header comments that also happens to mention that path.
COMPOSE_B64=$(sed -E 's#^([[:space:]]*-[[:space:]]+)\.\./\.\./\.env[[:space:]]*$#\1.env#' "$COMPOSE_FILE_LOCAL" | base64 | tr -d '\n')

# --- Build the remote shell script --------------------------------------
# Everything inside this heredoc runs ON THE EC2 INSTANCE, not locally.
# Local vars are interpolated here (unquoted heredoc) so the constants above
# stay the single source of truth; `\$` escapes protect the parts that must
# be evaluated remotely instead.
REMOTE_SCRIPT=$(cat <<EOF
#!/bin/bash
# SSM's AWS-RunShellScript document runs commands through /bin/sh (dash) by
# default, which doesn't understand \`pipefail\`; the shebang above makes the
# agent execute this script with bash instead.
set -euo pipefail

# SSM's AWS-RunShellScript document executes commands without a login shell,
# so \$HOME is unset here even though the instance is running as root. Git no
# longer runs on this instance, but \`docker login\` still needs \$HOME to
# find/write its credential store (~/.docker/config.json by default) — set
# it explicitly from the current user's passwd entry before anything that
# relies on it.
export HOME="\$(getent passwd "\$(whoami)" | cut -d: -f6)"

ENV_FILE="${ENV_FILE}"
REPO_DIR="${REPO_DIR}"
COMPOSE_FILE="\${REPO_DIR}/docker-compose.prod.yml"
COMPOSE_B64="${COMPOSE_B64}"

echo "=== [1/7] Checking for required .env at \${ENV_FILE} ==="
if [ ! -f "\${ENV_FILE}" ]; then
  echo "ERROR: \${ENV_FILE} does not exist on this instance." >&2
  echo "The production .env must be created manually via a one-off SSM session" >&2
  echo "before deploying (see openspec/changes/deploy-django-app design.md decision 6)." >&2
  echo "Aborting — not attempting any docker steps." >&2
  exit 1
fi
echo "OK: .env found."

echo "=== [2/7] Writing docker-compose.prod.yml to \${COMPOSE_FILE} ==="
echo "\${COMPOSE_B64}" | base64 -d > "\${COMPOSE_FILE}"
echo "OK: compose file written."

echo "=== [3/7] Extracting GHCR_USERNAME/GHCR_TOKEN from \${ENV_FILE} ==="
# Extracted with grep/cut rather than \`source\`-ing the whole .env: other
# values in that file (e.g. DATABASE_URL) may contain characters (\$, quotes,
# backticks) that bash would try to evaluate if the file were sourced
# directly. This only reads the two lines we need, literally.
extract_env_var() {
  local key="\$1" file="\$2" line value len first last
  line=\$(grep -m1 "^\${key}=" "\${file}" || true)
  if [ -z "\${line}" ]; then
    echo ""
    return 0
  fi
  value="\${line#*=}"
  # Strip one layer of matching surrounding quotes, if present (avoids
  # bracket-glob quote escaping, which is fragile once this whole function
  # is itself embedded inside a local heredoc).
  len="\${#value}"
  if [ "\${len}" -ge 2 ]; then
    first="\${value:0:1}"
    last="\${value:\$((len - 1)):1}"
    if { [ "\${first}" = '"' ] && [ "\${last}" = '"' ]; } || { [ "\${first}" = "'" ] && [ "\${last}" = "'" ]; }; then
      value="\${value:1:\$((len - 2))}"
    fi
  fi
  echo "\${value}"
}

GHCR_USERNAME="\$(extract_env_var GHCR_USERNAME "\${ENV_FILE}")"
GHCR_TOKEN="\$(extract_env_var GHCR_TOKEN "\${ENV_FILE}")"

if [ -z "\${GHCR_USERNAME}" ] || [ -z "\${GHCR_TOKEN}" ]; then
  echo "ERROR: GHCR_USERNAME and/or GHCR_TOKEN not set in \${ENV_FILE}." >&2
  echo "These must be added manually before deploy.sh can log in to GHCR" >&2
  echo "(see openspec/changes/deploy-django-app tasks.md task 10.3)." >&2
  echo "Aborting — not attempting docker login/pull/up." >&2
  exit 1
fi
echo "OK: GHCR_USERNAME/GHCR_TOKEN present."

echo "=== [4/7] docker login ghcr.io ==="
docker login ghcr.io -u "\${GHCR_USERNAME}" -p "\${GHCR_TOKEN}"

echo "=== [5/7] docker compose pull ==="
docker compose -f "\${COMPOSE_FILE}" pull

echo "=== [6/7] docker compose up -d ==="
docker compose -f "\${COMPOSE_FILE}" up -d

echo "=== [7/7] Running migrations and collecting static files ==="
docker compose -f "\${COMPOSE_FILE}" exec -T app python manage.py migrate
docker compose -f "\${COMPOSE_FILE}" exec -T app python manage.py collectstatic --noinput

echo "=== Deploy finished successfully ==="
EOF
)

# --- Ship it via SSM -----------------------------------------------------
echo "Sending deploy command to instance ${INSTANCE_ID} via SSM..."

PARAMS_JSON=$(jq -n --arg cmd "$REMOTE_SCRIPT" '{commands: [$cmd]}')

COMMAND_ID=$(aws ssm send-command \
  --region "$REGION" \
  --instance-ids "$INSTANCE_ID" \
  --document-name "AWS-RunShellScript" \
  --comment "jiu-sync-backend deploy.sh $(date -u +%FT%TZ)" \
  --timeout-seconds "$SSM_TIMEOUT_SECONDS" \
  --parameters "$PARAMS_JSON" \
  --output text \
  --query "Command.CommandId")

if [ -z "$COMMAND_ID" ] || [ "$COMMAND_ID" = "None" ]; then
  echo "ERROR: failed to obtain a CommandId from ssm send-command." >&2
  exit 1
fi

echo "SSM CommandId: ${COMMAND_ID}"
echo "Polling for completion (up to $((POLL_MAX_ATTEMPTS * POLL_INTERVAL_SECONDS / 60)) min)..."

STATUS="Pending"
INVOCATION_JSON=""
ATTEMPT=0
while [ "$ATTEMPT" -lt "$POLL_MAX_ATTEMPTS" ]; do
  ATTEMPT=$((ATTEMPT + 1))
  sleep "$POLL_INTERVAL_SECONDS"

  if ! INVOCATION_JSON=$(aws ssm get-command-invocation \
        --region "$REGION" \
        --command-id "$COMMAND_ID" \
        --instance-id "$INSTANCE_ID" \
        --output json 2>/dev/null); then
    echo "  (invocation not registered yet, retrying...)"
    continue
  fi

  STATUS=$(echo "$INVOCATION_JSON" | jq -r '.Status')
  echo "  status: ${STATUS}"

  case "$STATUS" in
    Success|Failed|Cancelled|TimedOut)
      break
      ;;
  esac
done

if [ -z "$INVOCATION_JSON" ]; then
  echo "ERROR: never received an invocation record for command ${COMMAND_ID}." >&2
  exit 1
fi

echo ""
echo "----- remote STDOUT -----"
echo "$INVOCATION_JSON" | jq -r '.StandardOutputContent'
echo "----- remote STDERR -----"
echo "$INVOCATION_JSON" | jq -r '.StandardErrorContent'
echo "--------------------------"
echo ""

if [ "$STATUS" = "Success" ]; then
  echo "Deploy succeeded (SSM status: Success)."
  exit 0
else
  echo "Deploy FAILED (SSM status: ${STATUS})." >&2
  exit 1
fi

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
# Remote-side logic (executed ON the EC2 instance by SSM):
#   1. Fail fast if /opt/jiu-sync-backend/.env does not exist. Secrets are
#      created manually by a human via a one-off SSM session (see task 6.1)
#      — this script only checks for the file, it never creates it or
#      fills in defaults (design.md decision 6).
#   2. git clone (first run) or git pull (subsequent runs) the repo into
#      /opt/jiu-sync-backend. Git credentials (e.g. a PAT in
#      ~/.git-credentials) are assumed to already be set up manually on the
#      instance (design.md decision 5) — this script never touches them;
#      if auth is missing, `git clone`/`git pull` fails loudly and that
#      failure surfaces as a non-Success SSM command status.
#   3. docker compose build / up -d / migrate / collectstatic. Re-running
#      this is safe: `git pull` is a no-op with no new commits, and Docker's
#      layer cache makes an unchanged `build` near-instant (design.md
#      decision 8 — idempotency is achieved for free, no extra
#      changed-or-not bookkeeping).
#
# Usage: ./infra/scripts/deploy.sh

set -euo pipefail

# --- Local configuration -----------------------------------------------
INSTANCE_ID="i-0f6d5dc974e91bbf6"
REGION="ap-northeast-3"
REPO_DIR="/opt/jiu-sync-backend"
REPO_URL="https://github.com/2026-MentorShip-Project/jiu-sync-backend.git"
COMPOSE_FILE="infra/docker/docker-compose.prod.yml"
ENV_FILE="${REPO_DIR}/.env"
SSM_TIMEOUT_SECONDS=1800   # generous ceiling for `docker compose build` on a t2.micro
POLL_INTERVAL_SECONDS=5
POLL_MAX_ATTEMPTS=400      # ~33 min at 5s/poll, comfortably above SSM_TIMEOUT_SECONDS

command -v aws >/dev/null 2>&1 || { echo "ERROR: aws CLI not found in PATH." >&2; exit 1; }
command -v jq  >/dev/null 2>&1 || { echo "ERROR: jq not found in PATH." >&2; exit 1; }

# --- Build the remote shell script --------------------------------------
# Everything inside this heredoc runs ON THE EC2 INSTANCE, not locally.
# Local vars are interpolated here (unquoted heredoc) so the two path/URL
# constants above stay the single source of truth; `\$` escapes protect
# the parts that must be evaluated remotely instead (the ENV_FILE re-use
# below, and any future remote-side expansion).
REMOTE_SCRIPT=$(cat <<EOF
#!/bin/bash
# SSM's AWS-RunShellScript document runs commands through /bin/sh (dash) by
# default, which doesn't understand \`pipefail\`; the shebang above makes the
# agent execute this script with bash instead.
set -euo pipefail

# SSM's AWS-RunShellScript document executes commands without a login shell,
# so \$HOME is unset here even though the instance is running as root — this
# breaks \`git\` when it needs to read the root-owned ~/.gitconfig /
# ~/.git-credentials that were set up manually per design.md decision 5 (git
# reports "fatal: \$HOME not set" and can't find the credential.helper store).
# Set it explicitly from the current user's passwd entry before anything
# that touches git or other tools relying on \$HOME.
export HOME="\$(getent passwd "\$(whoami)" | cut -d: -f6)"

ENV_FILE="${ENV_FILE}"
REPO_DIR="${REPO_DIR}"
REPO_URL="${REPO_URL}"
COMPOSE_FILE="${COMPOSE_FILE}"

echo "=== [1/6] Checking for required .env at \${ENV_FILE} ==="
if [ ! -f "\${ENV_FILE}" ]; then
  echo "ERROR: \${ENV_FILE} does not exist on this instance." >&2
  echo "The production .env must be created manually via a one-off SSM session" >&2
  echo "before deploying (see openspec/changes/deploy-django-app task 6.1)." >&2
  echo "Aborting — not attempting any git/docker steps." >&2
  exit 1
fi
echo "OK: .env found."

echo "=== [2/6] Syncing repo at \${REPO_DIR} ==="
if [ -d "\${REPO_DIR}/.git" ]; then
  echo "Repo already present, running git pull..."
  git -C "\${REPO_DIR}" pull
else
  # REPO_DIR may already exist and be non-empty here (e.g. it only contains
  # the manually-created .env from task 6.1's prerequisite step), so a plain
  # \`git clone\` into it would fail with "destination path ... already exists
  # and is not an empty directory". Initialize git in place instead: this
  # only ever writes files that are tracked in the remote repo, so untracked
  # local files like .env (which is git-ignored) are left untouched.
  echo "Repo not present, initializing git in place at \${REPO_DIR}..."
  mkdir -p "\${REPO_DIR}"
  git -C "\${REPO_DIR}" init
  git -C "\${REPO_DIR}" remote add origin "\${REPO_URL}"
  git -C "\${REPO_DIR}" fetch origin
  DEFAULT_BRANCH=\$(git -C "\${REPO_DIR}" remote show origin | sed -n 's/.*HEAD branch: //p')
  if [ -z "\${DEFAULT_BRANCH}" ]; then
    echo "ERROR: could not determine origin's default branch." >&2
    exit 1
  fi
  git -C "\${REPO_DIR}" checkout -f -B "\${DEFAULT_BRANCH}" "origin/\${DEFAULT_BRANCH}"
fi

cd "\${REPO_DIR}"

echo "=== [3/6] docker compose build ==="
docker compose -f "\${COMPOSE_FILE}" build

echo "=== [4/6] docker compose up -d ==="
docker compose -f "\${COMPOSE_FILE}" up -d

echo "=== [5/6] Running migrations ==="
docker compose -f "\${COMPOSE_FILE}" exec -T app python manage.py migrate

echo "=== [6/6] Collecting static files ==="
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

#!/usr/bin/env bash
set -Eeuo pipefail

commit="${1:?commit SHA is required}"
deploy_directory="${2:?deployment directory is required}"
health_url="${3:?health URL is required}"
staged_compose="${4:?staged Compose file is required}"

if [[ ! "$commit" =~ ^[0-9a-f]{40}$ ]]; then
    echo "Invalid commit SHA: $commit" >&2
    exit 2
fi
if [[ ! "$deploy_directory" =~ ^/[A-Za-z0-9._/-]+$ ]]; then
    echo "Invalid deployment directory: $deploy_directory" >&2
    exit 2
fi
if [[ ! "$health_url" =~ ^https://[^[:space:]]+$ ]]; then
    echo "Invalid health URL: $health_url" >&2
    exit 2
fi
expected_staging_directory="/tmp/money-profile-deploy-$commit"
if [[ "$staged_compose" != "$expected_staging_directory/compose.yaml" ]]; then
    echo "Invalid staged Compose path: $staged_compose" >&2
    exit 2
fi

image="ghcr.io/doubletriplee/natarazlozhi_money_profile_bot:$commit"
rollback_env="$deploy_directory/.env.rollback-$commit"
rollback_compose="$deploy_directory/compose.yaml.rollback-$commit"
next_env="$deploy_directory/.env.next-$commit"
next_compose="$deploy_directory/compose.yaml.next-$commit"
deployment_started=0

restore_previous_release() {
    echo "Deployment failed; restoring the previous release..." >&2
    if [[ -f "$rollback_env" ]]; then
        cp -p -- "$rollback_env" "$deploy_directory/.env"
    fi
    if [[ -f "$rollback_compose" ]]; then
        cp -p -- "$rollback_compose" "$deploy_directory/compose.yaml"
    fi
    (
        cd "$deploy_directory"
        docker compose up -d --no-build --remove-orphans
    ) || echo "Automatic application rollback also failed; inspect Docker Compose on the server." >&2
}

cleanup() {
    rm -f -- "$rollback_env" "$rollback_compose" "$next_env" "$next_compose" "$staged_compose"
    if [[ "$0" == /tmp/money-profile-deploy-*/deploy_remote.sh ]]; then
        rm -f -- "$0"
        rmdir -- "$(dirname -- "$0")" 2>/dev/null || true
    fi
}

on_exit() {
    status=$?
    trap - EXIT
    set +e
    if (( status != 0 && deployment_started == 1 )); then
        restore_previous_release
    fi
    cleanup
    exit "$status"
}
trap on_exit EXIT

cd "$deploy_directory"
[[ -f .env ]] || { echo "Missing $deploy_directory/.env" >&2; exit 1; }
[[ -f compose.yaml ]] || { echo "Missing $deploy_directory/compose.yaml" >&2; exit 1; }
[[ -s "$staged_compose" ]] || { echo "Missing staged Compose file" >&2; exit 1; }

app_env="$(sed -n 's/^APP_ENV=//p' .env | tail -n 1)"
payment_mode="$(sed -n 's/^PAYMENT_MODE=//p' .env | tail -n 1)"
robokassa_test_mode="$(sed -n 's/^ROBOKASSA_TEST_MODE=//p' .env | tail -n 1)"
if [[ "$app_env" == "test" && "$payment_mode" == "fake" ]]; then
    :
elif [[ "$app_env" == "staging" && "$payment_mode" == "robokassa" && "$robokassa_test_mode" == "true" ]]; then
    test_access_ids="$(sed -n 's/^TEST_ACCESS_TELEGRAM_IDS=//p' .env | tail -n 1)"
    test_password1="$(sed -n 's/^ROBOKASSA_TEST_PASSWORD1=//p' .env | tail -n 1)"
    test_password2="$(sed -n 's/^ROBOKASSA_TEST_PASSWORD2=//p' .env | tail -n 1)"
    [[ -n "$test_access_ids" ]] || { echo "Staging requires TEST_ACCESS_TELEGRAM_IDS." >&2; exit 1; }
    [[ -n "$test_password1" ]] || { echo "Staging requires ROBOKASSA_TEST_PASSWORD1." >&2; exit 1; }
    [[ -n "$test_password2" ]] || { echo "Staging requires ROBOKASSA_TEST_PASSWORD2." >&2; exit 1; }
else
    echo "Deployment allows only test/fake or private staging/robokassa with ROBOKASSA_TEST_MODE=true." >&2
    echo "Real payments require the separate release-gate process." >&2
    exit 1
fi

cp -p -- "$staged_compose" "$next_compose"
docker compose --env-file .env -f "$next_compose" config >/dev/null

pulled=0
for attempt in $(seq 1 30); do
    echo "Pulling CI image (attempt $attempt/30): $image"
    if docker pull "$image"; then
        pulled=1
        break
    fi
    sleep 10
done
if (( pulled == 0 )); then
    echo "The CI image did not become available within five minutes: $image" >&2
    exit 1
fi

running_container="$(docker compose ps -q app 2>/dev/null || true)"
if [[ -n "$running_container" ]] && [[ "$(docker inspect --format '{{.State.Running}}' "$running_container")" == "true" ]]; then
    echo "Creating the routine encrypted SQLite snapshot..."
    backup_output="$(docker compose exec -T app sh -ceu '
        database="${DATABASE_URL#sqlite+aiosqlite:///}"
        python scripts/backup.py "$database" /data/backups --retention-days "${BACKUP_RETENTION_DAYS:-14}"
    ')"
    [[ "$backup_output" == /data/backups/money-profile-*.sqlite3.aesgcm ]]
    docker compose exec -T app test -s "$backup_output"
fi

cp -p -- .env "$rollback_env"
cp -p -- compose.yaml "$rollback_compose"
cp -p -- .env "$next_env"

upsert_env() {
    local key="$1"
    local value="$2"
    local file="$3"
    if grep -q "^${key}=" "$file"; then
        sed -i "s|^${key}=.*|${key}=${value}|" "$file"
    else
        printf '\n%s=%s\n' "$key" "$value" >> "$file"
    fi
}

upsert_env "APP_IMAGE" "$image" "$next_env"
upsert_env "SOURCE_COMMIT" "$commit" "$next_env"
chmod 600 "$next_env"

deployment_started=1
mv -- "$next_compose" compose.yaml
mv -- "$next_env" .env

docker compose up -d --no-build --remove-orphans

healthy=0
for attempt in $(seq 1 18); do
    running_container="$(docker compose ps -q app 2>/dev/null || true)"
    container_health=""
    if [[ -n "$running_container" ]]; then
        container_health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$running_container")"
    fi
    if [[ "$container_health" == "healthy" ]] && curl --fail --silent --show-error --max-time 5 "$health_url" >/dev/null; then
        healthy=1
        break
    fi
    echo "Waiting for health check (attempt $attempt/18, container: ${container_health:-missing})..."
    sleep 5
done
if (( healthy == 0 )); then
    echo "The new release did not pass its health checks." >&2
    exit 1
fi

actual_image="$(docker inspect --format '{{.Config.Image}}' "$running_container")"
if [[ "$actual_image" != "$image" ]]; then
    echo "Wrong image is running: expected $image, got $actual_image" >&2
    exit 1
fi

docker compose ps
echo "Successfully deployed $commit"

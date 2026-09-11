#!/usr/bin/env bash
set -Eeuo pipefail

role=${MONITORING_ROLE:?MONITORING_ROLE must be Germany or Russia}
base=${KUMA_BASE_URL:-http://127.0.0.1:3001}
tokens=/var/lib/monitoring/runtime/push-tokens.env
state_root=/var/lib/monitoring/check-state
history="${state_root}/disk-history-${role}.tsv"
install -d -m 0700 "${state_root}"
[[ -s ${tokens} ]] || exit 0
# shellcheck disable=SC1090
source "${tokens}"

push() {
  local key=$1 status=$2 message=$3 ping=${4:-}
  local variable="KUMA_PUSH_${key}_TOKEN"
  local token=${!variable:-}
  [[ -n ${token} ]] || return 0
  local args=(--silent --show-error --fail --max-time 10 --get
    --data-urlencode "status=${status}" --data-urlencode "msg=${message}")
  if [[ -n ${ping} ]]; then
    args+=(--data-urlencode "ping=${ping}")
  fi
  curl "${args[@]}" "${base}/api/push/${token}" >/dev/null
}

counter() {
  local key=$1 active=$2
  local file="${state_root}/${role}-${key}.count"
  local value=0
  [[ -r ${file} ]] && read -r value <"${file}"
  if [[ ${active} == true ]]; then
    value=$((value + 1))
  else
    value=0
  fi
  printf '%s\n' "${value}" >"${file}"
  printf '%s' "${value}"
}

# Keep brief dependency glitches from flapping Telegram notifications.  A
# monitor changes to down only after several consecutive failed checks and
# changes back to up only after recovery has also proved stable.
debounced_push() {
  local key=$1 healthy=$2 up_message=$3 down_message=$4
  local failure_limit=${5:-3} recovery_limit=${6:-2}
  local stable_file="${state_root}/${role}-${key}.stable"
  local stable=up failures=0 recoveries=0

  [[ -r ${stable_file} ]] && read -r stable <"${stable_file}"
  [[ ${stable} == up || ${stable} == down ]] || stable=up

  if [[ ${healthy} == true ]]; then
    counter "${key}-failure" false >/dev/null
    if [[ ${stable} == down ]]; then
      recoveries=$(counter "${key}-recovery" true)
      if [[ ${recoveries} -ge ${recovery_limit} ]]; then
        stable=up
        counter "${key}-recovery" false >/dev/null
        printf '%s\n' "${stable}" >"${stable_file}"
        push "${key}" up "${up_message}; recovered after ${recoveries} consecutive checks"
      else
        push "${key}" down "recovering (${recoveries}/${recovery_limit}); ${down_message}"
      fi
    else
      counter "${key}-recovery" false >/dev/null
      printf '%s\n' "${stable}" >"${stable_file}"
      push "${key}" up "${up_message}"
    fi
    return
  fi

  counter "${key}-recovery" false >/dev/null
  if [[ ${stable} == down ]]; then
    push "${key}" down "${down_message}"
    return
  fi

  failures=$(counter "${key}-failure" true)
  if [[ ${failures} -ge ${failure_limit} ]]; then
    stable=down
    printf '%s\n' "${stable}" >"${stable_file}"
    push "${key}" down "${down_message}; failed ${failures} consecutive checks"
  else
    printf '%s\n' "${stable}" >"${stable_file}"
    push "${key}" up "transient failure suppressed (${failures}/${failure_limit}); ${down_message}"
  fi
}

read -r _ cpu_user cpu_nice cpu_system cpu_idle cpu_iowait cpu_irq cpu_softirq cpu_steal _ </proc/stat
total_a=$((cpu_user + cpu_nice + cpu_system + cpu_idle + cpu_iowait + cpu_irq + cpu_softirq + cpu_steal))
idle_a=$((cpu_idle + cpu_iowait))
sleep 1
read -r _ cpu_user cpu_nice cpu_system cpu_idle cpu_iowait cpu_irq cpu_softirq cpu_steal _ </proc/stat
total_b=$((cpu_user + cpu_nice + cpu_system + cpu_idle + cpu_iowait + cpu_irq + cpu_softirq + cpu_steal))
idle_b=$((cpu_idle + cpu_iowait))
cpu_pct=$(awk -v total=$((total_b-total_a)) -v idle=$((idle_b-idle_a)) 'BEGIN { if (total<=0) print 0; else printf "%.0f", (total-idle)*100/total }')
read -r mem_total mem_available swap_total swap_free < <(awk '
  /MemTotal:/ {mt=$2} /MemAvailable:/ {ma=$2} /SwapTotal:/ {st=$2} /SwapFree:/ {sf=$2}
  END {print mt, ma, st, sf}' /proc/meminfo)
ram_pct=$(awk -v t="${mem_total}" -v a="${mem_available}" 'BEGIN {printf "%.0f", (t-a)*100/t}')
swap_pct=$(awk -v t="${swap_total}" -v f="${swap_free}" 'BEGIN {if (t==0) print 0; else printf "%.0f", (t-f)*100/t}')
disk_pct=$(df -P / | awk 'NR==2 {gsub("%", "", $5); print $5}')

cpu_count=$(counter cpu "$([[ ${cpu_pct} -ge 85 ]] && echo true || echo false)")
ram_warn_count=$(counter ram-warning "$([[ ${ram_pct} -ge 85 ]] && echo true || echo false)")
ram_critical_count=$(counter ram-critical "$([[ ${ram_pct} -ge 95 ]] && echo true || echo false)")
swap_count=$(counter swap "$([[ ${swap_pct} -ge 60 ]] && echo true || echo false)")

warnings=()
critical=()
[[ ${disk_pct} -ge 75 ]] && warnings+=("disk ${disk_pct}%")
[[ ${cpu_count} -ge 10 ]] && warnings+=("CPU ${cpu_pct}% for 10m")
[[ ${ram_warn_count} -ge 10 ]] && warnings+=("RAM ${ram_pct}% for 10m")
[[ ${swap_count} -ge 10 ]] && warnings+=("swap ${swap_pct}% for 10m")
[[ ${disk_pct} -ge 85 ]] && critical+=("disk ${disk_pct}%")
[[ ${ram_critical_count} -ge 5 ]] && critical+=("RAM ${ram_pct}% for 5m")

role_key=$(printf '%s' "${role}" | tr '[:lower:]' '[:upper:]')
if ((${#warnings[@]})); then
  push "${role_key}_RESOURCES_WARNING" down "WARNING: ${warnings[*]}"
else
  push "${role_key}_RESOURCES_WARNING" up "CPU ${cpu_pct}%, RAM ${ram_pct}%, swap ${swap_pct}%, disk ${disk_pct}%"
fi
if ((${#critical[@]})); then
  push "${role_key}_RESOURCES_CRITICAL" down "CRITICAL: ${critical[*]}"
else
  push "${role_key}_RESOURCES_CRITICAL" up "no critical resource thresholds"
fi

failed=()
if [[ ${role} == Russia ]]; then
  containers=(
    natarazlozhi_money_profile_bot-app-1
    ne-zabud-miniapp-1 ne-zabud-bot-1 ne-zabud-api-1 ne-zabud-postgres-1
    natarazlozhi_money_profile_bot-telegram-proxy-1
    simonenko-crm-web-1 simonenko-crm-api-1 simonenko-crm-postgres-1 simonenko-crm-minio-1
  )
  services=(docker caddy fail2ban kwork-monitor simonenko-portfolio beszel-agent monitoring-push-tunnel)
else
  containers=(
    monitoring-homepage monitoring-uptime-kuma monitoring-beszel monitoring-manager
    ne-zabud-monitor-monitor-1 ne-zabud-groq-proxy-proxy-1 amnezia-awg2
  )
  [[ -e /etc/monitoring/public-enabled ]] && containers+=(monitoring-caddy)
  services=(docker beszel-agent)
fi
for container in "${containers[@]}"; do
  running=$(docker inspect -f '{{.State.Running}}' "${container}" 2>/dev/null || true)
  health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "${container}" 2>/dev/null || true)
  [[ ${running} == true && ${health} != unhealthy ]] || failed+=("container:${container}")
done
for service in "${services[@]}"; do
  systemctl is-active --quiet "${service}.service" || failed+=("service:${service}")
done
if ((${#failed[@]})); then
  push "${role_key}_SERVICES" down "CRITICAL: ${failed[*]}"
else
  push "${role_key}_SERVICES" up "required containers and systemd services are running"
fi

if [[ ${role} == Russia ]]; then
  proxy=http://94.183.198.54:3128
  env_value() {
    local name=$1 file=$2
    sed -n -E "s/^${name}=['\"]?([^'\"]+)['\"]?$/\\1/p" "${file}" | tail -1
  }
  container_env_value() {
    local name=$1 container=$2
    docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "${container}" 2>/dev/null \
      | sed -n -E "s/^${name}=(.*)$/\\1/p" | tail -1
  }
  telegram_ok() {
    local token=$1
    [[ -n ${token} ]] && curl -x "${proxy}" -fsS --max-time 15 \
      "https://api.telegram.org/bot${token}/getMe" | grep -q '"ok":true'
  }

  money_telegram_ok() {
    timeout 15 docker exec -i natarazlozhi_money_profile_bot-app-1 python - <<'PY_MONEY' >/dev/null 2>&1
import asyncio, os
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession

async def check():
    bot = Bot(os.environ['BOT_TOKEN'], session=AiohttpSession(
        proxy=os.environ.get('TELEGRAM_PROXY_URL'), timeout=10))
    try:
        await bot.get_me()
    finally:
        await bot.session.close()

try:
    asyncio.run(check())
except Exception:
    raise SystemExit(1)
PY_MONEY
  }

  money_healthy=false
  if curl -fsS --max-time 10 http://127.0.0.1:18080/healthz >/dev/null && money_telegram_ok; then
    money_healthy=true
  fi
  debounced_push MONEY_PROFILE "${money_healthy}" \
    "event loop, SQLite and Telegram getMe are healthy" \
    "money-profile event loop, SQLite or Telegram getMe failed"

  ne_token=$(container_env_value TELEGRAM_BOT_TOKEN ne-zabud-bot-1)
  ne_api_health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' ne-zabud-api-1 2>/dev/null || true)
  ne_healthy=false
  if [[ ${ne_api_health} == healthy ]] \
    && docker exec ne-zabud-bot-1 find /tmp/monitoring-heartbeat -mmin -3 -print -quit 2>/dev/null | grep -q . \
    && telegram_ok "${ne_token}"; then
    ne_healthy=true
  fi
  debounced_push NE_ZABUD "${ne_healthy}" \
    "event loop, PostgreSQL and Telegram getMe are healthy" \
    "ne-zabud event loop, PostgreSQL or Telegram getMe failed"

  kwork_healthy=false
  if find /opt/kwork-monitor/data/monitoring-heartbeat -mmin -3 -print -quit 2>/dev/null | grep -q .; then
    kwork_healthy=true
  fi
  debounced_push KWORK_MONITOR "${kwork_healthy}" \
    "poll loop completed successfully" \
    "kwork-monitor has not completed a successful poll for at least three minutes"
fi

today=$(date -u +%F)
last_date=$(tail -1 "${history}" 2>/dev/null | cut -f1 || true)
if [[ ${last_date} != "${today}" ]]; then
  printf '%s\t%s\n' "${today}" "${disk_pct}" >>"${history}"
fi
forecast=$(tail -14 "${history}" | awk -F '\t' '
  {x=NR-1; y=$2; sx+=x; sy+=y; sxy+=x*y; sx2+=x*x; n++}
  END {
    if (n<7 || n*sx2-sx*sx==0) {print "insufficient"; exit}
    slope=(n*sxy-sx*sy)/(n*sx2-sx*sx); current=y
    if (slope<=0) {print "stable"; exit}
    printf "%.0f", (90-current)/slope
  }')
case "${forecast}" in
  insufficient) push "${role_key}_DISK_FORECAST" up "collecting daily samples (${role}); need seven" ;;
  stable) push "${role_key}_DISK_FORECAST" up "disk growth is stable or decreasing" ;;
  *)
    if [[ ${forecast} -lt 7 ]]; then
      push "${role_key}_DISK_FORECAST" down "CRITICAL: forecast to 90% in ${forecast} days"
    elif [[ ${forecast} -lt 30 ]]; then
      push "${role_key}_DISK_FORECAST" down "WARNING: forecast to 90% in ${forecast} days"
    else
      push "${role_key}_DISK_FORECAST" up "forecast to 90% in ${forecast} days"
    fi
    ;;
esac

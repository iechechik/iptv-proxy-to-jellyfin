#!/bin/bash
# Watchdog FlareSolverr. Ставится в /usr/local/bin/restart-flaresolverr.sh
# Вызывается systemd-таймером flaresolverr_iptv.timer раз в минуту.
#
# Файлы/пути — здесь, в переменных. Меняешь под свой сетап:
#   CONFIG_DIR — каталог, примонтированный в контейнер как /app
#   FLAG       — flag-файл, который ставит resolver.py при ошибке flare
#   LOG        — файл лога (только для этого скрипта)
#   COOLDOWN   — секунд между рестартами (защита от crash-loop)

set -u

CONFIG_DIR="/opt/docker-compose/configs/iptv-proxy"
FLAG="${CONFIG_DIR}/flags/flaresolverr_restart.request"
LOG="${CONFIG_DIR}/logs/iptv-proxy.log"
COOLDOWN=300

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') - [flare-watchdog] - $*" >> "$LOG"; }

[ -f "$FLAG" ] || exit 0

# Кулдаун — рядом с flag-файлом, чтобы не терялся при перезагрузке.
LAST="${CONFIG_DIR}/flags/.last_restart_ts"
now=$(date +%s)
if [ -f "$LAST" ]; then
    last=$(cat "$LAST" 2>/dev/null || echo 0)
    [ $((now - last)) -lt "$COOLDOWN" ] && exit 0
fi

reason=$(head -1 "$FLAG" 2>/dev/null)
log "Flag detected: $reason — restarting flaresolverr"

if docker restart flaresolverr >> "$LOG" 2>&1; then
    rm -f "$FLAG"
    echo "$now" > "$LAST"
    log "Restart OK, flag removed"
else
    log "Restart FAILED, flag left in place"
fi

#!/bin/sh
set -eu

SOURCE="${TG_ANTISPAM_BOT_SOURCE:-git+https://github.com/PromiseFru/tg-antispam-bot}"
SERVICE=tg-antispam-bot
UPDATER=tg-antispam-bot-update

fail() {
  echo "error: $*" >&2
  exit 1
}

usage() {
  echo "usage: install.sh [--user | --system]"
  echo "  --user    run as your user, no sudo (default)"
  echo "  --system  run as a dedicated system user, needs root"
}

main() {
  MODE=user
  case "${1:-}" in
  "" | --user) ;;
  --system) MODE=system ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    exit 2
    ;;
  esac

  command -v python3 >/dev/null || fail "python3 is required"
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' ||
    fail "Python 3.11 or newer is required"
  command -v git >/dev/null || fail "git is required"
  command -v systemctl >/dev/null || fail "systemd is required"

  if [ "$MODE" = system ]; then
    [ "$(id -u)" -eq 0 ] || fail "--system needs root, run it with sudo"
    VENV=/opt/tg-antispam-bot/venv
    CONFIG=/etc/tg-antispam-bot
    ENV_FILE=$CONFIG/tg-antispam-bot.env
    DB=/var/lib/tg-antispam-bot/tg-antispam-bot.db
    UNIT=/etc/systemd/system/$SERVICE.service
    CTL="systemctl"
    LOGS="journalctl"
    id tg-antispam-bot >/dev/null 2>&1 ||
      useradd --system --user-group --shell /usr/sbin/nologin tg-antispam-bot
  else
    DATA="${XDG_DATA_HOME:-$HOME/.local/share}/tg-antispam-bot"
    VENV=$DATA/venv
    CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/tg-antispam-bot"
    ENV_FILE=$CONFIG/.env
    DB=$DATA/tg-antispam-bot.db
    UNIT="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$SERVICE.service"
    CTL="systemctl --user"
    LOGS="journalctl --user"
  fi

  echo "Installing into $VENV"
  mkdir -p "$(dirname "$VENV")" "$CONFIG" "$(dirname "$UNIT")"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade "$SOURCE"

  $CTL stop "$SERVICE" 2>/dev/null || true
  "$VENV/bin/tg-antispam-bot" setup --env-file "$ENV_FILE" --db-path "$DB" \
    --rules-path "$CONFIG/rules.yaml" </dev/tty

  if [ "$MODE" = system ]; then
    chgrp -R tg-antispam-bot "$CONFIG"
    chmod 750 "$CONFIG"
    chmod 640 "$CONFIG/rules.yaml"
    cat >"$UNIT" <<EOF
[Unit]
Description=Telegram anti-spam bot
After=network-online.target
Wants=network-online.target

[Service]
User=tg-antispam-bot
Group=tg-antispam-bot
EnvironmentFile=$ENV_FILE
Type=notify
NotifyAccess=main
TimeoutStartSec=5min
ExecStart=$VENV/bin/tg-antispam-bot
ExecReload=/bin/kill -HUP \$MAINPID
StateDirectory=tg-antispam-bot
StateDirectoryMode=0750
Restart=on-failure
RestartSec=5
UMask=0077
MemoryMax=512M
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true
ProtectControlGroups=true
ProtectClock=true
ProtectHostname=true
RestrictNamespaces=true
RestrictRealtime=true
RestrictSUIDSGID=true
LockPersonality=true
CapabilityBoundingSet=
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX

[Install]
WantedBy=multi-user.target
EOF
  else
    cat >"$UNIT" <<EOF
[Unit]
Description=Telegram anti-spam bot
After=network-online.target

[Service]
EnvironmentFile=$ENV_FILE
Type=notify
NotifyAccess=main
TimeoutStartSec=5min
ExecStart=$VENV/bin/tg-antispam-bot
ExecReload=/bin/kill -HUP \$MAINPID
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=default.target
EOF
  fi

  UNIT_DIR=$(dirname "$UNIT")
  cat >"$UNIT_DIR/$UPDATER.service" <<EOF
[Unit]
Description=Telegram anti-spam bot updater
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=$ENV_FILE
StateDirectory=$UPDATER
StateDirectoryMode=0700
ExecStart=$VENV/bin/tg-antispam-bot update --auto
EOF
  cat >"$UNIT_DIR/$UPDATER.timer" <<EOF
[Unit]
Description=Daily Telegram anti-spam bot update

[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF

  $CTL daemon-reload
  $CTL enable --now "$SERVICE"
  $CTL enable --now "$UPDATER.timer"
  if [ "$MODE" = user ]; then
    loginctl enable-linger "$(id -un)" 2>/dev/null ||
      echo "To keep the bot running after you log out: sudo loginctl enable-linger $(id -un)"
  fi
  echo "Running. Logs: $LOGS -u $SERVICE -f"
  grep -qiE '^AUTO_UPDATE=["'"'"']?(true|1|yes|on)' "$ENV_FILE" ||
    echo "Automatic updates are off. Set AUTO_UPDATE=true in $ENV_FILE to turn them on."
}

main "$@"

# Telegram Anti-Spam Bot

Customizable anti-spam bot for Telegram groups. Detectors score each message to decide whether to delete it, restrict the sender or ban them, and admins can review and undo every action from a DM.

Features marked **[Experimental]** still need more testing.

## Requirements

- [Python](https://www.python.org/downloads/) 3.11+
- [Git](https://git-scm.com/)
- A bot token from [@BotFather](https://t.me/BotFather)
- A supergroup where the bot is an admin with **Delete messages** and **Ban users**
- [systemd](https://systemd.io/)

## Quick start

```sh
curl -fsSL https://raw.githubusercontent.com/PromiseFru/tg-antispam-bot/main/install.sh | sh
```

The installer asks for the token, waits for you to add the bot to your groups, and starts the service. Admins then send `/setupalerts` in the group to get alerts.

| Mode | Command | Files |
| --- | --- | --- |
| User (default, no sudo) | `… \| sh` | `~/.local/share/tg-antispam-bot/`, `~/.config/tg-antispam-bot/` |
| System | `… \| sudo sh -s -- --system` | `/opt/tg-antispam-bot/`, `/etc/tg-antispam-bot/`, `/var/lib/tg-antispam-bot/` |

```sh
systemctl --user status tg-antispam-bot
systemctl --user reload tg-antispam-bot
journalctl --user -u tg-antispam-bot -f
```

In system mode, use `sudo` and drop `--user`.

> [!TIP]
> Run the installer again to upgrade or add a group. It keeps your `.env` and `rules.yaml`, so compare yours with the [shipped rules](src/tg_antispam_bot/rules.yaml) after upgrading.

> [!NOTE]
> User services stop at logout. The installer enables lingering; if it can't, run `loginctl enable-linger $USER`.

## Environment

| Variable | Default | |
| --- | --- | --- |
| `BOT_TOKEN` | required | |
| `DB_ENCRYPTION_KEY` | required | 64 hex characters |
| `ALLOWED_CHAT_IDS` | required | comma-separated group IDs, or `none` |
| `DB_PATH` | `tg-antispam-bot.db` | |
| `RULES_PATH` | `rules.yaml` | |
| `FALLBACK_LOG_CHAT_ID` | unset | chat for alerts from groups with no subscribed admins **[Experimental]** |
| `LOG_LEVEL` | `INFO` | |

Written by `tg-antispam-bot setup`. See [example.env](example.env).

> [!IMPORTANT]
> Run one instance per token.

## Commands

Group commands are admin-only, and only the sender sees the reply.

| In a group | |
| --- | --- |
| `/status` | rights, thresholds, admins' alert status, detectors |
| `/log [n]` | recent actions, `n` per page (up to 10) |
| `/setupalerts` | link for subscribing to alerts |
| `/antispam on\|off` | turn moderation on or off **[Experimental]** |
| `/antispam alerts on\|off` | live alerts in the group **[Experimental]** |
| `/threshold delete\|restrict\|ban <n>` | change a threshold **[Experimental]** |
| `/addrule <detector> <regex>` | add a pattern **[Experimental]** |
| `/delrule [<detector> <regex>]` | remove a pattern, or list them **[Experimental]** |
| `/trust`, `/untrust` `<id>` or as a reply | exempt a user **[Experimental]** |
| `/raidmode on\|off` | start or stop raid mode **[Experimental]** |

| In a private chat | |
| --- | --- |
| `/start`, `/groups` | turn alerts on or off per group |
| `/prefs` | all actions, bans only, or digest only **[Experimental]** |

## Features

### Scoring

Each detector that fires adds its `weight`, and the total picks the action:

- `tiers.delete`: delete the message
- `tiers.delete_and_restrict`: also restrict the sender for `restrict_duration`
- `tiers.delete_and_ban`: also ban the sender

Context detectors add points but can't trigger an action without a signal detector.

| Detector | Kind | Fires when | Options |
| --- | --- | --- | --- |
| `mention_count` | signal | at least `threshold` mentions | `threshold` |
| `mention_pattern` | signal | at least `min_matches` mentioned usernames match | `patterns`, `min_matches` |
| `text_pattern` | signal | the text matches | `patterns` |
| `link_pattern` | signal | the text or a link matches | `patterns` |
| `keyword_cluster` | signal | at least `min_matches` different patterns match the text | `patterns`, `min_matches` |
| `forwarded_from_channel` | signal | a new member forwards a channel post **[Experimental]** | |
| `cas` | signal | the sender is on the [CAS](https://cas.chat) list, off by default **[Experimental]** | `timeout`, `cache_ttl` |
| `new_member` | context | the sender joined within `window` | `window` |
| `early_messages` | context | one of the sender's first `first_n` messages | `first_n` |
| `display_name_pattern` | context | the sender's name matches **[Experimental]** | `patterns` |
| `edited_spam` | context | an edited message is flagged | |
| `raid` | context | a new member posts during a join surge **[Experimental]** | `joins`, `window` |
| `join_churn` | context | the sender joined `joins` times within `window` **[Experimental]** | `joins`, `window` |

Every detector also takes `enabled`, `weight` and `description`.

### Admin review

Subscribed admins get a card for every action, showing the reasons and the message. **Unban** lifts the penalty and closes all of that user's open actions. Above `notifications.digest_threshold`, cards switch to a single digest that keeps updating.

### Trust, False positive and Confirm [Experimental]

- **Trust** lifts the penalty and exempts the user.
- **False positive** lifts it and forgets the user's join history.
- **Confirm** keeps the penalty.
- **Dismiss** removes a resolved card from your chat.

### Repeat offenders [Experimental]

Repeat offences within `escalation.window` restrict the user at `restrict_after` and ban them at `ban_after`, whatever the score. Unban, Trust and False positive reset the count; Confirm doesn't.

### Bots [Experimental]

A bot added by someone who isn't an admin is banned straight away, and admins get a card to undo it. Trusted bots are kept. Set `remove_bots_added_by_members: false` to allow it.

A guest bot summoned by someone who isn't an admin is banned and its reply deleted, and admins get a card to undo it. Bots summoned by admins or trusted users are kept. Set `remove_guest_bots_summoned_by_members: false` to score their replies like other messages instead.

Messages from other bots are scored like anyone else's, unless the bot is an admin.

> [!NOTE]
> Telegram only delivers other bots' messages if **Bot-to-Bot Communication Mode** is on for this bot in @BotFather.

### Raid mode [Experimental]

Starts when more than `detectors.raid.joins` accounts join within `detectors.raid.window`, or on `/raidmode on`. New members are restricted for `raid_mode.restrict_duration` until `raid_mode.duration` after the last surge. Set `raid_mode.auto: false` to keep it manual.

### Rules reload [Experimental]

A service reload applies changes to `defaults` and `chats`, and keeps the current rules if the file is invalid. Changes to `global` need a restart.

### Group upgrades [Experimental]

When a basic group is upgraded to a supergroup, the bot follows its new ID and logs a reminder to add it to `ALLOWED_CHAT_IDS`.

## Rules

Each layer overrides the one before: built-in defaults, `defaults`, `chats."<id>"`, then admin commands.

```yaml
global:
  joins_retention: 7d
  actions_retention: 90d
  message_text_retention: 14d

defaults:
  tiers: {delete: 50, delete_and_restrict: 70, delete_and_ban: 100}
  restrict_duration: 1d
  escalation: {enabled: true, window: 10m, restrict_after: 2, ban_after: 3}
  raid_mode: {auto: true, duration: 30m, restrict_duration: 1h}
  detectors:
    mention_pattern: {weight: 60, patterns: ["bot$"]}

chats:
  "-1001234567890":
    tiers: {delete: 40}
```

Durations use `s`, `m`, `h`, `d` or `w`. See [rules.yaml](src/tg_antispam_bot/rules.yaml) for every key.

## Data

Stored encrypted with SQLCipher. Message text is kept only for actioned messages.

| Stored | Kept for |
| --- | --- |
| join times | `joins_retention` |
| message count per member | `activity_retention` after their last message |
| IDs of new members' recent messages | `recent_messages_retention` |
| actions: user ID, name, score, reasons | `actions_retention` |
| text of actioned messages | `message_text_retention` |
| subscriptions, preferences, trusted IDs | until changed |

> [!IMPORTANT]
> Back up `DB_ENCRYPTION_KEY` along with the database.

## Manual installation

The user-mode steps from `install.sh`:

```sh
python3 -m venv ~/.local/share/tg-antispam-bot/venv
~/.local/share/tg-antispam-bot/venv/bin/pip install git+https://github.com/PromiseFru/tg-antispam-bot
~/.local/share/tg-antispam-bot/venv/bin/tg-antispam-bot setup \
    --env-file ~/.config/tg-antispam-bot/.env \
    --db-path ~/.local/share/tg-antispam-bot/tg-antispam-bot.db \
    --rules-path ~/.config/tg-antispam-bot/rules.yaml
```

Copy the user unit from [install.sh](install.sh) to `~/.config/systemd/user/tg-antispam-bot.service`, then:

```sh
systemctl --user daemon-reload && systemctl --user enable --now tg-antispam-bot
loginctl enable-linger $USER
```

> [!TIP]
> System mode uses the same steps with the paths above, a `tg-antispam-bot` system user, and the hardened unit.

## Uninstall

> [!WARNING]
> This deletes the database. Back it up with `DB_ENCRYPTION_KEY` first if you might reinstall.

User mode:

```sh
systemctl --user disable --now tg-antispam-bot
rm ~/.config/systemd/user/tg-antispam-bot.service
systemctl --user daemon-reload
rm -rf ~/.local/share/tg-antispam-bot ~/.config/tg-antispam-bot
```

System mode:

```sh
sudo systemctl disable --now tg-antispam-bot
sudo rm /etc/systemd/system/tg-antispam-bot.service
sudo systemctl daemon-reload
sudo rm -rf /opt/tg-antispam-bot /etc/tg-antispam-bot /var/lib/tg-antispam-bot
sudo userdel tg-antispam-bot
```

Run `loginctl disable-linger` too if nothing else of yours needs to run after logout.

## Development

```sh
git clone https://github.com/PromiseFru/tg-antispam-bot && cd tg-antispam-bot
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/ruff format . && .venv/bin/ruff check .
uvx pyright
```

For schema changes, edit `models.py` and run `alembic revision --autogenerate`, one table per migration.

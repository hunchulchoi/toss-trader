#!/bin/sh
set -eu

: "${TELEGRAM_BOT_TOKEN:?TELEGRAM_BOT_TOKEN is required}"
: "${TELEGRAM_CHAT_ID:?TELEGRAM_CHAT_ID is required}"
: "${TELEGRAM_TOPIC:?TELEGRAM_TOPIC is required}"

case "$TELEGRAM_CHAT_ID" in
  -*) chat_digits=${TELEGRAM_CHAT_ID#-} ;;
  *) chat_digits=$TELEGRAM_CHAT_ID ;;
esac
case "$chat_digits" in
  ""|*[!0-9]*)
    echo "TELEGRAM_CHAT_ID must be an integer" >&2
    exit 1
    ;;
esac

case "$TELEGRAM_TOPIC" in
  ""|*[!0-9]*|0)
    echo "TELEGRAM_TOPIC must be a positive integer" >&2
    exit 1
    ;;
esac

template=/etc/alertmanager/config.template.yml
rendered=/tmp/alertmanager.yml
umask 077

while IFS= read -r line || [ -n "$line" ]; do
  rendered_line=$line
  case "$rendered_line" in
    *"__TELEGRAM_BOT_TOKEN__"*)
      prefix=${rendered_line%%__TELEGRAM_BOT_TOKEN__*}
      suffix=${rendered_line#*__TELEGRAM_BOT_TOKEN__}
      rendered_line="${prefix}${TELEGRAM_BOT_TOKEN}${suffix}"
      ;;
  esac
  case "$rendered_line" in
    *"__TELEGRAM_CHAT_ID__"*)
      prefix=${rendered_line%%__TELEGRAM_CHAT_ID__*}
      suffix=${rendered_line#*__TELEGRAM_CHAT_ID__}
      rendered_line="${prefix}${TELEGRAM_CHAT_ID}${suffix}"
      ;;
  esac
  case "$rendered_line" in
    *"__TELEGRAM_TOPIC__"*)
      prefix=${rendered_line%%__TELEGRAM_TOPIC__*}
      suffix=${rendered_line#*__TELEGRAM_TOPIC__}
      rendered_line="${prefix}${TELEGRAM_TOPIC}${suffix}"
      ;;
  esac
  printf '%s\n' "$rendered_line"
done < "$template" > "$rendered"

unset TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID TELEGRAM_TOPIC chat_digits line rendered_line prefix suffix
exec /bin/alertmanager "$@"

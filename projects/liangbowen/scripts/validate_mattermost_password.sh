#!/bin/sh
set -eu

password=${MATTERMOST_DB_PASSWORD-}

case "$password" in
  ""|*[!A-Za-z0-9._~-]*)
    printf '%s\n' \
      'MATTERMOST_DB_PASSWORD must contain only URL-safe A-Z, a-z, 0-9, ., _, ~, or - characters.' \
      >&2
    exit 64
    ;;
esac

if [ "${#password}" -lt 16 ]; then
  printf '%s\n' 'MATTERMOST_DB_PASSWORD must contain at least 16 characters.' >&2
  exit 64
fi

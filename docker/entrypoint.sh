#!/bin/sh
# Start the Snipmint server in the container.
#
# The server refuses to listen beyond localhost without an access token. The token comes from
# SNIPMINT_TOKEN (e.g. in .env); otherwise one is made on the first start and kept in the data
# folder, so the link (and any open browser tab) keeps working after a restart or an update.
set -e

TOKEN_FILE="${SNIPMINT_HOME:-/data}/.token"
if [ -z "${SNIPMINT_TOKEN}" ]; then
  if [ -s "${TOKEN_FILE}" ]; then
    SNIPMINT_TOKEN="$(cat "${TOKEN_FILE}")"
  else
    SNIPMINT_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(12))')"
    mkdir -p "$(dirname "${TOKEN_FILE}")"
    printf '%s' "${SNIPMINT_TOKEN}" > "${TOKEN_FILE}"
  fi
fi
export SNIPMINT_TOKEN

echo "Snipmint: open http://localhost:${SNIPMINT_PORT:-8000}/?token=${SNIPMINT_TOKEN}"
exec snipmint serve --host 0.0.0.0 --port "${SNIPMINT_PORT:-8000}"

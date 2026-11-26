#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
state=secrets/local-dashboard
origin=https://localhost:39475
export LOCAL_UID=$(id -u) LOCAL_GID=$(id -g)
# Pair each checkout's database volume with its own generated credentials.
checkout_id=$(pwd -P | cksum | cut -d ' ' -f 1)
project=monzo-dashboard-local-$checkout_id
export LOCAL_API_IMAGE=$project-api
compose() { docker compose -p "$project" -f compose.local.yaml "$@"; }
case "${1:-start}" in
  stop) compose stop; exit ;;
  start) ;;
  *) echo "Usage: $0 [start|stop]" >&2; exit 2 ;;
esac
umask 077
mkdir -p "$state"
docker build --target runtime -t "$LOCAL_API_IMAGE" .
setup() {
  script=$1
  shift
  docker run --rm --network none --user "$LOCAL_UID:$LOCAL_GID" \
    -v "$PWD/$state:/state" -v "$PWD/scripts:/setup:ro" \
    "$LOCAL_API_IMAGE" python "/setup/$script" "$@"
}
if [ ! -e "$state/.env" ]; then
  setup bootstrap_secrets.py /state
fi
if [ ! -e "$state/dashboard.env" ]; then
  setup setup-dashboard.py --directory /state --origin "$origin"
fi
if [ ! -s "$state/localhost.crt" ] || ! openssl x509 -in "$state/localhost.crt" -checkend 86400 -noout >/dev/null; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj /CN=localhost \
    -addext 'subjectAltName=DNS:localhost' -keyout "$state/localhost.key" -out "$state/localhost.crt"
fi
python3 - "$state/gateway.conf" <<'GATEWAY'
from pathlib import Path
import sys
config = Path("deploy/dashboard-gateway.conf").read_text()
config = config.replace("listen 8082;", "listen 8082 ssl;\n        ssl_certificate /tmp/localhost.crt;\n        ssl_certificate_key /tmp/localhost.key;")
config = config.replace("server_name spending.example.invalid;", "server_name localhost;")
config = config.replace('"spending.example.invalid"', '"localhost:39475"')
config = "\n".join(line for line in config.splitlines() if "add_header Strict-Transport-Security" not in line)
Path(sys.argv[1]).write_text(config + "\n")
GATEWAY
compose up -d --build --wait
printf 'Dashboard: %s/spending\nLocal password: %s/%s/dashboard-login.txt\n' "$origin" "$PWD" "$state"

#!/bin/sh
# Sert les rapports en tâche de fond, puis passe la main au planificateur.
set -eu

pea serve --port "${PEA_PORT:-8080}" &

# Chemin absolu voulu : supercronic se relance lui-même sans consulter le PATH.
exec /usr/local/bin/supercronic -passthrough-logs /app/deploy/crontab

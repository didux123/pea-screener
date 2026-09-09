#!/bin/sh
# Sert les rapports en tâche de fond, puis passe la main au planificateur.
set -eu

pea serve --port "${PEA_PORT:-8080}" &

exec supercronic -passthrough-logs /app/deploy/crontab

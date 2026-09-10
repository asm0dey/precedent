#!/usr/bin/env bash
# Recreate the eval fixture graph: seven tagged projects and the decisions that
# make precedent meaningful. `bot-new` is deliberately left untagged so an eval
# exercises classification rather than assuming it.
set -euo pipefail
HOME_DIR="${1:?usage: seed-fixtures.sh <graph-dir> <projects-dir>}"
P="${2:?usage: seed-fixtures.sh <graph-dir> <projects-dir>}"
CLI="$(dirname "$0")/../scripts/precedent.py"
d() { uv run --quiet "$CLI" --home "$HOME_DIR" "$@"; }
r() { local proj="$1"; shift; d record --project "$P/$proj" "$@" >/dev/null; }

mkdir -p "$P"/{bot-alpha,bot-beta,bot-gamma,bot-new,api-one,api-two,shop-web,etl-nightly}
for b in bot-alpha bot-beta bot-gamma bot-new; do
  printf '[project]\ndependencies=["aiogram","redis","httpx"]\n' > "$P/$b/pyproject.toml"
done
for a in api-one api-two; do
  printf '[project]\ndependencies=["fastapi","sqlalchemy","uvicorn"]\n' > "$P/$a/pyproject.toml"
done
printf '{"dependencies":{"next":"15.0.0","react":"19.0.0"}}\n' > "$P/shop-web/package.json"
printf '[project]\ndependencies=["dagster","polars"]\n' > "$P/etl-nightly/pyproject.toml"

d tag --project "$P/bot-alpha"   --add bot,telegram,python >/dev/null
d tag --project "$P/bot-beta"    --add bot,telegram,python >/dev/null
d tag --project "$P/bot-gamma"   --add bot,telegram,python >/dev/null
d tag --project "$P/api-one"     --add backend,api,python  >/dev/null
d tag --project "$P/api-two"     --add backend,api,python  >/dev/null
d tag --project "$P/shop-web"    --add frontend,typescript >/dev/null
d tag --project "$P/etl-nightly" --add data-pipeline,python >/dev/null

for b in bot-alpha bot-beta bot-gamma; do
  r "$b" --title "Postgres for bot state ($b)" --scope architecture --topic persistence \
    --chose postgres --rejected sqlite \
    --rationale "Two bots already share one Postgres; SQLite locks up once webhook handlers run concurrently"
  r "$b" --title "structlog with JSON renderer ($b)" --scope tooling --topic logging \
    --chose structlog --rejected stdlib-logging \
    --rationale "Loki queries need structured fields; plain formatter strings are unusable at volume"
done
r bot-alpha --title "Webhooks, not long polling" --scope architecture --topic ingest \
  --chose webhook --rejected long-polling \
  --rationale "Long polling burned the API quota and made restarts drop updates"
r bot-beta --title "Webhooks for beta too" --scope architecture --topic ingest \
  --chose webhook --rejected long-polling --rationale "Same quota problem as alpha"
r bot-alpha --title "Telegram Stars for payments" --scope business --topic payments \
  --chose telegram-stars --rejected stripe \
  --rationale "Stripe needs a merchant entity per country; Stars is in-platform and takes the 30% hit instead"
r bot-gamma --title "Deploy as a systemd unit on the VPS" --scope process --topic deployment \
  --chose systemd --rejected docker,k8s \
  --rationale "One VPS, one process; containers add a build step for no isolation benefit here"
for a in api-one api-two; do
  r "$a" --title "Postgres via SQLAlchemy ($a)" --scope architecture --topic persistence \
    --chose postgres --rejected mongo \
    --rationale "Relational data, and migrations matter more than schema flexibility"
done
r api-one --title "Keycloak for auth" --scope architecture --topic auth \
  --chose keycloak --rejected auth0,homegrown-jwt \
  --rationale "Auth0 pricing steps up hard past 7k MAU; Keycloak is self-hosted next to the DB"
r api-two --title "Keycloak again" --scope architecture --topic auth --chose keycloak --rejected auth0 \
  --rationale "Consistency with api-one; one realm serves both"
r api-one --title "Docker Compose on a single host" --scope process --topic deployment \
  --chose docker-compose --rejected k8s --rationale "Two services and a DB; k8s is a second job"
r shop-web --title "Zustand over Redux" --scope architecture --topic state-management \
  --chose zustand --rejected redux,context-only \
  --rationale "Three stores, no middleware needed; Redux boilerplate is not paying rent"
r etl-nightly --title "Dagster for orchestration" --scope tooling --topic orchestration \
  --chose dagster --rejected airflow \
  --rationale "Asset-based model matches the warehouse tables; Airflow DAG plumbing was fighting us"
d principle --id logging-structlog \
  --statement "For logging, use structlog with a JSON renderer." >/dev/null
echo "seeded: $(d maintain | tail -2 | head -1)"

# Open Food Facts - Self-Hosted Local API

Self-hosted API compatible with the [Open Food Facts](https://world.openfoodfacts.org) endpoints, running locally in Docker.

**Works on ARM64** (Raspberry Pi, Apple Silicon, ARM VPS).

Exposes the OFF API via [Caddy reverse proxy](https://github.com/BuildWithPaul/caddy-docker) at `/off/` with automatic HTTPS. No direct host port needed — Caddy handles TLS termination and path routing.

Uses **FastAPI + PostgreSQL** instead of the official OFF Perl stack (which is x86_64-only).

## Quick Start

This repo is designed to run as part of the [caddy-docker](https://github.com/BuildWithPaul/caddy-docker) stack. Clone all repos side by side:

```bash
# Clone the stack
git clone https://github.com/BuildWithPaul/caddy-docker.git ~/caddy-docker
git clone https://github.com/BuildWithPaul/openfoodfacts-docker.git ~/openfoodfacts-docker
# ...plus any other services (VitaminChecker, ChouChouAlerte, etc.)

# Start the whole stack
cd ~/caddy-docker
docker compose up -d --build

# Wait for off-postgres to be healthy
docker compose ps off-postgres

# Import French products (~500K products, takes 10-20 min)
docker compose up off-import

# Verify it's running (through Caddy)
curl -s "https://paul-sandbox.duckdns.org/off/health" | python3 -m json.tool

# Search for a product
curl -s "https://paul-sandbox.duckdns.org/off/cgi/search.pl?search_terms=banane&json=1&page_size=3" | python3 -m json.tool

# Barcode lookup
curl -s "https://paul-sandbox.duckdns.org/off/api/v2/product/3017620422003.json?fields=product_name,nutriments" | python3 -m json.tool
```

### Standalone mode (without Caddy)

If you want to run the OFF API standalone on a host port:

```bash
cd ~/openfoodfacts-docker
docker compose up -d postgres

# Wait for PostgreSQL to be healthy
docker compose ps postgres

# Import data
docker compose up off-import

# Start the API (exposes port 5003 on the host)
docker compose up -d off-api

# Verify
curl -s "http://localhost:5003/health" | python3 -m json.tool
```

## API Endpoints

Compatible with OFF API — your existing OFF client code works without changes:

When accessed through Caddy (`/off/` prefix):

| Endpoint (via Caddy) | Description |
|---|---|
| `GET /off/api/v2/product/{barcode}.json` | Product lookup by barcode |
| `GET /off/api/v3/product/{barcode}` | V3 product lookup |
| `GET /off/cgi/search.pl?search_terms=banane&json=1` | Full-text search |
| `GET /off/api/v2/search?search_terms=banane` | V2 search |
| `GET /off/health` | Health check + DB stats |

When accessed directly (standalone, port 5003):

| Endpoint | Description |
|---|---|
| `GET /api/v2/product/{barcode}.json` | Product lookup by barcode |
| `GET /api/v3/product/{barcode}` | V3 product lookup |
| `GET /cgi/search.pl?search_terms=banane&json=1` | Full-text search |
| `GET /api/v2/search?search_terms=banane` | V2 search |
| `GET /health` | Health check + DB stats |

Products not found locally are automatically proxied to `world.openfoodfacts.org`.

## Data Import

Choose your dataset based on disk space and needs:

| Dataset | Products | Disk Usage | Import Time |
|---------|----------|------------|-------------|
| `french` | ~500K | ~3-5 GB | 10-20 min |
| `full` | ~3M | ~15-20 GB | 30-60 min |

Set `IMPORT_DATASET` in `.env`:

```bash
# French products only (recommended)
IMPORT_DATASET=french

# All products worldwide
IMPORT_DATASET=full
```

Then run:
```bash
# From caddy-docker directory
docker compose up off-import
```

> **Note:** The import streams the full OFF JSONL dump (~11 GB compressed) and filters in-memory. You need ~2 GB RAM for the filtering process.

## Configuration

Edit `.env`:

| Variable | Default | Description |
|---|---|---|
| `POSTGRES_USER` | `off` | PostgreSQL user |
| `POSTGRES_PASSWORD` | `off` | PostgreSQL password |
| `POSTGRES_DB` | `openfoodfacts` | Database name |
| `IMPORT_DATASET` | `french` | Which data to import (french/full) |

> **Note:** `OFF_PORT` is only used in standalone mode. In the Caddy stack, the API is accessed through Caddy's reverse proxy — no host port mapping needed.

## Architecture

### In the Caddy stack (production)

```
Browser ──HTTPS──▶ Caddy (80/443)
                      │
                      ├── /off/api/v2/product/* ──▶ off-api:8000
                      ├── /off/api/v3/product/* ──▶ off-api:8000
                      ├── /off/cgi/search.pl    ──▶ off-api:8000
                      └── /off/health            ──▶ off-api:8000
                                                      │
                                                 ┌────▼─────┐
                                                 │ off-postgres│
                                                 │  :5432      │
                                                 └────────────┘
```

### Standalone

```
              Port 5003
                  │
             ┌────▼─────┐
             │  off-api  │  (FastAPI — serves OFF-compatible endpoints)
             │  :8000    │
             └────┬─────┘
                  │
             ┌────▼─────┐
             │ PostgreSQL│  (JSONB + full-text search)
             │  :5432    │
             └───────────┘

off-import ──▶ Downloads OFF JSONL ──▶ Filters ──▶ Inserts into PostgreSQL
```

The API only stores the fields VitaminChecker needs:
- Product name, generic name, brands, categories
- Vitamin and mineral nutriment data (per 100g values + units)
- Country/language tags for filtering

Each product's full nutriment data is stored as JSONB, enabling fast lookups.

## Using with VitaminChecker

Point VitaminChecker to the OFF API through Caddy:

```yaml
# In VitaminChecker's environment (docker-compose.yml):
environment:
  - OFF_API_URL=http://off-api:8000
```

Or externally:

```bash
OFF_API_URL=https://paul-sandbox.duckdns.org/off
```

VitaminChecker automatically falls back to `world.openfoodfacts.org` if the local API is down.

## Management

```bash
# View API logs
docker compose logs -f off-api

# View import logs
docker compose logs off-import

# Re-import data (e.g., after a DB wipe)
docker compose up off-import

# Restart everything
docker compose restart

# Stop everything
docker compose down

# Stop and delete data
docker compose down -v
```

> Run these from `~/caddy-docker` when using the Caddy stack, or from `~/openfoodfacts-docker` in standalone mode.

## Resources

- **Disk:** French=3-5 GB, Full=15-20 GB
- **RAM:** 512 MB for API, 256 MB for PostgreSQL idle, ~1 GB during import
- **CPU:** Minimal for queries, significant during import

## Troubleshooting

### API returns empty results
Import data first:
```bash
docker compose up off-import
```

### Import fails with connection error
Make sure PostgreSQL is healthy:
```bash
docker compose ps off-postgres
```

### 502 Bad Gateway through Caddy
Check the off-api container is running:
```bash
docker compose logs off-api
```

### Health check not responding (standalone mode)
```bash
docker compose ps off-api
curl -I http://localhost:5003/health
```

### Want to re-import from scratch
```bash
docker compose down -v  # destroys all data
docker compose up -d off-postgres
docker compose up off-import
docker compose up -d off-api
```

## Related

- [caddy-docker](https://github.com/BuildWithPaul/caddy-docker) — Caddy reverse proxy that exposes this service over HTTPS
- [VitaminChecker](https://github.com/BuildWithPaul/VitaminChecker) — Flask app that uses this API

## License

The Open Food Facts data is licensed under the [Open Database License](https://opendatacommons.org/licenses/odbl/1.0/).
This Docker setup and API code is released under the MIT License.
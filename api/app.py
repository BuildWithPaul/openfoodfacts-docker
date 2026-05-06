"""
Open Food Facts Local API
-------------------------
Serves OFF-compatible API endpoints backed by PostgreSQL.
Compatible with the VitaminChecker OFF client code.

Endpoints:
  GET /api/v2/product/{barcode}.json - Product lookup by barcode
  GET /cgi/search.pl                  - Text search
  GET /health                         - Health check
"""

import os
import logging
from typing import Optional

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool

# ─── Config ──────────────────────────────────────────────────────────

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://off:off@off-postgres:5432/off")
OFF_PUBLIC_URL = os.environ.get("OFF_PUBLIC_URL", "https://world.openfoodfacts.org")

logger = logging.getLogger("off-api")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(name)s — %(message)s")

# ─── Database ────────────────────────────────────────────────────────

engine = create_engine(DATABASE_URL, poolclass=QueuePool, pool_size=10, max_overflow=20)


def get_db():
    """Get a database connection."""
    conn = engine.connect()
    try:
        return conn
    except Exception:
        conn.close()
        raise


# ─── FastAPI App ─────────────────────────────────────────────────────

app = FastAPI(title="Open Food Facts Local API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ─── Barcode Lookup ──────────────────────────────────────────────────

@app.get("/api/v2/product/{barcode}.json")
async def product_lookup(barcode: str, request: Request):
    """Get product by barcode — same response format as OFF API."""
    fields = request.query_params.get("fields", "")

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT data FROM products WHERE code = :code"),
            {"code": barcode.strip()}
        ).fetchone()

    if row is None:
        # Not found locally — try to proxy to public OFF
        return await _proxy_to_public(request, f"/api/v2/product/{barcode}.json")

    product_data = _filter_fields(row[0], fields)

    return JSONResponse({
        "code": barcode,
        "product": product_data,
        "status": 1,
        "status_verbose": "product found",
    })


@app.get("/api/v3/product/{barcode}")
async def product_lookup_v3(barcode: str, request: Request):
    """V3 API — same data, different wrapper."""
    return await product_lookup(barcode, request)


# ─── Text Search ─────────────────────────────────────────────────────

@app.get("/cgi/search.pl")
async def search(
    request: Request,
    search_terms: Optional[str] = Query(None, alias="search_terms"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    json_param: int = Query(1, alias="json"),
    fields: str = Query(""),
):
    """Search products by text — compatible with OFF search API."""
    if not search_terms or not search_terms.strip():
        return JSONResponse({"products": [], "count": 0, "page": page, "page_size": page_size})

    terms = search_terms.strip().split()
    # Build tsquery from search terms
    ts_query = " & ".join(terms)

    with engine.connect() as conn:
        # Use full-text search with ranking
        count_row = conn.execute(
            text("""
                SELECT count(*) FROM products
                WHERE search_vector @@ to_tsquery('french', :query)
            """),
            {"query": ts_query}
        ).fetchone()
        total_count = count_row[0] if count_row else 0

        offset = (page - 1) * page_size
        rows = conn.execute(
            text("""
                SELECT data FROM products
                WHERE search_vector @@ to_tsquery('french', :query)
                ORDER BY ts_rank(search_vector, to_tsquery('french', :query)) DESC
                LIMIT :limit OFFSET :offset
            """),
            {"query": ts_query, "limit": page_size, "offset": offset}
        ).fetchall()

    products = [_filter_fields(row[0], fields) for row in rows]

    return JSONResponse({
        "products": products,
        "count": total_count,
        "page": page,
        "page_size": page_size,
        "skip": offset,
    })


@app.get("/api/v2/search")
async def search_v2(request: Request):
    """V2 tag-based search — redirects to search.pl for compatibility."""
    params = dict(request.query_params)
    search_terms = params.get("search_terms", "")
    if search_terms:
        return await search(
            request,
            search_terms=search_terms,
            page=int(params.get("page", 1)),
            page_size=int(params.get("page_size", 20)),
            fields=params.get("fields", ""),
        )
    # Tag-based search without text terms
    return JSONResponse({"products": [], "count": 0})


# ─── Health Check ────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check — shows DB stats."""
    try:
        with engine.connect() as conn:
            count_row = conn.execute(text("SELECT count(*) FROM products")).fetchone()
            db_count = count_row[0] if count_row else 0
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "error": str(e),
            "db_count": 0,
        }, status_code=503)

    return JSONResponse({
        "status": "ok",
        "db_count": db_count,
        "version": "1.0.0",
    })


# ─── Proxy Fallback ──────────────────────────────────────────────────

async def _proxy_to_public(request: Request, path: str):
    """Proxy a request to the public OFF API for products not in local DB."""
    import httpx

    url = f"{OFF_PUBLIC_URL}{path}"
    params = dict(request.query_params)

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                return JSONResponse(resp.json())
    except httpx.RequestException:
        pass

    return JSONResponse({
        "code": path.split("/")[-1].replace(".json", ""),
        "status": 0,
        "status_verbose": "product not found",
    }, status_code=404)


# ─── Field Filtering ────────────────────────────────────────────────

def _filter_fields(product: dict, fields_str: str) -> dict:
    """Filter product dict to only include requested fields."""
    if not fields_str:
        return product

    requested = set(f.strip() for f in fields_str.split(",") if f.strip())
    # Always include essential fields
    essential = {"code", "product_name", "generic_name"}
    requested.update(essential)

    # Handle nested nutriments
    if "nutriments" in requested or any(f.startswith("nutriments") for f in requested):
        # Keep all of nutriments if requested
        pass

    return {k: v for k, v in product.items() if k in requested}
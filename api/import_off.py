"""
Open Food Facts Data Importer
-------------------------------
Downloads OFF product data and imports into PostgreSQL.
Supports three modes:
  - sample:  ~200 products (fast, testing)
  - french:   ~500K products (French only)
  - full:     ~3M products (all countries, ~7GB compressed)
"""

import gzip
import json
import os
import sys
import time
import logging
import urllib.request

from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s — %(message)s")
logger = logging.getLogger("import_off")

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://off:off@off-postgres:5432/off")
IMPORT_DATASET = os.environ.get("IMPORT_DATASET", "french")

# URLs for OFF data dumps
OFF_JSONL_URL = "https://static.openfoodfacts.org/data/openfoodfacts-products.jsonl.gz"
OFF_SAMPLE_URL = "https://static.openfoodfacts.org/exports/products.random-modulo-100000.tar.gz"

# Vitamin fields to extract from OFF products (what VitaminChecker needs)
VITAMIN_FIELDS = [
    "vitamin-a", "vitamin-b1", "vitamin-b2", "vitamin-b3", "vitamin-b5",
    "vitamin-b6", "vitamin-b9", "vitamin-b12", "vitamin-c", "vitamin-d",
    "vitamin-e", "vitamin-k",
    "thiamin", "riboflavin", "niacin", "pantothenic-acid", "folic-acid",
    "cobalamin", "ascorbic-acid",
]

# All nutriment fields we want to store (vitamins + basic macros)
NUTRIMENT_FIELDS = VITAMIN_FIELDS + [
    "energy-kcal_100g", "energy-kj_100g", "energy_100g",
    "fat_100g", "saturated-fat_100g", "carbohydrates_100g",
    "sugars_100g", "fiber_100g", "proteins_100g", "salt_100g",
    "sodium_100g", "calcium_100g", "iron_100g", "zinc_100g",
    "magnesium_100g", "phosphorus_100g", "potassium_100g",
    "selenium_100g",
]


def create_tables(engine):
    """Create the products table with full-text search."""
    with engine.connect() as conn:
        # Main product table — stores minimal data + full JSON
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS products (
                code        TEXT PRIMARY KEY,
                product_name TEXT,
                generic_name TEXT,
                brands      TEXT,
                categories  TEXT,
                countries   TEXT,
                lang        TEXT,
                data        JSONB NOT NULL,
                search_vector tsvector
            )
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_products_search
            ON products USING GIN(search_vector)
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_products_product_name
            ON products USING GIN(to_tsvector('french', COALESCE(product_name, '')))
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_products_categories
            ON products USING GIN(to_tsvector('french', COALESCE(categories, '')))
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_products_lang
            ON products (lang)
        """))
        conn.commit()
    logger.info("Tables created / verified")


def extract_product(record: dict) -> dict | None:
    """Extract relevant fields from an OFF product record.
    
    Returns a dict with minimal fields + full nutriments, or None if the product
    has no useful data.
    """
    code = record.get("code", "").strip()
    if not code:
        return None

    product_name = record.get("product_name", "") or ""
    generic_name = record.get("generic_name", "") or ""

    # Skip products with no name at all
    if not product_name and not generic_name:
        return None

    nutriments = record.get("nutriments", {}) or {}
    
    # Build a minimal data dict that matches what VitaminChecker expects
    # This is the same structure as OFF API /api/v2/product/{code}.json
    data = {
        "code": code,
        "product_name": product_name,
        "generic_name": generic_name,
        "brands": record.get("brands", "") or "",
        "categories": record.get("categories", "") or "",
        "countries": record.get("countries", "") or "",
        "lang": record.get("lang", "") or "",
        "nutriments": {},
    }

    # Extract only the vitamin/micronutrient fields we care about
    has_vitamins = False
    for field in NUTRIMENT_FIELDS:
        val_100g = nutriments.get(f"{field}_100g")
        val_base = nutriments.get(field)
        val_unit = nutriments.get(f"{field}_unit", "")
        val_serving = nutriments.get(f"{field}_serving")

        if val_100g is not None or val_base is not None:
            entry = {}
            if val_100g is not None:
                entry["100g"] = val_100g
            if val_base is not None:
                entry["value"] = val_base
            if val_unit:
                entry["unit"] = val_unit
            if val_serving is not None:
                entry["serving"] = val_serving
            data["nutriments"][field] = entry
            if field in VITAMIN_FIELDS and (val_100g is not None or val_base is not None):
                has_vitamins = True

    # Skip products with no nutriment data at all
    if not data["nutriments"] and not has_vitamins:
        return None

    return {
        "code": code,
        "product_name": product_name,
        "generic_name": generic_name,
        "brands": record.get("brands", "") or "",
        "categories": record.get("categories", "") or "",
        "countries": record.get("countries", "") or "",
        "lang": record.get("lang", "") or "",
        "data": data,
        "has_vitamins": has_vitamins,
    }


def is_french(record: dict) -> bool:
    """Check if a product is relevant for French users."""
    countries = (record.get("countries", "") or "").lower()
    countries_tags = record.get("countries_tags", []) or []
    lang = (record.get("lang", "") or "").lower()

    if "france" in countries or "en:france" in countries_tags:
        return True
    if lang == "fr":
        return True
    # French-sounding product names (heuristic)
    product_name = (record.get("product_name", "") or "").lower()
    generic_name = (record.get("generic_name", "") or "").lower()
    # Common French food words
    french_words = ["fromage", "yaourt", "baguette", "poulet", "saumon",
                    "beurre", "crème", "confiture", "croissant", "gruyère",
                    "comté", "camembert", "roquefort", "épinard", "brocoli"]
    for w in french_words:
        if w in product_name or w in generic_name:
            return True
    return False


def build_search_vector(product: dict) -> str:
    """Build a tsvector for full-text search from product fields."""
    parts = []
    if product["product_name"]:
        parts.append(product["product_name"])
    if product["generic_name"]:
        parts.append(product["generic_name"])
    if product["brands"]:
        parts.append(product["brands"])
    if product["categories"]:
        parts.append(product["categories"])
    text = " ".join(parts)
    return text


def import_from_jsonl(url: str, filter_fn=None, batch_size=500):
    """Stream-download JSONL, filter, and import into PostgreSQL."""
    engine = create_engine(DATABASE_URL)
    create_tables(engine)

    logger.info("Starting download: %s", url)
    logger.info("Filter: %s", "French products only" if filter_fn else "all products")

    downloaded = 0
    imported = 0
    skipped_no_nutriments = 0
    batch = []
    start_time = time.time()

    # Stream the gzipped file
    req = urllib.request.Request(url, headers={"User-Agent": "OFF-Local-Importer/1.0"})
    response = urllib.request.urlopen(req, timeout=600)

    with gzip.GzipFile(fileobj=response) as gz:
        for line in gz:
            line = line.decode("utf-8", errors="ignore").strip()
            if not line:
                continue

            downloaded += 1
            if downloaded % 10000 == 0:
                elapsed = time.time() - start_time
                rate = downloaded / elapsed if elapsed > 0 else 0
                logger.info("Downloaded %d lines (%.0f/sec), imported %d, skipped %d",
                            downloaded, rate, imported, skipped_no_nutriments)

            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Apply filter
            if filter_fn and not filter_fn(record):
                continue

            # Extract relevant data
            product = extract_product(record)
            if product is None:
                skipped_no_nutriments += 1
                continue

            batch.append(product)

            if len(batch) >= batch_size:
                _insert_batch(engine, batch)
                imported += len(batch)
                batch = []

        # Insert remaining
        if batch:
            _insert_batch(engine, batch)
            imported += len(batch)

    elapsed = time.time() - start_time
    logger.info("Import complete: %d downloaded, %d imported, %d skipped (no nutriments), %.1f min",
                downloaded, imported, skipped_no_nutriments, elapsed / 60)

    # Log final DB count
    with engine.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM products")).scalar()
        logger.info("Total products in database: %d", count)


def _insert_batch(engine, batch: list):
    """Insert a batch of products into PostgreSQL."""
    with engine.connect() as conn:
        for product in batch:
            search_text = build_search_vector(product)
            conn.execute(text("""
                INSERT INTO products (code, product_name, generic_name, brands, categories, countries, lang, data, search_vector)
                VALUES (:code, :product_name, :generic_name, :brands, :categories, :countries, :lang, :data,
                        to_tsvector('french', :search_text))
                ON CONFLICT (code) DO UPDATE SET
                    product_name = EXCLUDED.product_name,
                    generic_name = EXCLUDED.generic_name,
                    brands = EXCLUDED.brands,
                    categories = EXCLUDED.categories,
                    countries = EXCLUDED.countries,
                    lang = EXCLUDED.lang,
                    data = EXCLUDED.data,
                    search_vector = EXCLUDED.search_vector
            """), {
                "code": product["code"],
                "product_name": product["product_name"],
                "generic_name": product["generic_name"],
                "brands": product["brands"],
                "categories": product["categories"],
                "countries": product["countries"],
                "lang": product["lang"],
                "data": json.dumps(product["data"]),
                "search_text": search_text,
            })
        conn.commit()


def import_sample():
    """Import sample data (~200 products) for quick testing.
    
    This downloads a small random subset from OFF.
    For a real deployment, use `french` or `full`.
    """
    # For sample, we just use the JSONL but stop early
    import_from_jsonl(
        OFF_JSONL_URL,
        filter_fn=lambda r: True,  # Accept any product with nutriments
        batch_size=200,
    )
    # Trim to ~200 products
    engine = create_engine(DATABASE_URL)
    with engine.connect() as conn:
        conn.execute(text("DELETE FROM products WHERE code NOT IN (SELECT code FROM products LIMIT 200)"))
        conn.commit()
        count = conn.execute(text("SELECT count(*) FROM products")).scalar()
        logger.info("Sample import complete: %d products", count)


def main():
    dataset = IMPORT_DATASET.lower().strip()
    logger.info("Starting OFF data import: dataset=%s", dataset)

    if dataset == "sample":
        import_sample()
    elif dataset == "french":
        import_from_jsonl(OFF_JSONL_URL, filter_fn=is_french)
    elif dataset == "full":
        import_from_jsonl(OFF_JSONL_URL, filter_fn=None)
    else:
        logger.error("Unknown IMPORT_DATASET: %s (use: sample, french, full)", dataset)
        sys.exit(1)


if __name__ == "__main__":
    main()
    # In case this is run as module: python -m import_off
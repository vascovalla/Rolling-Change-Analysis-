"""Import a saved Risk PX_LAST JSON cache into this app's PostgreSQL database."""
import argparse
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psycopg
from price_history import PriceHistory, date, valid_price


def validate_cache(path):
    """Reject unusable input before touching the destination database."""
    with path.open(encoding='utf-8') as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError('History cache must be a JSON object')
    data = payload.get('data', payload)
    if not isinstance(data, dict):
        raise ValueError('History cache data must be an object')
    usable = 0
    for series in data.values():
        if not isinstance(series, dict):
            continue
        days, values = series.get('dates', []), series.get('last', series.get('values', []))
        if not isinstance(days, list) or not isinstance(values, list):
            continue
        for day, value in zip(days, values):
            try:
                date(day)
            except (ValueError, TypeError):
                continue
            usable += int(valid_price(value))
    if not usable:
        raise ValueError('History cache contains no usable price observations')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="bbg_history_cache.json or bbg_field_history__PX_LAST.json")
    args = parser.parse_args()
    try:
        path = args.path.resolve(strict=True)
        validate_cache(path)
    except (OSError, ValueError, TypeError) as error:
        parser.error(str(error))
    database = os.environ.get("DATABASE_URL")
    if not database:
        parser.error("Set DATABASE_URL in the environment first")
    def no_live_fetch(*args):
        raise RuntimeError("Import never calls the data agent")
    service = PriceHistory(lambda: psycopg.connect(database), no_live_fetch, seed_paths=[str(path)])
    affected = service.seed()
    if str(path) not in service.seeds:
        parser.error("History file could not be imported; no success recorded")
    with psycopg.connect(database) as connection:
        count = connection.execute("SELECT count(*) FROM daily_prices").fetchone()[0]
    print(f"Import completed: {affected} observations inserted or updated; {count} total saved. Newer existing prices retained. Source file unchanged.")


if __name__ == "__main__":
    main()

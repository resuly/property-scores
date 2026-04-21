"""Download Overture Maps data extracts for Australia."""

import argparse
import sys
import time

import pyarrow as pa
import pyarrow.parquet as pq

from property_scores.common.config import data_path
from property_scores.common.overture import ROADS_FILE, POIS_FILE

# Australia bounding box (generous)
AU_BBOX = (112.0, -44.0, 154.0, -10.0)

# Melbourne metro for quick testing
MELB_BBOX = (144.5, -38.1, 145.5, -37.5)


def download_overture(overture_type: str, bbox: tuple, output: str) -> int:
    try:
        import overturemaps
    except ImportError:
        print("pip install overturemaps", file=sys.stderr)
        sys.exit(1)

    out_path = data_path(output)
    print(f"Downloading {overture_type} for bbox {bbox} ...")
    print(f"Output: {out_path}")

    t0 = time.time()
    reader = overturemaps.record_batch_reader(overture_type, bbox)
    schema = reader.schema

    total = 0
    with pq.ParquetWriter(str(out_path), schema) as writer:
        while True:
            try:
                batch = reader.read_next_batch()
            except StopIteration:
                break
            if batch.num_rows == 0:
                break
            writer.write_batch(batch)
            total += batch.num_rows
            if total % 50_000 == 0:
                print(f"  {total:,} records ...")

    elapsed = time.time() - t0
    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"Done: {total:,} records, {size_mb:.1f} MB, {elapsed:.0f}s")
    return total


def main():
    parser = argparse.ArgumentParser(description="Download Overture data")
    parser.add_argument("--type", choices=["roads", "pois", "both"], default="both")
    parser.add_argument("--region", choices=["melbourne", "australia"], default="melbourne",
                        help="melbourne for quick test, australia for full")
    args = parser.parse_args()

    bbox = MELB_BBOX if args.region == "melbourne" else AU_BBOX

    if args.type in ("roads", "both"):
        download_overture("segment", bbox, ROADS_FILE)

    if args.type in ("pois", "both"):
        download_overture("place", bbox, POIS_FILE)


if __name__ == "__main__":
    main()

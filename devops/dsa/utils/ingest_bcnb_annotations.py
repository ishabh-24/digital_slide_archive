#!/usr/bin/env python3
"""
Bulk-ingest BCNB-style annotation JSON files as HistomicsUI annotations.

Each BCNB whole-slide image ships with a companion ``<slide>.json`` of the shape::

    {
      "positive": [ {"name": "Annotation 0", "vertices": [[x, y], ...]}, ... ],
      "negative": [ {"name": "...",          "vertices": [[x, y], ...]}, ... ]
    }

Every top-level key is treated as a *class*. This script converts each class into
one large_image annotation document (so it shows as an independently toggleable,
independently colored layer in HistomicsUI) whose elements are the class's
polygons, and POSTs them to::

    POST /api/v1/annotation?itemId=<slide item id>

Matching: a JSON file named ``<stem>.json`` is attached to the imported slide
Item whose filename (minus its extension) equals ``<stem>``. The ``.json`` items
that Girder created when the folder was imported are ignored as targets.

Coordinates are used as-is: BCNB vertices are level-0 pixel coordinates, which is
exactly the space HistomicsUI annotations live in — no scaling.

Requires: pip install girder-client   (optional: pip install python-dotenv)

Examples
--------
  # Dry run first — see what would match, ingest nothing:
  python ingest_bcnb_annotations.py \
      --annotations-dir /mnt/raidData/BCNB \
      --collection-id 66b0... \
      --api-url http://localhost:8080/api/v1 --api-key KEY --dry-run

  # Real run (skips slides that already have these annotations):
  python ingest_bcnb_annotations.py \
      --annotations-dir /mnt/raidData/BCNB \
      --collection-id 66b0... \
      --api-url http://localhost:8080/api/v1 --api-key KEY

  # Re-ingest, replacing any existing annotation of the same name:
  python ingest_bcnb_annotations.py ... --overwrite
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# .env support (optional)
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except ImportError:
        return
    script_dir = Path(__file__).resolve().parent
    for path in (script_dir / ".env", Path.cwd() / ".env"):
        if path.is_file():
            load_dotenv(path)
            return
    load_dotenv()


try:
    import girder_client
except ImportError:
    girder_client = None


# ---------------------------------------------------------------------------
# Conversion: BCNB JSON -> large_image annotation documents
# ---------------------------------------------------------------------------

# Deterministic colors per class. Falls back to a cycling palette for any class
# name that isn't one of the well-known BCNB ones.
_CLASS_COLORS = {
    'positive': ('rgb(255,0,0)', 'rgba(255,0,0,0.25)'),
    'negative': ('rgb(0,128,255)', 'rgba(0,128,255,0.25)'),
    'tumor': ('rgb(255,0,0)', 'rgba(255,0,0,0.25)'),
    'stroma': ('rgb(0,180,0)', 'rgba(0,180,0,0.25)'),
}
_PALETTE = [
    ('rgb(255,0,0)', 'rgba(255,0,0,0.25)'),
    ('rgb(0,128,255)', 'rgba(0,128,255,0.25)'),
    ('rgb(0,180,0)', 'rgba(0,180,0,0.25)'),
    ('rgb(200,120,0)', 'rgba(200,120,0,0.25)'),
    ('rgb(150,0,200)', 'rgba(150,0,200,0.25)'),
    ('rgb(0,160,160)', 'rgba(0,160,160,0.25)'),
]


def _colors_for(class_name, index):
    return _CLASS_COLORS.get(str(class_name).lower(), _PALETTE[index % len(_PALETTE)])


def _polygon_element(vertices, label, group, line_color, fill_color):
    """One closed polyline element from a list of [x, y] vertices."""
    points = [[float(v[0]), float(v[1]), 0.0] for v in vertices if len(v) >= 2]
    return {
        'type': 'polyline',
        'closed': True,
        'points': points,
        'label': {'value': str(label)} if label else {},
        'group': str(group),
        'lineColor': line_color,
        'lineWidth': 2,
        'fillColor': fill_color,
    }


def bcnb_json_to_annotations(data, base_name):
    """Convert a parsed BCNB JSON dict into a list of annotation documents.

    Returns one document per non-empty top-level class, each named
    ``"<base_name> - <class>"``. Skips classes with no usable polygons.
    Raises ValueError if the structure isn't the expected {class: [polygons]}.
    """
    if not isinstance(data, dict):
        raise ValueError('top-level JSON is not an object of {class: [polygons]}')

    docs = []
    for idx, (class_name, polygons) in enumerate(sorted(data.items())):
        if not isinstance(polygons, list):
            continue
        line_color, fill_color = _colors_for(class_name, idx)
        elements = []
        for poly in polygons:
            if not isinstance(poly, dict):
                continue
            verts = poly.get('vertices') or []
            if len(verts) < 3:  # need at least a triangle to be a region
                continue
            elements.append(_polygon_element(
                verts, poly.get('name', ''), class_name, line_color, fill_color))
        if not elements:
            continue
        docs.append({
            'name': '{} - {}'.format(base_name, class_name),
            'description': 'Imported from {}.json (BCNB {} regions)'.format(
                base_name, class_name),
            'elements': elements,
        })
    return docs


# ---------------------------------------------------------------------------
# Girder wiring
# ---------------------------------------------------------------------------

# Extensions treated as whole-slide images (i.e. valid annotation targets).
_WSI_EXTS = {'.svs', '.ndpi', '.tif', '.tiff', '.scn', '.mrxs', '.vms',
             '.vmu', '.svslide', '.bif', '.dcm'}


def connect(api_url, api_key):
    if girder_client is None:
        sys.exit('Install girder-client: pip install girder-client')
    if not api_key:
        sys.exit('Provide --api-key or set GIRDER_API_KEY.')
    client = girder_client.GirderClient(apiUrl=api_url.rstrip('/'))
    client.authenticate(apiKey=api_key)
    return client


def _stem(name):
    """Filename minus its final extension, lowercased for matching."""
    return os.path.splitext(name)[0].strip().lower()


def build_slide_index(client, collection_id):
    """Map slide-stem -> item, for every WSI item under the collection.

    Walks all items under the collection and keeps the ones that look like
    slides (large_image flag set, or a known WSI extension). Companion .json
    items created during folder import are never eligible targets.
    """
    index = {}
    collisions = set()
    offset = 0
    limit = 200
    while True:
        items = client.get('resource/%s/items' % collection_id,
                           parameters={'type': 'collection',
                                       'limit': limit, 'offset': offset})
        if not items:
            break
        for it in items:
            name = it.get('name', '')
            ext = os.path.splitext(name)[1].lower()
            is_slide = bool(it.get('largeImage')) or ext in _WSI_EXTS
            if not is_slide:
                continue
            stem = _stem(name)
            if stem in index:
                collisions.add(stem)
            index[stem] = it
        if len(items) < limit:
            break
        offset += limit
    return index, collisions


def existing_annotation_names(client, item_id):
    """Set of annotation names already on an item (for resumable / overwrite)."""
    try:
        anns = client.get('annotation', parameters={'itemId': item_id, 'limit': 0})
    except Exception:
        return {}
    out = {}
    for a in anns:
        nm = (a.get('annotation') or {}).get('name')
        if nm:
            out[nm] = str(a['_id'])
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    _load_dotenv()
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--annotations-dir', required=True,
                   help='Directory tree to scan recursively for *.json (host path, '
                        'e.g. /mnt/raidData/BCNB).')
    p.add_argument('--collection-id', required=True,
                   help='Girder collection id whose slide items to attach to.')
    p.add_argument('--api-url', default=os.environ.get('GIRDER_API_URL',
                   'http://localhost:8080/api/v1'))
    p.add_argument('--api-key', default=os.environ.get('GIRDER_API_KEY'))
    p.add_argument('--overwrite', action='store_true',
                   help='Replace an existing annotation of the same name instead '
                        'of skipping it.')
    p.add_argument('--dry-run', action='store_true',
                   help='Report matches and counts; POST nothing.')
    p.add_argument('--limit', type=int, default=0,
                   help='Process at most N json files (0 = all). Handy for testing.')
    args = p.parse_args()

    ann_dir = Path(args.annotations_dir)
    if not ann_dir.is_dir():
        sys.exit('Not a directory: %s' % ann_dir)

    client = connect(args.api_url, args.api_key)

    print('Indexing slide items under collection %s ...' % args.collection_id)
    index, collisions = build_slide_index(client, args.collection_id)
    print('  %d slide item(s) indexed.' % len(index))
    if collisions:
        print('  WARNING: %d stem(s) map to more than one slide (last wins): %s'
              % (len(collisions), ', '.join(sorted(collisions)[:10])))

    json_files = sorted(ann_dir.rglob('*.json'))
    if args.limit:
        json_files = json_files[:args.limit]
    print('Found %d json file(s) under %s\n' % (len(json_files), ann_dir))

    stats = {'posted': 0, 'skipped_existing': 0, 'no_match': 0,
             'no_regions': 0, 'errors': 0, 'files_ok': 0}
    unmatched = []

    for jf in json_files:
        stem = _stem(jf.name)
        item = index.get(stem)
        if not item:
            stats['no_match'] += 1
            unmatched.append(jf.name)
            continue

        try:
            data = json.loads(jf.read_text(encoding='utf-8'))
            docs = bcnb_json_to_annotations(data, os.path.splitext(item['name'])[0])
        except Exception as exc:
            stats['errors'] += 1
            print('  ! %s: parse/convert error: %s' % (jf.name, exc))
            continue

        if not docs:
            stats['no_regions'] += 1
            continue

        item_id = str(item['_id'])
        existing = {} if args.dry_run else existing_annotation_names(client, item_id)
        file_did_something = False

        for doc in docs:
            n_el = len(doc['elements'])
            if doc['name'] in existing and not args.overwrite:
                stats['skipped_existing'] += 1
                print('  = %s  (already present, skip)' % doc['name'])
                continue

            if args.dry_run:
                stats['posted'] += 1
                file_did_something = True
                print('  + %s  (%d region%s)  [dry-run]'
                      % (doc['name'], n_el, '' if n_el == 1 else 's'))
                continue

            try:
                if doc['name'] in existing and args.overwrite:
                    client.delete('annotation/%s' % existing[doc['name']])
                client.post('annotation', parameters={'itemId': item_id}, json=doc)
                stats['posted'] += 1
                file_did_something = True
                print('  + %s  ->  %s  (%d region%s)'
                      % (doc['name'], item['name'], n_el, '' if n_el == 1 else 's'))
            except Exception as exc:
                stats['errors'] += 1
                print('  ! %s: POST failed: %s' % (doc['name'], exc))

        if file_did_something:
            stats['files_ok'] += 1

    print('\n' + '=' * 60)
    print('Done%s.' % (' (dry run)' if args.dry_run else ''))
    print('  annotations posted:    %d' % stats['posted'])
    print('  files with annotations:%d' % stats['files_ok'])
    print('  skipped (existing):    %d' % stats['skipped_existing'])
    print('  json with no match:    %d' % stats['no_match'])
    print('  json with no regions:  %d' % stats['no_regions'])
    print('  errors:                %d' % stats['errors'])
    if unmatched:
        print('\nUnmatched json files (first 20):')
        for nm in unmatched[:20]:
            print('    %s' % nm)
        print('  Tip: unmatched usually means the json stem differs from the '
              'slide filename stem, or the slide was not imported.')


if __name__ == '__main__':
    main()

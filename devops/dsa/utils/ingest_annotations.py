#!/usr/bin/env python3
"""
Dataset-agnostic bulk ingestion of polygon annotation JSON into HistomicsUI.

This generalises ``ingest_bcnb_annotations.py`` along the two axes that actually
vary between datasets:

1. **How a slide is paired with its JSON** (``--manifest`` / ``--match``), and
2. **What shape the JSON is in** (auto-detected; see ``--inspect``).

Each distinct class found in a file becomes one large_image annotation document,
so it renders in HistomicsUI as an independently coloured, independently
toggleable layer.

Pairing modes
-------------
``--manifest CSV`` (preferred when the dataset ships an index, e.g. BEETLE's
``data_overview.csv``): read slide and JSON paths straight out of two columns.
No filename guessing::

    python ingest_annotations.py \
        --manifest /mnt/raidData/BEETLE/data_overview.csv \
        --slide-col wsi_path --json-col annotation_json_path \
        --path-root /mnt/raidData/BEETLE \
        --collection-id <id> --dry-run

``--annotations-dir DIR``: scan for ``*.json`` and pair by a key derived from
each filename. Default key is the filename stem; ``--slide-key-regex`` and
``--json-key-regex`` let you define an arbitrary rule, e.g. pair
``TCGA-A2-A0EM-01Z-00-DX1.<uuid>.tif`` with ``TCGA-A2-A0EM.json``::

    --slide-key-regex '^([^.]+)' --json-key-regex '^([^.]+)'

Inspecting an unknown format (no Girder connection needed)::

    python ingest_annotations.py --inspect /path/to/one.json

Requires: pip install girder-client   (optional: pip install python-dotenv)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# .env support (optional)
# ---------------------------------------------------------------------------

def _load_dotenv():
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except ImportError:
        return
    script_dir = Path(__file__).resolve().parent
    for path in (script_dir / '.env', Path.cwd() / '.env'):
        if path.is_file():
            load_dotenv(path)
            return
    load_dotenv()


try:
    import girder_client
except ImportError:
    girder_client = None


# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------

_CLASS_COLORS = {
    'positive': ('rgb(255,0,0)', 'rgba(255,0,0,0.25)'),
    'negative': ('rgb(0,128,255)', 'rgba(0,128,255,0.25)'),
    'tumor': ('rgb(255,0,0)', 'rgba(255,0,0,0.25)'),
    'invasive tumor': ('rgb(255,0,0)', 'rgba(255,0,0,0.25)'),
    'stroma': ('rgb(0,180,0)', 'rgba(0,180,0,0.25)'),
    'in-situ tumor': ('rgb(200,120,0)', 'rgba(200,120,0,0.25)'),
    'necrosis': ('rgb(90,90,90)', 'rgba(90,90,90,0.25)'),
}
_PALETTE = [
    ('rgb(255,0,0)', 'rgba(255,0,0,0.25)'),
    ('rgb(0,128,255)', 'rgba(0,128,255,0.25)'),
    ('rgb(0,180,0)', 'rgba(0,180,0,0.25)'),
    ('rgb(200,120,0)', 'rgba(200,120,0,0.25)'),
    ('rgb(150,0,200)', 'rgba(150,0,200,0.25)'),
    ('rgb(0,160,160)', 'rgba(0,160,160,0.25)'),
    ('rgb(210,0,120)', 'rgba(210,0,120,0.25)'),
]


def _colors_for(class_name, index):
    return _CLASS_COLORS.get(str(class_name).strip().lower(),
                             _PALETTE[index % len(_PALETTE)])


def _polygon_element(vertices, label, group, line_color, fill_color):
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


# ---------------------------------------------------------------------------
# JSON shape detection -> (class, label, vertices) triples
#
# Handles, without being told which is which:
#   BCNB      {"positive": [{"name":..., "vertices": [[x,y],...]}, ...], ...}
#   GeoJSON   {"type":"FeatureCollection","features":[{"geometry":{...}}]}
#   flat list [{"vertices"|"points"|"coordinates": [...], "class": "..."}]
#   wrapped   {"annotations"|"objects"|"shapes"|"regions": [ ...as above... ]}
# ---------------------------------------------------------------------------

_COORD_KEYS = ('vertices', 'points', 'coordinates', 'coords', 'polygon', 'contour')
_LIST_KEYS = ('annotations', 'objects', 'shapes', 'regions', 'features', 'items')
# Deliberately excludes 'name'/'label' (those identify one region, not its
# class) and 'type' (GeoJSON sets it to the literal "Feature").
_CLASS_KEYS = ('class', 'class_name', 'category', 'classification', 'group')


def _is_point(v):
    return (isinstance(v, (list, tuple)) and len(v) >= 2
            and all(isinstance(c, (int, float)) for c in v[:2]))


def _as_ring(value):
    """Coerce nested coordinate lists to a flat [[x, y], ...] ring."""
    if not isinstance(value, list) or not value:
        return None
    if _is_point(value[0]):
        return [v for v in value if _is_point(v)]
    # One more level of nesting: Polygon rings, or MultiPolygon.
    inner = value[0]
    if isinstance(inner, list) and inner:
        return _as_ring(inner)
    return None


def _coords_of(obj):
    if not isinstance(obj, dict):
        return None
    for key in _COORD_KEYS:
        ring = _as_ring(obj.get(key))
        if ring:
            return ring
    geom = obj.get('geometry')
    if isinstance(geom, dict):
        return _as_ring(geom.get('coordinates'))
    return None


def _class_of(obj, default, class_key=None):
    """Best-effort class name for one polygon object."""
    # Most specific first: QuPath/GeoJSON nest the real class under
    # properties.classification, and the outer object often has a generic
    # 'type' or a per-region 'name' that must not win.
    sources = []
    props = obj.get('properties')
    if isinstance(props, dict):
        nested = props.get('classification')
        if isinstance(nested, dict):
            sources.append(nested)
        sources.append(props)
    sources.append(obj)
    keys = [class_key] if class_key else _CLASS_KEYS
    for src in sources:
        for key in keys:
            if not key:
                continue
            val = src.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, dict) and isinstance(val.get('name'), str):
                return val['name'].strip()
    return default


def _walk(node, default_class, class_key, label_key, out):
    """Collect (class, label, ring) triples from an arbitrarily shaped node."""
    if isinstance(node, list):
        for item in node:
            _walk(item, default_class, class_key, label_key, out)
        return

    if not isinstance(node, dict):
        return

    ring = _coords_of(node)
    if ring and len(ring) >= 3:
        label = ''
        if label_key:
            label = node.get(label_key) or ''
        else:
            for lkey in ('name', 'label'):
                if isinstance(node.get(lkey), str):
                    label = node[lkey]
                    break
        out.append((_class_of(node, default_class, class_key), label, ring))
        return

    # A container. Recurse into known list-valued keys first; if none match,
    # treat every list-of-dicts value as a class bucket (the BCNB shape).
    recursed = False
    for key in _LIST_KEYS:
        if isinstance(node.get(key), list):
            _walk(node[key], default_class, class_key, label_key, out)
            recursed = True
    if recursed:
        return

    for key, value in node.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            # key names the class for everything inside it
            _walk(value, str(key), class_key, label_key, out)
        elif isinstance(value, (dict, list)):
            _walk(value, default_class, class_key, label_key, out)


def json_to_annotations(data, base_name, default_class='annotation',
                        class_key=None, label_key=None, scale=1.0):
    """Parsed JSON -> list of annotation documents, one per class.

    Raises ValueError if no usable polygons are found anywhere in the document.
    """
    triples = []
    _walk(data, default_class, class_key, label_key, triples)
    if not triples:
        raise ValueError('no polygons found (looked for %s under any of %s)'
                         % ('/'.join(_COORD_KEYS), '/'.join(_LIST_KEYS)))

    by_class = {}
    for class_name, label, ring in triples:
        by_class.setdefault(class_name, []).append((label, ring))

    docs = []
    for idx, class_name in enumerate(sorted(by_class)):
        line_color, fill_color = _colors_for(class_name, idx)
        elements = []
        for label, ring in by_class[class_name]:
            if scale != 1.0:
                ring = [[c[0] * scale, c[1] * scale] for c in ring]
            elements.append(_polygon_element(
                ring, label, class_name, line_color, fill_color))
        if elements:
            docs.append({
                'name': '{} - {}'.format(base_name, class_name),
                'description': 'Imported annotation ({} regions)'.format(class_name),
                'elements': elements,
            })
    return docs


# ---------------------------------------------------------------------------
# Pairing: slide item  <->  json file
# ---------------------------------------------------------------------------

_WSI_EXTS = {'.svs', '.ndpi', '.tif', '.tiff', '.scn', '.mrxs', '.vms',
             '.vmu', '.svslide', '.bif', '.dcm', '.jpg', '.jpeg', '.png'}


def _stem(name):
    return os.path.splitext(os.path.basename(str(name)))[0].strip().lower()


def _key_for(name, regex):
    """Derive the pairing key for a filename, optionally via a capture group."""
    stem = _stem(name)
    if not regex:
        return stem
    match = re.search(regex, os.path.basename(str(name)), re.IGNORECASE)
    if not match:
        return None
    return (match.group(1) if match.groups() else match.group(0)).strip().lower()


def build_slide_index(client, collection_id, key_regex=None):
    """Map pairing-key -> slide item, for every WSI item in the collection."""
    index, collisions = {}, set()
    offset, limit = 0, 200
    while True:
        items = client.get('resource/%s/items' % collection_id,
                           parameters={'type': 'collection',
                                       'limit': limit, 'offset': offset})
        if not items:
            break
        for item in items:
            name = item.get('name', '')
            ext = os.path.splitext(name)[1].lower()
            if not (item.get('largeImage') or ext in _WSI_EXTS):
                continue
            key = _key_for(name, key_regex)
            if key is None:
                continue
            if key in index:
                collisions.add(key)
            index[key] = item
        if len(items) < limit:
            break
        offset += limit
    return index, collisions


def pairs_from_manifest(manifest, slide_col, json_col, path_root, key_regex):
    """Read (key, json_path, source_row) triples out of a manifest CSV."""
    pairs, skipped = [], 0
    root = Path(path_root) if path_root else None
    with open(manifest, newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        for col in (slide_col, json_col):
            if col not in (reader.fieldnames or []):
                sys.exit('Column %r not in manifest. Available: %s'
                         % (col, ', '.join(reader.fieldnames or [])))
        for row in reader:
            slide = (row.get(slide_col) or '').strip()
            jsn = (row.get(json_col) or '').strip()
            if not slide or not jsn:
                skipped += 1      # annotation without image, or image without annotation
                continue
            path = Path(jsn)
            if not path.is_absolute() and root:
                path = root / path
            key = _key_for(slide, key_regex)
            if key is None:
                skipped += 1
                continue
            pairs.append((key, path, slide))
    return pairs, skipped


def pairs_from_dir(ann_dir, key_regex):
    pairs = []
    for path in sorted(Path(ann_dir).rglob('*.json')):
        key = _key_for(path.name, key_regex)
        if key is not None:
            pairs.append((key, path, path.name))
    return pairs


# ---------------------------------------------------------------------------
# Girder
# ---------------------------------------------------------------------------

def connect(api_url, api_key):
    if girder_client is None:
        sys.exit('Install girder-client: pip install girder-client')
    if not api_key:
        sys.exit('Provide --api-key or set GIRDER_API_KEY.')
    client = girder_client.GirderClient(apiUrl=api_url.rstrip('/'))
    client.authenticate(apiKey=api_key)
    return client


def existing_annotation_names(client, item_id):
    try:
        anns = client.get('annotation', parameters={'itemId': item_id, 'limit': 0})
    except Exception:
        return {}
    return {(a.get('annotation') or {}).get('name'): str(a['_id'])
            for a in anns if (a.get('annotation') or {}).get('name')}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _inspect(paths):
    """Print the structure of sample files so an unknown format can be read."""
    for path in paths:
        print('=' * 70)
        print(path)
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8'))
        except Exception as exc:
            print('  ! unreadable: %s' % exc)
            continue
        print('  top-level: %s' % type(data).__name__)
        if isinstance(data, dict):
            print('  keys: %s' % ', '.join(list(data)[:15]))
        elif isinstance(data, list):
            print('  length: %d' % len(data))
        triples = []
        _walk(data, 'annotation', None, None, triples)
        print('  polygons detected: %d' % len(triples))
        if triples:
            classes = {}
            for cls, _, ring in triples:
                classes.setdefault(cls, []).append(len(ring))
            for cls, sizes in sorted(classes.items()):
                print('    class %-24s %4d polygon(s), %d-%d vertices'
                      % (cls, len(sizes), min(sizes), max(sizes)))
            cls, label, ring = triples[0]
            print('  first polygon: class=%r label=%r first 3 pts=%s'
                  % (cls, label, ring[:3]))
        else:
            print('  RAW HEAD:')
            print('    ' + json.dumps(data, indent=1)[:600].replace('\n', '\n    '))


def main():
    _load_dotenv()
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--inspect', nargs='+', metavar='JSON',
                   help='Print the structure of these JSON files and exit. '
                        'No Girder connection required.')
    p.add_argument('--manifest', help='CSV mapping slides to annotation JSONs.')
    p.add_argument('--slide-col', default='wsi_path',
                   help='Manifest column holding the slide path/name.')
    p.add_argument('--json-col', default='annotation_json_path',
                   help='Manifest column holding the annotation JSON path.')
    p.add_argument('--path-root', help='Directory that manifest relative paths '
                                       'are resolved against.')
    p.add_argument('--annotations-dir', help='Scan this tree for *.json instead '
                                             'of using a manifest.')
    p.add_argument('--slide-key-regex', help='Regex applied to the slide filename '
                                             'to derive the pairing key (group 1 '
                                             'if present).')
    p.add_argument('--json-key-regex', help='Regex applied to the JSON filename '
                                            'to derive the pairing key.')
    p.add_argument('--collection-id', help='Girder collection holding the slides.')
    p.add_argument('--class-key', help='Force this JSON field as the class name.')
    p.add_argument('--label-key', help='Force this JSON field as the per-region label.')
    p.add_argument('--scale', type=float, default=1.0,
                   help='Multiply every coordinate by this factor (default 1.0 — '
                        'use when JSON coords are not level-0 pixels).')
    p.add_argument('--api-url', default=os.environ.get(
        'GIRDER_API_URL', 'http://localhost:8080/api/v1'))
    p.add_argument('--api-key', default=os.environ.get('GIRDER_API_KEY'))
    p.add_argument('--overwrite', action='store_true',
                   help='Replace an existing annotation of the same name.')
    p.add_argument('--dry-run', action='store_true', help='POST nothing.')
    p.add_argument('--limit', type=int, default=0, help='Process at most N files.')
    args = p.parse_args()

    if args.inspect:
        _inspect(args.inspect)
        return

    if not args.collection_id:
        sys.exit('--collection-id is required (or use --inspect).')
    if bool(args.manifest) == bool(args.annotations_dir):
        sys.exit('Give exactly one of --manifest or --annotations-dir.')

    client = connect(args.api_url, args.api_key)

    print('Indexing slide items under collection %s ...' % args.collection_id)
    index, collisions = build_slide_index(client, args.collection_id,
                                          args.slide_key_regex)
    print('  %d slide item(s) indexed.' % len(index))
    if collisions:
        print('  WARNING: %d key(s) match more than one slide (last wins): %s'
              % (len(collisions), ', '.join(sorted(collisions)[:5])))

    if args.manifest:
        pairs, incomplete = pairs_from_manifest(
            args.manifest, args.slide_col, args.json_col,
            args.path_root, args.slide_key_regex)
        print('Manifest %s: %d row(s) with both slide and JSON, %d skipped '
              '(missing one side).' % (args.manifest, len(pairs), incomplete))
    else:
        pairs = pairs_from_dir(args.annotations_dir, args.json_key_regex)
        print('Found %d json file(s) under %s' % (len(pairs), args.annotations_dir))

    if args.limit:
        pairs = pairs[:args.limit]
    print()

    stats = dict(posted=0, skipped_existing=0, no_match=0, missing_file=0,
                 no_regions=0, errors=0, files_ok=0)
    unmatched, missing = [], []

    for key, json_path, source in pairs:
        item = index.get(key)
        if not item:
            stats['no_match'] += 1
            unmatched.append(source)
            continue
        if not Path(json_path).is_file():
            stats['missing_file'] += 1
            missing.append(str(json_path))
            continue

        try:
            data = json.loads(Path(json_path).read_text(encoding='utf-8'))
            docs = json_to_annotations(
                data, os.path.splitext(item['name'])[0],
                class_key=args.class_key, label_key=args.label_key,
                scale=args.scale)
        except Exception as exc:
            stats['errors'] += 1
            print('  ! %s: %s' % (os.path.basename(str(json_path)), exc))
            continue

        if not docs:
            stats['no_regions'] += 1
            continue

        item_id = str(item['_id'])
        existing = {} if args.dry_run else existing_annotation_names(client, item_id)
        did_something = False

        for doc in docs:
            count = len(doc['elements'])
            if doc['name'] in existing and not args.overwrite:
                stats['skipped_existing'] += 1
                print('  = %s  (already present, skip)' % doc['name'])
                continue
            if args.dry_run:
                stats['posted'] += 1
                did_something = True
                print('  + %s  (%d region%s)  [dry-run]'
                      % (doc['name'], count, '' if count == 1 else 's'))
                continue
            try:
                if doc['name'] in existing and args.overwrite:
                    client.delete('annotation/%s' % existing[doc['name']])
                client.post('annotation', parameters={'itemId': item_id}, json=doc)
                stats['posted'] += 1
                did_something = True
                print('  + %s  ->  %s  (%d region%s)'
                      % (doc['name'], item['name'], count,
                         '' if count == 1 else 's'))
            except Exception as exc:
                stats['errors'] += 1
                print('  ! %s: POST failed: %s' % (doc['name'], exc))

        if did_something:
            stats['files_ok'] += 1

    print('\n' + '=' * 60)
    print('Done%s.' % (' (dry run)' if args.dry_run else ''))
    print('  annotations posted:     %d' % stats['posted'])
    print('  slides with annotations:%d' % stats['files_ok'])
    print('  skipped (existing):     %d' % stats['skipped_existing'])
    print('  no matching slide:      %d' % stats['no_match'])
    print('  json file not on disk:  %d' % stats['missing_file'])
    print('  json with no regions:   %d' % stats['no_regions'])
    print('  errors:                 %d' % stats['errors'])
    if unmatched:
        print('\nNo slide item matched (first 10):')
        for name in unmatched[:10]:
            print('    %s' % name)
        print('  These usually are annotation-only rows, or slides not imported.')
    if missing:
        print('\nJSON path did not exist (first 10):')
        for name in missing[:10]:
            print('    %s' % name)
        print('  Check --path-root.')


if __name__ == '__main__':
    main()

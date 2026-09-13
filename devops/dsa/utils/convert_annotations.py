#!/usr/bin/env python3
r"""
Convert existing annotation files into the DSA annotation format (v1).

The upload accepts only the standard format, so this tool is where lenient,
dataset-specific parsing lives. Every file it writes has already passed the
format's validator, so a converted file passes the schema at upload. Checks
that need the target slide or its collection (E-BOUNDS, E-SIZE, W-SLIDE,
E-VOCAB) still run when the file is uploaded.

Source adapters
---------------
beetle  [{"index": 0, "coordinates": [[x, y], ...],
          "label": {"name": "invasive epithelium", "value": 1}}, ...]
bcnb    {"<class>": [{"name": "Annotation 0", "vertices": [[x, y], ...]}, ...]}
auto    best-effort shape detection from ingest_annotations.py (polygons only)

Regions with fewer than 3 distinct vertices are dropped and counted. Negative
coordinates fail the file unless --clip-negative is given, since clamping
changes the geometry.

Examples
--------
One file:

    python convert_annotations.py --from beetle \
        --in /mnt/raidData/BEETLE/annotations/jsons/patient1_wsi1.json \
        --slide-name patient1_wsi1.tif

A whole dataset, paired through its manifest:

    python convert_annotations.py --from beetle \
        --manifest /mnt/raidData/BEETLE/data_overview.csv \
        --slide-col wsi_path --json-col annotation_json_path \
        --path-root /mnt/raidData/BEETLE \
        --out-dir ~/beetle_dsa

Requires: pip install jsonschema
"""

import argparse
import datetime
import json
import math
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / 'dsa_csv_plugin' / 'dsa_csv_plugin'))
sys.path.insert(0, str(_HERE))

import annotation_format  # noqa: E402
import ingest_annotations  # noqa: E402

CONVERTER = 'convert_annotations.py 1.0'
MAX_LABEL_LENGTH = 200


class AdapterError(ValueError):
    """A file couldn't be converted; the message says why."""


class Region(object):
    __slots__ = ('cls', 'ring', 'description', 'label', 'region_id')

    def __init__(self, cls, ring, description=None, label=None, region_id=None):
        self.cls = cls
        self.ring = ring
        self.description = description
        self.label = label
        self.region_id = region_id


# ---------------------------------------------------------------------------
# Source adapters: parsed JSON -> list of Region
# ---------------------------------------------------------------------------

def _is_number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _ring(value, where):
    if not isinstance(value, list) or not value:
        raise AdapterError('%s: coordinates must be a non-empty list of [x, y] pairs' % where)
    ring = []
    for index, position in enumerate(value):
        if not (isinstance(position, (list, tuple)) and len(position) >= 2
                and _is_number(position[0]) and _is_number(position[1])):
            raise AdapterError('%s: vertex %d is not an [x, y] pair of numbers: %s'
                               % (where, index, json.dumps(position)[:60]))
        ring.append([position[0], position[1]])
    return ring


def _region_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) or isinstance(value, str):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def adapt_beetle(data):
    if not isinstance(data, list):
        raise AdapterError('expected a top-level list of regions (BEETLE format), got a %s'
                           % annotation_format._json_type(data))
    regions = []
    for index, item in enumerate(data):
        where = 'region %d' % index
        if not isinstance(item, dict) or 'coordinates' not in item:
            raise AdapterError('%s: missing "coordinates"' % where)
        label = item.get('label')
        if not (isinstance(label, dict) and isinstance(label.get('name'), str)
                and label['name'].strip()):
            raise AdapterError('%s: missing label.name (the class)' % where)
        description = None
        if 'value' in label:
            description = 'BEETLE label value %s' % label['value']
        regions.append(Region(label['name'].strip(), _ring(item['coordinates'], where),
                              description=description,
                              region_id=_region_id(item.get('index', index))))
    return regions


def adapt_bcnb(data):
    if not isinstance(data, dict):
        raise AdapterError('expected a top-level object of {class: [regions]} (BCNB format), '
                           'got a %s' % annotation_format._json_type(data))
    regions = []
    for cls, items in data.items():
        if not isinstance(items, list):
            raise AdapterError('class "%s": expected a list of regions' % cls)
        for index, item in enumerate(items):
            where = 'class "%s", region %d' % (cls, index)
            if not isinstance(item, dict) or 'vertices' not in item:
                raise AdapterError('%s: missing "vertices"' % where)
            name = item.get('name')
            regions.append(Region(str(cls).strip(), _ring(item['vertices'], where),
                                  label=name if isinstance(name, str) and name else None))
    return regions


def adapt_auto(data):
    triples = []
    ingest_annotations._walk(data, 'annotation', None, None, triples)
    if not triples:
        raise AdapterError('no polygons found; name the source format with --from instead')
    return [Region(cls, _ring(ring, 'region %d' % index), label=label or None)
            for index, (cls, label, ring) in enumerate(triples)]


ADAPTERS = {'beetle': adapt_beetle, 'bcnb': adapt_bcnb, 'auto': adapt_auto}


# ---------------------------------------------------------------------------
# Regions -> standard document
# ---------------------------------------------------------------------------

def build_document(regions, slide_name, source_format, source_file, clip_negative=False):
    """Returns ``(document, notes)``. Raises AdapterError if nothing usable remains."""
    classes = {}
    features = []
    dropped = clipped = 0
    for index, region in enumerate(regions):
        ring = region.ring
        negatives = sum(1 for position in ring for value in position if value < 0)
        if negatives:
            if not clip_negative:
                bad = next(position for position in ring if position[0] < 0 or position[1] < 0)
                raise AdapterError('region %s has a negative coordinate %s; rerun with '
                                   '--clip-negative to clamp to 0'
                                   % (region.region_id if region.region_id is not None else index,
                                      annotation_format._pos(bad)))
            ring = [[max(position[0], 0), max(position[1], 0)] for position in ring]
            clipped += negatives
        if len({(position[0], position[1]) for position in ring}) < 3:
            dropped += 1
            continue
        if ring[0] != ring[-1]:
            ring = ring + [list(ring[0])]

        entry = classes.setdefault(region.cls, {})
        if region.description and 'description' not in entry:
            entry['description'] = region.description

        feature = {'type': 'Feature'}
        if region.region_id is not None:
            feature['id'] = region.region_id
        feature['geometry'] = {'type': 'Polygon', 'coordinates': [ring]}
        properties = {'class': region.cls}
        if region.label:
            properties['label'] = str(region.label)[:MAX_LABEL_LENGTH]
        feature['properties'] = properties
        features.append(feature)

    if not features:
        raise AdapterError('no usable regions (%d dropped with fewer than 3 distinct vertices)'
                           % dropped)

    document = {
        'type': 'FeatureCollection',
        'properties': {
            'format': annotation_format.FORMAT_NAME,
            'version': annotation_format.FORMAT_VERSION,
            'coordinate_space': 'level0_pixels',
            'slide': {'name': slide_name},
            'classes': {name: classes[name] for name in sorted(classes)},
            'provenance': {
                'source_format': source_format,
                'source_file': source_file,
                'converter': CONVERTER,
                'converted_at': datetime.datetime.now(datetime.timezone.utc)
                .replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
            },
        },
        'features': features,
    }
    return document, {'features': len(features), 'dropped': dropped, 'clipped': clipped}


def convert_file(path, source_format, slide_name, source_file, clip_negative=False):
    """Convert one file. Returns ``(document, notes)`` or raises AdapterError."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise AdapterError('could not read JSON: %s' % exc)
    regions = ADAPTERS[source_format](data)
    document, notes = build_document(regions, slide_name, source_format, source_file,
                                     clip_negative)
    report = annotation_format.validate(document)
    if not report.ok:
        raise AdapterError('converted document fails validation: %s'
                           % '; '.join(issue.message for issue in report.errors[:3]))
    return document, notes


def _write_json(path, document, pretty):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with open(temporary, 'w', encoding='utf-8') as handle:
        json.dump(document, handle, indent=1 if pretty else None, ensure_ascii=False)
        handle.write('\n')
    os.replace(temporary, path)


def _stem(name):
    return os.path.splitext(os.path.basename(str(name)))[0]


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--from', dest='source', required=True, choices=sorted(ADAPTERS),
                        help='Format of the input files.')
    single = parser.add_argument_group('one file')
    single.add_argument('--in', dest='input', help='Annotation file to convert.')
    single.add_argument('--slide-name', help='Slide file these annotations belong to.')
    single.add_argument('--out', help='Output path (default: ./<slide stem>.dsa.json).')
    bulk = parser.add_argument_group('many files, paired through a manifest')
    bulk.add_argument('--manifest', help='CSV mapping slides to annotation files.')
    bulk.add_argument('--slide-col', default='wsi_path')
    bulk.add_argument('--json-col', default='annotation_json_path')
    bulk.add_argument('--path-root', help='Directory manifest paths are relative to.')
    bulk.add_argument('--out-dir', help='Directory for <slide stem>.dsa.json outputs.')
    bulk.add_argument('--limit', type=int, default=0, help='Convert at most N files.')
    parser.add_argument('--clip-negative', action='store_true',
                        help='Clamp negative coordinates to 0 instead of failing the file.')
    parser.add_argument('--overwrite', action='store_true',
                        help='Replace existing output files (default: skip them).')
    parser.add_argument('--pretty', action='store_true', help='Indent the output JSON.')
    args = parser.parse_args(argv)

    if bool(args.input) == bool(args.manifest):
        parser.error('give exactly one of --in or --manifest')

    if args.input:
        if not args.slide_name:
            parser.error('--in needs --slide-name (the slide file these annotations belong to)')
        out = Path(args.out) if args.out else Path.cwd() / ('%s.dsa.json' % _stem(args.slide_name))
        jobs = [(Path(args.input), args.slide_name, Path(args.input).name, out)]
    else:
        if not args.out_dir:
            parser.error('--manifest needs --out-dir')
        pairs, incomplete = ingest_annotations.pairs_from_manifest(
            args.manifest, args.slide_col, args.json_col, args.path_root, None)
        print('Manifest: %d row(s) with both a slide and an annotation file, '
              '%d skipped (missing one side).' % (len(pairs), incomplete))
        root = Path(args.path_root).resolve() if args.path_root else None
        jobs = []
        for _, json_path, slide in pairs:
            try:
                label = str(Path(json_path).resolve().relative_to(root)) if root else str(json_path)
            except ValueError:
                label = str(json_path)
            slide_name = os.path.basename(slide)
            jobs.append((Path(json_path), slide_name, label,
                         Path(args.out_dir).expanduser() / ('%s.dsa.json' % _stem(slide_name))))
        if args.limit:
            jobs = jobs[:args.limit]

    stats = dict(converted=0, skipped=0, failed=0, features=0, dropped=0, clipped=0)
    failures = []
    claimed = set()
    for json_path, slide_name, label, out in jobs:
        if out in claimed:
            stats['failed'] += 1
            failures.append((label, 'another row already writes %s' % out.name))
            continue
        claimed.add(out)
        if out.exists() and not args.overwrite:
            stats['skipped'] += 1
            continue
        try:
            document, notes = convert_file(json_path, args.source, slide_name, label,
                                           args.clip_negative)
        except AdapterError as exc:
            stats['failed'] += 1
            failures.append((label, str(exc)))
            print('  ! %s: %s' % (label, exc))
            continue
        _write_json(out, document, args.pretty)
        stats['converted'] += 1
        stats['features'] += notes['features']
        stats['dropped'] += notes['dropped']
        stats['clipped'] += notes['clipped']
        extra = []
        if notes['dropped']:
            extra.append('%d degenerate dropped' % notes['dropped'])
        if notes['clipped']:
            extra.append('%d negative values clipped' % notes['clipped'])
        print('  + %s  (%d features%s)' % (out, notes['features'],
                                            '; ' + ', '.join(extra) if extra else ''))

    print('\nDone.')
    print('  converted:          %d' % stats['converted'])
    print('  skipped (existing): %d' % stats['skipped'])
    print('  failed:             %d' % stats['failed'])
    print('  features written:   %d' % stats['features'])
    print('  degenerate dropped: %d' % stats['dropped'])
    print('  negatives clipped:  %d' % stats['clipped'])
    if failures:
        print('\nFailures (first 10):')
        for label, reason in failures[:10]:
            print('  %s: %s' % (label, reason))
    return 1 if stats['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())

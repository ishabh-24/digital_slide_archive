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
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / 'dsa_csv_plugin'))
sys.path.insert(0, str(_HERE))

import ingest_annotations  # noqa: E402

from dsa_csv_plugin import converters  # noqa: E402
from dsa_csv_plugin.converters import (  # noqa: E402,F401  (re-exported for callers and tests)
    AdapterError, Region, build_document, ring_from)

CONVERTER = 'convert_annotations.py 1.0'


def adapt_auto(data):
    """Best-effort shape detection, for files that are neither BEETLE nor BCNB."""
    triples = []
    ingest_annotations._walk(data, 'annotation', None, None, triples)
    if not triples:
        raise AdapterError('no polygons found; name the source format with --from instead')
    return [Region(cls, ring_from(ring, 'region %d' % index), label=label or None)
            for index, (cls, label, ring) in enumerate(triples)]


ADAPTERS = dict(converters.ADAPTERS, auto=adapt_auto)


def convert_file(path, source_format, slide_name, source_file, clip_negative=False):
    """Convert one file. Returns ``(document, notes)`` or raises AdapterError."""
    try:
        text = Path(path).read_text(encoding='utf-8')
    except OSError as exc:
        raise AdapterError('could not read JSON: %s' % exc)
    return converters.convert_text(text, source_format, slide_name, source_file,
                                   clip_negative, converter=CONVERTER, adapters=ADAPTERS)


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

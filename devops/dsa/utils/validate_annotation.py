#!/usr/bin/env python3
r"""
Check annotation files against the DSA annotation format (v1).

Runs the same checks as the upload. Checks that compare against the target
slide or collection run only when you supply that information:

  --slide-name, --slide-size   enable W-SLIDE, E-SIZE and E-BOUNDS
  --vocabulary                 enables E-VOCAB

Examples
--------
  python validate_annotation.py ~/beetle_dsa/*.dsa.json --quiet

  python validate_annotation.py patient1_wsi1.dsa.json \
      --slide-name patient1_wsi1.tif --slide-size 98000x112000

Exits 1 if any file has errors. Requires: pip install jsonschema
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'dsa_csv_plugin'))

from dsa_csv_plugin import annotation_format  # noqa: E402


def _size(text):
    try:
        width, height = text.lower().split('x')
        return int(width), int(height)
    except ValueError:
        raise argparse.ArgumentTypeError(
            'expected WIDTHxHEIGHT in level-0 pixels, e.g. 98000x112000')


def _vocabulary(path):
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if isinstance(data, dict) and isinstance(data.get('annotationClasses'), dict):
        data = data['annotationClasses']
    if not isinstance(data, dict):
        raise SystemExit('%s: expected a JSON object keyed by class name' % path)
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('files', nargs='+', metavar='FILE')
    parser.add_argument('--slide-name', help='Name of the target slide item.')
    parser.add_argument('--slide-size', type=_size, help='Target slide size as WIDTHxHEIGHT.')
    parser.add_argument('--vocabulary', help='JSON file holding the collection class vocabulary.')
    parser.add_argument('--collection-name', help='Collection name, used in messages.')
    parser.add_argument('--quiet', action='store_true', help='Only print files with errors.')
    args = parser.parse_args(argv)

    slide = None
    if args.slide_name or args.slide_size:
        slide = {}
        if args.slide_name:
            slide['name'] = args.slide_name
        if args.slide_size:
            slide['sizeX'], slide['sizeY'] = args.slide_size
    options = {'slide': slide, 'collection_name': args.collection_name}
    if args.vocabulary:
        options['vocabulary'] = _vocabulary(args.vocabulary)

    invalid = 0
    for name in args.files:
        try:
            text = Path(name).read_text(encoding='utf-8')
        except OSError as exc:
            invalid += 1
            print('%s  UNREADABLE  %s' % (name, exc))
            continue
        _, report = annotation_format.validate_json_text(text, **options)
        if not report.ok:
            invalid += 1
        elif args.quiet:
            continue
        print('%s  %s' % (name, 'OK' if report.ok else 'INVALID'))
        for issue in report.issues:
            print('  %-7s %-9s %s' % (issue.severity.upper(), issue.code, issue.message))

    total = len(args.files)
    print('\n%d file(s): %d valid, %d invalid' % (total, total - invalid, invalid))
    return 1 if invalid else 0


if __name__ == '__main__':
    sys.exit(main())

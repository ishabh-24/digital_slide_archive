#!/usr/bin/env python3
"""
End-to-end check of the strict annotation upload against a running DSA.

Run on the server, from devops/dsa/utils, after `git pull` and a rebuild::

    python smoke_test_annotation_upload.py            # uses patient1_wsi1.tif
    python smoke_test_annotation_upload.py patient3_wsi1.tif

It uses ONE BEETLE slide. The only things it writes are the BEETLE collection's
class vocabulary and that slide's annotation layers (replacing same-named
layers). Every other step is validate-only.

Needs: ~/beetle_dsa/<slide>.dsa.json (from convert_annotations.py) and the
API key in ./.env.
"""
import copy
import json
import os
import sys

import girder_client

API = os.environ.get('GIRDER_API_URL', 'http://localhost:8080/api/v1')
SLIDE = sys.argv[1] if len(sys.argv) > 1 else 'patient1_wsi1.tif'
STEM = SLIDE.rsplit('.', 1)[0]
CONVERTED = os.path.expanduser('~/beetle_dsa/%s.dsa.json' % STEM)
RAW = '/mnt/raidData/BEETLE/annotations/jsons/%s.json' % STEM
VOCABULARY = {
    'invasive epithelium': {'color': '#FF0000'},
    'non-invasive epithelium': {'color': '#C87800'},
    'necrosis': {'color': '#5A5A5A'},
    'other': {'color': '#9600C8'},
}

passed = failed = 0


def check(condition, label, detail=''):
    global passed, failed
    if condition:
        passed += 1
        print('  PASS  %s' % label)
    else:
        failed += 1
        print('  FAIL  %s%s' % (label, ('\n        ' + detail) if detail else ''))
    return condition


def step(title):
    print('\n== %s' % title)


def codes(result, severity):
    return sorted({issue['code'] for issue in result.get(severity, [])})


def api_key():
    key = os.environ.get('GIRDER_API_KEY')
    if not key and os.path.isfile('.env'):
        for line in open('.env'):
            if line.startswith('GIRDER_API_KEY='):
                key = line.split('=', 1)[1].strip()
    if not key:
        sys.exit('No API key: set GIRDER_API_KEY or put it in ./.env')
    return key


def find_items(gc, name):
    """Every item called ``name`` in any collection.

    Lists items rather than using resource/search: Girder's text search
    tokenizes names, and ``patient1_wsi1.tif`` doesn't match itself there.
    """
    found = []
    for collection in gc.get('collection', parameters={'limit': 0}):
        offset = 0
        while True:
            page = gc.get('resource/%s/items' % collection['_id'],
                          parameters={'type': 'collection', 'limit': 500, 'offset': offset})
            found.extend(i for i in page if i['name'] == name)
            if len(page) < 500:
                break
            offset += 500
    return found


def upload(gc, item_id, text, **options):
    body = dict({'json_content': text}, **options)
    return gc.post('dsa_tools/item/%s/ingest_annotation_json' % item_id, json=body)


def main():
    if not os.path.isfile(CONVERTED):
        sys.exit('Missing %s\nRun convert_annotations.py first (see SPEC.md).' % CONVERTED)

    gc = girder_client.GirderClient(apiUrl=API)
    gc.authenticate(apiKey=api_key())

    step('server')
    version = gc.get('system/version')
    print('  girder %s at %s' % (version.get('release'), API))
    me = gc.get('user/me')
    check(me is not None, 'API key authenticates (user %s)' % (me or {}).get('login'))

    step('find the slide')
    items = find_items(gc, SLIDE)
    wsis = [i for i in items if gc.get('folder/%s' % i['folderId'])['name'] == 'wsis']
    if not check(len(wsis) == 1, 'exactly one %s under a "wsis" folder (found %d, %d total same-name)'
                 % (SLIDE, len(wsis), len(items))):
        sys.exit(1)
    item = wsis[0]
    item_id = str(item['_id'])
    collection = gc.get('collection/%s' % item['baseParentId'])
    print('  item %s in collection %s' % (item_id, collection['name']))
    tiles = gc.get('item/%s/tiles' % item_id)
    size_x, size_y = tiles['sizeX'], tiles['sizeY']
    check(size_x > 0 and size_y > 0, 'slide has a tile source (%d x %d)' % (size_x, size_y))

    step('strict route is live (rebuild happened)')
    probe = upload(gc, item_id, '[]', validate_only=True)
    if not check('errors' in probe and 'ok' in probe,
                 'route returns the new ok/errors/warnings shape',
                 'got keys %s -- the image was not rebuilt after the pull' % sorted(probe)):
        sys.exit(1)

    step('collection vocabulary')
    gc.put('collection/%s/metadata' % collection['_id'], json={'annotationClasses': VOCABULARY})
    stored = gc.get('collection/%s' % collection['_id']).get('meta', {}).get('annotationClasses')
    check(stored == VOCABULARY, 'annotationClasses saved on %s' % collection['name'])

    converted_text = open(CONVERTED, encoding='utf-8').read()
    converted = json.loads(converted_text)

    step('validate only: converted file')
    result = upload(gc, item_id, converted_text, validate_only=True)
    check(result['ok'], 'converted file is valid', json.dumps(result.get('errors'))[:300])
    check(result['annotations_created'] == [], 'validate_only wrote nothing')
    layers = [l['name'] for l in result['layers']]
    print('  would create: %s' % ', '.join(layers))
    check(all(name.startswith(STEM + ' - ') for name in layers) and layers,
          'layers are named "<slide> - <class>"')
    check(codes(result, 'warnings') == [], 'no warnings', json.dumps(result.get('warnings'))[:300])

    step('reject: raw BEETLE file (not the standard format)')
    if os.path.isfile(RAW):
        result = upload(gc, item_id, open(RAW, encoding='utf-8').read(), validate_only=True)
        check(not result['ok'] and codes(result, 'errors') == ['E-SCHEMA'],
              'rejected with E-SCHEMA only (got %s)' % codes(result, 'errors'))
    else:
        print('  skipped: %s not found' % RAW)

    step('reject: class not in vocabulary')
    doc = copy.deepcopy(converted)
    bad = next(iter(doc['properties']['classes']))
    renamed = bad.upper()
    doc['properties']['classes'][renamed] = doc['properties']['classes'].pop(bad)
    for feature in doc['features']:
        if feature['properties']['class'] == bad:
            feature['properties']['class'] = renamed
    result = upload(gc, item_id, json.dumps(doc), validate_only=True)
    check(not result['ok'] and 'E-VOCAB' in codes(result, 'errors'),
          'rejected with E-VOCAB (got %s)' % codes(result, 'errors'))
    messages = ' | '.join(e['message'] for e in result.get('errors', []))
    check('did you mean "%s"' % bad in messages, 'suggests the correct spelling', messages[:200])

    step('reject: coordinates in the wrong space')
    doc = copy.deepcopy(converted)
    for feature in doc['features']:
        for ring in feature['geometry']['coordinates']:
            for position in ring:
                position[0] *= 40
                position[1] *= 40
    result = upload(gc, item_id, json.dumps(doc), validate_only=True)
    check(not result['ok'] and 'E-BOUNDS' in codes(result, 'errors'),
          'rejected with E-BOUNDS (got %s)' % codes(result, 'errors'))
    first = next((e['message'] for e in result.get('errors', []) if e['code'] == 'E-BOUNDS'), '')
    check('not level-0 pixels' in first, 'message explains the likely cause', first[:200])

    step('real upload (replace existing layers)')
    result = upload(gc, item_id, converted_text, replace=True)
    created = [c['name'] for c in result['annotations_created']]
    check(result['ok'] and created == layers, 'created %d layer(s): %s' % (len(created), ', '.join(created)))

    step('read back from Girder')
    stored = gc.get('annotation', parameters={'itemId': item_id, 'limit': 0})
    ours = [a for a in stored if a['annotation']['name'] in layers]
    others = [a['annotation']['name'] for a in stored if a['annotation']['name'] not in layers]
    if others:
        print('  (left untouched, not part of this test: %s)' % ', '.join(others))
    names = sorted(a['annotation']['name'] for a in ours)
    check(names == sorted(layers), 'stored layers match (%s)' % ', '.join(names))
    inside = True
    for a in ours:
        full = gc.get('annotation/%s' % a['_id'])
        elements = full['annotation']['elements']
        xs = [p[0] for e in elements for p in e.get('points', [e.get('center')]) if p]
        ys = [p[1] for e in elements for p in e.get('points', [e.get('center')]) if p]
        hole_count = sum(len(e.get('holes', [])) for e in elements)
        print('  %-45s %5d elements  x %.0f..%.0f  y %.0f..%.0f  holes %d  color %s'
              % (full['annotation']['name'], len(elements), min(xs), max(xs), min(ys), max(ys),
                 hole_count, elements[0].get('lineColor')))
        inside &= max(xs) <= size_x and max(ys) <= size_y
        expected = next((VOCABULARY[c]['color'] for c in VOCABULARY
                         if full['annotation']['name'].endswith(' - ' + c)), None)
        if expected:
            r, g, b = int(expected[1:3], 16), int(expected[3:5], 16), int(expected[5:7], 16)
            check(elements[0].get('lineColor') == 'rgb(%d,%d,%d)' % (r, g, b),
                  'color from vocabulary for %s' % full['annotation']['name'].split(' - ')[-1])
    check(inside, 'every coordinate lies inside the %d x %d image' % (size_x, size_y))

    print('\n%d passed, %d failed' % (passed, failed))
    print('\nNow look at it:\n  %s/histomics#?image=%s' % (API.replace('/api/v1', ''), item_id))
    print('  Tick the %d layers in the Annotations panel (they start hidden).' % len(layers))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

"""
Convert dataset-specific annotation files into the DSA annotation format (v1).

The upload accepts only the standard format, so this is where lenient,
dataset-specific parsing lives. Every document produced here has passed the
format's validator; the checks that need the target slide or its collection
(E-BOUNDS, E-SIZE, W-SLIDE, E-VOCAB) still run at upload.

Used by the /annotation_convert page (via rest.py) and by the command-line
``utils/convert_annotations.py``, which adds an ``auto`` adapter on top.

Adapters
--------
beetle  [{"index": 0, "coordinates": [[x, y], ...],
          "label": {"name": "invasive epithelium", "value": 1}}, ...]
bcnb    {"<class>": [{"name": "Annotation 0", "vertices": [[x, y], ...]}, ...]}
"""

import datetime
import json
import math

from . import annotation_format

CONVERTER = 'dsa_csv_plugin converters 1.0'
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


def ring_from(value, where):
    """Coerce a list of [x, y] pairs to a ring, or raise AdapterError."""
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
        regions.append(Region(label['name'].strip(), ring_from(item['coordinates'], where),
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
            regions.append(Region(str(cls).strip(), ring_from(item['vertices'], where),
                                  label=name if isinstance(name, str) and name else None))
    return regions


ADAPTERS = {'beetle': adapt_beetle, 'bcnb': adapt_bcnb}

# Shown in the convert page's format picker and in --help.
ADAPTER_DESCRIPTIONS = {
    'beetle': 'BEETLE: list of {coordinates, label: {name, value}}',
    'bcnb': 'BCNB: {class: [{name, vertices}]}',
}


# ---------------------------------------------------------------------------
# Regions -> standard document
# ---------------------------------------------------------------------------

def build_document(regions, slide_name, source_format, source_file, clip_negative=False,
                   converter=CONVERTER):
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
                raise AdapterError('region %s has a negative coordinate %s; clip negative '
                                   'coordinates (--clip-negative) to clamp to 0'
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
                'converter': converter,
                'converted_at': datetime.datetime.now(datetime.timezone.utc)
                .replace(microsecond=0).isoformat().replace('+00:00', 'Z'),
            },
        },
        'features': features,
    }
    notes = {'features': len(features), 'dropped': dropped, 'clipped': clipped,
             'classes': sorted(classes)}
    return document, notes


def convert_text(text, source_format, slide_name, source_file, clip_negative=False,
                 converter=CONVERTER, adapters=None):
    """Convert the text of one source file. Returns ``(document, notes)``.

    Raises AdapterError for anything wrong with the input, and validates the
    output before returning it so a converted document always passes the
    format's schema.
    """
    adapters = adapters or ADAPTERS
    if source_format not in adapters:
        raise AdapterError('unknown source format %r; choose one of %s'
                           % (source_format, ', '.join(sorted(adapters))))
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise AdapterError('could not read JSON: %s' % exc)
    regions = adapters[source_format](data)
    document, notes = build_document(regions, slide_name, source_format, source_file,
                                     clip_negative, converter)
    report = annotation_format.validate(document)
    if not report.ok:
        raise AdapterError('converted document fails validation: %s'
                           % '; '.join(issue.message for issue in report.errors[:3]))
    return document, notes

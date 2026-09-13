"""
Validation for the DSA annotation format, version 1.

The format and every check here are specified in
devops/dsa/annotation_format/SPEC.md. This module deliberately has no Girder
imports, so the same rules run in two places: the upload route in ``rest.py``,
which knows the target item and its collection, and the offline tools under
``devops/dsa/utils``, which usually don't.

Validation runs in two layers:

1. The JSON Schema (``schemas/dsa-annotation-v1.schema.json``) checks structure,
   types and allowed values.
2. Ingest checks cover what a schema can't express: references between fields,
   geometry rules, and comparisons against the target slide and the
   collection's class vocabulary. They assume a structurally valid document, so
   they only run once layer 1 passes.
"""

import json
import os
from collections import defaultdict

FORMAT_NAME = 'dsa-annotation'
FORMAT_VERSION = '1.0'
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           'schemas', 'dsa-annotation-v1.schema.json')

ERROR = 'error'
WARNING = 'warning'

# Beyond this many issues with one code, the rest fold into a single summary
# line: a file with 800 mis-scaled regions should still produce a readable report.
MAX_ISSUES_PER_CODE = 20

# Passed as ``vocabulary`` to skip the vocabulary check entirely. Distinct from
# None, which means "the collection has no vocabulary" and produces W-NOVOCAB.
NOT_CHECKED = object()

# Order of the geometry branches in the schema's oneOf.
_GEOMETRY_BRANCH = {'Polygon': 0, 'MultiPolygon': 1, 'Point': 2}

# A slight overshoot is a region drawn past the image edge; anything beyond
# this factor is almost always coordinates in the wrong space.
_SCALE_HINT_FACTOR = 1.05

_validator = None


class Issue(object):
    __slots__ = ('code', 'severity', 'message')

    def __init__(self, code, severity, message):
        self.code = code
        self.severity = severity
        self.message = message

    def as_dict(self):
        return {'code': self.code, 'severity': self.severity, 'message': self.message}

    def __repr__(self):
        return '%s %s: %s' % (self.severity.upper(), self.code, self.message)


class Report(object):
    """Every problem found in one document, errors and warnings together."""

    def __init__(self):
        self.issues = []
        self._counts = defaultdict(int)
        self._overflow = defaultdict(int)
        self._overflow_severity = {}

    def add(self, code, severity, message):
        self._counts[code] += 1
        if self._counts[code] <= MAX_ISSUES_PER_CODE:
            self.issues.append(Issue(code, severity, message))
        else:
            self._overflow[code] += 1
            self._overflow_severity[code] = severity

    def _finish(self):
        for code, extra in self._overflow.items():
            self.issues.append(Issue(
                code, self._overflow_severity[code],
                '... and %d more %s issue%s' % (extra, code, '' if extra == 1 else 's')))
        self._overflow.clear()
        return self

    def count(self, code):
        """Total issues with this code, including ones folded into a summary."""
        return self._counts[code]

    @property
    def errors(self):
        return [issue for issue in self.issues if issue.severity == ERROR]

    @property
    def warnings(self):
        return [issue for issue in self.issues if issue.severity == WARNING]

    @property
    def codes(self):
        return {issue.code for issue in self.issues}

    @property
    def ok(self):
        return not self.errors

    def as_dict(self):
        return {
            'ok': self.ok,
            'errors': [issue.as_dict() for issue in self.errors],
            'warnings': [issue.as_dict() for issue in self.warnings],
        }


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def validate(doc, slide=None, vocabulary=NOT_CHECKED, collection_name=None):
    """Validate a parsed document and return a :class:`Report`.

    :param doc: the parsed JSON document.
    :param slide: the target item as ``{'name', 'sizeX', 'sizeY'}``, any of
        which may be missing. Enables W-SLIDE, E-SIZE and E-BOUNDS. Leave as
        None when the target isn't known, e.g. when converting offline.
    :param vocabulary: the collection's class vocabulary (a dict keyed by class
        name) to enable E-VOCAB; None when the collection has no vocabulary,
        which produces W-NOVOCAB; or NOT_CHECKED to skip both.
    :param collection_name: used only to make messages specific.
    """
    report = Report()
    for error in sorted(_schema_validator().iter_errors(doc), key=_error_sort_key):
        for message in _schema_messages(error):
            report.add('E-SCHEMA', ERROR, message)
    if report.errors:
        return report._finish()

    props = doc['properties']
    classes = props['classes']
    _check_slide(report, props['slide'], slide)
    _check_vocabulary(report, classes, vocabulary, collection_name)

    used = set()
    for index, feature in enumerate(doc['features']):
        _check_feature(report, index, feature, classes, used, slide)

    for name in classes:
        if name not in used:
            report.add('W-UNUSED', WARNING,
                       'class "%s" is declared but has no features' % name)
    return report._finish()


def validate_json_text(text, **kwargs):
    """Parse ``text`` and validate it. Returns ``(document or None, report)``."""
    try:
        doc = json.loads(text)
    except (TypeError, ValueError) as exc:
        report = Report()
        report.add('E-JSON', ERROR, 'not valid JSON: %s' % exc)
        return None, report._finish()
    return doc, validate(doc, **kwargs)


# ---------------------------------------------------------------------------
# Layer 1: schema
# ---------------------------------------------------------------------------

def _schema_validator():
    global _validator
    if _validator is None:
        from jsonschema import Draft202012Validator

        with open(SCHEMA_PATH, encoding='utf-8') as handle:
            _validator = Draft202012Validator(json.load(handle))
    return _validator


def _error_sort_key(error):
    return [('%09d' % part) if isinstance(part, int) else str(part)
            for part in error.absolute_path]


def _path(parts):
    text = ''
    for part in parts:
        if isinstance(part, int):
            text += '[%d]' % part
        else:
            text += ('.' if text else '') + str(part)
    return text or '(top level)'


def _schema_messages(error):
    """Yield readable messages for one jsonschema error.

    A malformed geometry fails the schema's oneOf, whose own message just says
    the geometry matched no branch. Report the errors from the branch the
    geometry claims to be instead, so a bad Polygon vertex is reported as such.
    """
    parts = list(error.absolute_path)
    if error.validator == 'oneOf' and parts and parts[-1] == 'geometry':
        geometry = error.instance
        if not isinstance(geometry, dict):
            yield '%s: must be a geometry object' % _path(parts)
            return
        branch = _GEOMETRY_BRANCH.get(geometry.get('type'))
        if branch is None:
            yield ('%s: geometry type %s is not allowed; use Polygon, MultiPolygon, '
                   'or Point' % (_path(parts), json.dumps(geometry.get('type'))))
            return
        matched = [sub for sub in error.context
                   if sub.relative_schema_path and sub.relative_schema_path[0] == branch]
        if matched:
            for sub in sorted(matched, key=_error_sort_key):
                for message in _schema_messages(sub):
                    yield message
            return
    yield '%s: %s' % (_path(parts), _short_message(error))


def _json_type(value):
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, (int, float)):
        return 'number'
    if isinstance(value, str):
        return 'string'
    if isinstance(value, list):
        return 'array'
    if isinstance(value, dict):
        return 'object'
    return 'null'


def _short_message(error):
    """jsonschema's messages embed the whole failing value, which for a
    coordinate array can be enormous. Say what's wrong without repeating it."""
    kind, expected, value = error.validator, error.validator_value, error.instance
    if kind == 'const':
        return 'must be %s' % json.dumps(expected)
    if kind == 'type':
        wanted = ' or '.join(expected) if isinstance(expected, list) else expected
        return 'must be of type %s, not %s' % (wanted, _json_type(value))
    if kind == 'minItems':
        return 'needs at least %d item%s, has %d' % (
            expected, '' if expected == 1 else 's', len(value))
    if kind == 'items' and expected is False:
        return 'has %d values; positions are exactly [x, y]' % len(value)
    if kind == 'minProperties':
        return 'must not be empty'
    if kind == 'minLength':
        return 'must not be empty' if expected == 1 else 'must be at least %d characters' % expected
    if kind == 'maxLength':
        return 'must be at most %d characters' % expected
    if kind == 'minimum':
        return 'must be %s or greater, got %s' % (expected, value)
    if kind == 'pattern':
        return '%s does not match %s' % (json.dumps(value)[:60], expected)
    message = error.message
    return message if len(message) <= 160 else message[:157] + '...'


# ---------------------------------------------------------------------------
# Layer 2: ingest checks
# ---------------------------------------------------------------------------

def _num(value):
    return '%d' % value if float(value).is_integer() else '%.1f' % value


def _pos(position):
    return '[%s, %s]' % (_num(position[0]), _num(position[1]))


def _polygons(geometry):
    """Yield ``(polygon index or None, rings)`` for each polygon in a geometry."""
    if geometry['type'] == 'Polygon':
        yield None, geometry['coordinates']
    elif geometry['type'] == 'MultiPolygon':
        for index, rings in enumerate(geometry['coordinates']):
            yield index, rings


def _positions(geometry):
    if geometry['type'] == 'Point':
        yield geometry['coordinates']
        return
    for _, rings in _polygons(geometry):
        for ring in rings:
            for position in ring:
                yield position


def _where(feature_index, polygon_index, ring_index):
    text = 'feature %d' % feature_index
    if polygon_index is not None:
        text += ', polygon %d' % polygon_index
    return '%s, ring %d' % (text, ring_index)


def _on_segment(px, py, x1, y1, x2, y2, eps=1e-6):
    if not (min(x1, x2) - eps <= px <= max(x1, x2) + eps
            and min(y1, y2) - eps <= py <= max(y1, y2) + eps):
        return False
    cross = (x2 - x1) * (py - y1) - (y2 - y1) * (px - x1)
    length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    return abs(cross) <= eps * max(length, 1.0)


def _inside_or_on(position, ring):
    """Ray casting, counting points on the boundary as inside: a hole that
    shares an edge with its outer ring is still contained."""
    x, y = position[0], position[1]
    inside = False
    previous = len(ring) - 1
    for current in range(len(ring)):
        xi, yi = ring[current][0], ring[current][1]
        xj, yj = ring[previous][0], ring[previous][1]
        if _on_segment(x, y, xi, yi, xj, yj):
            return True
        if (yi > y) != (yj > y):
            if x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
        previous = current
    return inside


def _check_slide(report, declared, target):
    if not target:
        return
    target_name = target.get('name')
    if target_name and declared['name'] != target_name:
        report.add('W-SLIDE', WARNING, 'file names "%s" but the target item is "%s"'
                   % (declared['name'], target_name))
    for key, adjective in (('sizeX', 'wide'), ('sizeY', 'tall')):
        if key in declared and target.get(key) and declared[key] != target[key]:
            report.add('E-SIZE', ERROR, 'slide.%s is %d but the item is %d pixels %s'
                       % (key, declared[key], target[key], adjective))


def _fold(name):
    return ' '.join(name.split()).casefold()


def _check_vocabulary(report, classes, vocabulary, collection_name):
    if vocabulary is NOT_CHECKED:
        return
    if not vocabulary:
        where = 'collection "%s"' % collection_name if collection_name else 'the collection'
        report.add('W-NOVOCAB', WARNING,
                   '%s has no class vocabulary; classes were not checked' % where)
        return
    folded = {}
    for name in vocabulary:
        folded.setdefault(_fold(name), name)
    label = collection_name or 'collection'
    for name in classes:
        if name in vocabulary:
            continue
        message = 'class "%s" is not in the %s vocabulary' % (name, label)
        suggestion = folded.get(_fold(name))
        if suggestion:
            message += ' (did you mean "%s"?)' % suggestion
        report.add('E-VOCAB', ERROR, message)


def _check_feature(report, index, feature, classes, used, slide):
    name = feature['properties']['class']
    used.add(name)
    if name not in classes:
        report.add('E-CLASS', ERROR, 'feature %d: class "%s" is not declared in '
                   'properties.classes' % (index, name))

    geometry = feature['geometry']
    for polygon_index, rings in _polygons(geometry):
        outer_usable = True
        for ring_index, ring in enumerate(rings):
            where = _where(index, polygon_index, ring_index)
            if ring[0] != ring[-1]:
                report.add('E-RING', ERROR, '%s: last vertex %s does not repeat first '
                           'vertex %s' % (where, _pos(ring[-1]), _pos(ring[0])))
            distinct = len({(position[0], position[1]) for position in ring})
            if distinct < 3:
                report.add('E-DEGEN', ERROR, '%s: only %d distinct vert%s'
                           % (where, distinct, 'ex' if distinct == 1 else 'ices'))
                if ring_index == 0:
                    outer_usable = False
            elif ring_index > 0 and outer_usable:
                outside = next((position for position in ring
                                if not _inside_or_on(position, rings[0])), None)
                if outside is not None:
                    report.add('E-HOLE', ERROR, '%s: hole extends outside the outer '
                               'boundary (vertex %s)' % (where, _pos(outside)))

    if slide:
        _check_bounds(report, index, geometry, slide)


def _check_bounds(report, index, geometry, slide):
    size_x, size_y = slide.get('sizeX'), slide.get('sizeY')
    if not size_x or not size_y:
        return
    max_x = max_y = 0
    for position in _positions(geometry):
        max_x = max(max_x, position[0])
        max_y = max(max_y, position[1])
    over = []
    if max_x > size_x:
        over.append(('x', max_x, 'width', size_x))
    if max_y > size_y:
        over.append(('y', max_y, 'height', size_y))
    if not over:
        return
    detail = ' and '.join('%s = %s exceeds image %s %s' % (axis, _num(value), dimension, _num(size))
                          for axis, value, dimension, size in over)
    factor = max(value / float(size) for _, value, _, size in over)
    if factor >= _SCALE_HINT_FACTOR:
        message = 'feature %d: %s (%.1f×). Coordinates are probably not level-0 pixels.' % (
            index, detail, factor)
    else:
        message = 'feature %d: %s; the region runs past the image edge.' % (index, detail)
    report.add('E-BOUNDS', ERROR, message)

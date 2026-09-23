import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))

from dsa_csv_plugin import annotation_format as af  # noqa: E402

EXAMPLE = os.path.join(HERE, os.pardir, 'dsa_csv_plugin', 'docs', 'example-beetle-patient104_wsi1.json')
SETUP_CFG = os.path.join(HERE, os.pardir, 'setup.cfg')

SQUARE = [[0, 0], [100, 0], [100, 100], [0, 100], [0, 0]]


def make_doc(features=None, classes=None, **slide):
    return {
        'type': 'FeatureCollection',
        'properties': {
            'format': 'dsa-annotation',
            'version': '1.0',
            'coordinate_space': 'level0_pixels',
            'slide': dict({'name': 'slide.tif'}, **slide),
            'classes': classes if classes is not None else {'tumor': {}},
        },
        'features': features if features is not None else [polygon([SQUARE])],
    }


def polygon(rings, cls='tumor'):
    return {'type': 'Feature', 'geometry': {'type': 'Polygon', 'coordinates': rings},
            'properties': {'class': cls}}


def point(x, y, cls='tumor'):
    return {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [x, y]},
            'properties': {'class': cls}}


def scaled(ring, factor):
    return [[x * factor, y * factor] for x, y in ring]


def messages(report, code):
    return [issue.message for issue in report.issues if issue.code == code]


# --- packaging -------------------------------------------------------------

def test_schema_ships_inside_the_plugin_package():
    assert os.path.isfile(af.SCHEMA_PATH)
    with open(SETUP_CFG) as handle:
        assert 'schemas/*.json' in handle.read()


def test_spec_example_is_valid():
    with open(EXAMPLE) as handle:
        report = af.validate(json.load(handle))
    assert report.ok
    assert report.issues == []


def test_minimal_document_is_valid():
    report = af.validate(make_doc())
    assert report.ok and report.issues == []


# --- layer 1: schema -------------------------------------------------------

def test_disallowed_geometry_type_is_named():
    doc = make_doc([{'type': 'Feature',
                     'geometry': {'type': 'LineString', 'coordinates': SQUARE},
                     'properties': {'class': 'tumor'}}])
    report = af.validate(doc)
    assert messages(report, 'E-SCHEMA') == [
        'features[0].geometry: geometry type "LineString" is not allowed; '
        'use Polygon, MultiPolygon, or Point']


def test_bad_vertex_reported_at_its_path_not_as_a_oneof_failure():
    ring = [[0, 0, 5]] + SQUARE[1:-1] + [[0, 0, 5]]
    report = af.validate(make_doc([polygon([ring])]))
    found = messages(report, 'E-SCHEMA')
    assert 'features[0].geometry.coordinates[0][0]: has 3 values; positions are exactly [x, y]' in found
    assert all('oneOf' not in message and 'is not valid under any' not in message for message in found)


def test_missing_class_is_reported_with_its_path():
    doc = make_doc()
    del doc['features'][0]['properties']['class']
    assert messages(af.validate(doc), 'E-SCHEMA') == [
        "features[0].properties: 'class' is a required property"]


def test_raw_beetle_file_is_rejected_without_crashing():
    raw = [{'index': 0, 'coordinates': SQUARE, 'label': {'name': 'other', 'value': 1}}]
    report = af.validate(raw)
    assert not report.ok
    assert report.codes == {'E-SCHEMA'}


def test_downsampled_coordinate_space_is_rejected():
    doc = make_doc()
    doc['properties']['coordinate_space'] = 'level2_pixels'
    assert messages(af.validate(doc), 'E-SCHEMA') == [
        'properties.coordinate_space: must be "level0_pixels"']


def test_ingest_checks_do_not_run_on_a_structurally_invalid_document():
    doc = make_doc([polygon([SQUARE], cls='undeclared')])
    doc['properties']['coordinate_space'] = 'microns'
    assert af.validate(doc).codes == {'E-SCHEMA'}


def test_invalid_json_text():
    doc, report = af.validate_json_text('{"type": ')
    assert doc is None
    assert report.codes == {'E-JSON'}


# --- layer 2: ingest checks --------------------------------------------------

def test_undeclared_class():
    report = af.validate(make_doc([polygon([SQUARE], cls='tumour')]))
    assert messages(report, 'E-CLASS') == [
        'feature 0: class "tumour" is not declared in properties.classes']


def test_unclosed_ring():
    ring = SQUARE[:-1] + [[0, 10]]
    report = af.validate(make_doc([polygon([ring])]))
    assert messages(report, 'E-RING') == [
        'feature 0, ring 0: last vertex [0, 10] does not repeat first vertex [0, 0]']


def test_degenerate_ring():
    ring = [[0, 0], [1, 1], [0, 0], [1, 1], [0, 0]]
    report = af.validate(make_doc([polygon([ring])]))
    assert messages(report, 'E-DEGEN') == ['feature 0, ring 0: only 2 distinct vertices']


def test_hole_inside_outer_ring_is_valid():
    hole = [[20, 20], [40, 20], [40, 40], [20, 20]]
    assert af.validate(make_doc([polygon([SQUARE, hole])])).ok


def test_hole_touching_the_outer_boundary_is_valid():
    hole = [[0, 10], [20, 10], [20, 20], [0, 10]]
    assert af.validate(make_doc([polygon([SQUARE, hole])])).ok


def test_hole_outside_outer_ring():
    hole = [[90, 90], [150, 90], [150, 150], [90, 90]]
    report = af.validate(make_doc([polygon([SQUARE, hole])]))
    assert messages(report, 'E-HOLE') == [
        'feature 0, ring 1: hole extends outside the outer boundary (vertex [150, 90])']


def test_multipolygon_messages_name_the_polygon():
    hole = [[90, 90], [150, 90], [150, 150], [90, 90]]
    feature = {'type': 'Feature',
               'geometry': {'type': 'MultiPolygon', 'coordinates': [[SQUARE], [SQUARE, hole]]},
               'properties': {'class': 'tumor'}}
    report = af.validate(make_doc([feature]))
    assert messages(report, 'E-HOLE')[0].startswith('feature 0, polygon 1, ring 1:')


def test_coordinates_in_the_wrong_space():
    report = af.validate(make_doc([polygon([scaled(SQUARE, 40)])]),
                         slide={'name': 'slide.tif', 'sizeX': 1000, 'sizeY': 1000})
    assert messages(report, 'E-BOUNDS') == [
        'feature 0: x = 4000 exceeds image width 1000 and y = 4000 exceeds image height 1000 '
        '(4.0×). Coordinates are probably not level-0 pixels.']


def test_region_slightly_past_the_edge_gets_a_different_hint():
    report = af.validate(make_doc([polygon([scaled(SQUARE, 10.1)])]),
                         slide={'sizeX': 1000, 'sizeY': 2000})
    assert messages(report, 'E-BOUNDS') == [
        'feature 0: x = 1010 exceeds image width 1000; the region runs past the image edge.']


def test_points_are_bounds_checked():
    report = af.validate(make_doc([point(5000, 10)]), slide={'sizeX': 1000, 'sizeY': 1000})
    assert report.count('E-BOUNDS') == 1


def test_bounds_are_not_checked_without_a_target_slide():
    assert af.validate(make_doc([polygon([scaled(SQUARE, 1000)])])).ok


def test_declared_size_must_match_the_item():
    doc = make_doc(sizeX=1000, sizeY=500)
    report = af.validate(doc, slide={'name': 'slide.tif', 'sizeX': 2000, 'sizeY': 500})
    assert messages(report, 'E-SIZE') == ['slide.sizeX is 1000 but the item is 2000 pixels wide']


def test_slide_name_mismatch_is_only_a_warning():
    report = af.validate(make_doc(), slide={'name': 'other.tif'})
    assert report.ok
    assert messages(report, 'W-SLIDE') == ['file names "slide.tif" but the target item is "other.tif"']


def test_class_outside_vocabulary_suggests_the_near_match():
    doc = make_doc([polygon([SQUARE], cls='Necrosis')], classes={'Necrosis': {}})
    report = af.validate(doc, vocabulary={'necrosis': {}, 'other': {}}, collection_name='BEETLE')
    assert messages(report, 'E-VOCAB') == [
        'class "Necrosis" is not in the BEETLE vocabulary (did you mean "necrosis"?)']


def test_collection_without_vocabulary_warns_but_accepts():
    report = af.validate(make_doc(), vocabulary=None, collection_name='BEETLE')
    assert report.ok
    assert messages(report, 'W-NOVOCAB') == [
        'collection "BEETLE" has no class vocabulary; classes were not checked']


def test_vocabulary_is_ignored_when_not_supplied():
    assert af.validate(make_doc()).issues == []


def test_unused_class_warns():
    doc = make_doc(classes={'tumor': {}, 'necrosis': {}})
    report = af.validate(doc)
    assert report.ok
    assert messages(report, 'W-UNUSED') == ['class "necrosis" is declared but has no features']


def test_repeated_issues_are_folded_into_a_summary():
    features = [polygon([scaled(SQUARE, 40)]) for _ in range(50)]
    report = af.validate(make_doc(features), slide={'sizeX': 1000, 'sizeY': 1000})
    found = messages(report, 'E-BOUNDS')
    assert report.count('E-BOUNDS') == 50
    assert len(found) == af.MAX_ISSUES_PER_CODE + 1
    assert found[-1] == '... and 30 more E-BOUNDS issues'


def test_report_as_dict_separates_errors_and_warnings():
    doc = make_doc([polygon([SQUARE], cls='tumour')], classes={'tumor': {}})
    result = af.validate(doc).as_dict()
    assert result['ok'] is False
    assert [issue['code'] for issue in result['errors']] == ['E-CLASS']
    assert [issue['code'] for issue in result['warnings']] == ['W-UNUSED']


# --- upload: conversion to large_image annotations ---------------------------

# large_image's colorSchema pattern, copied from girder_large_image_annotation.
LARGE_IMAGE_COLOR = re.compile(r'^(#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})|'
                               r'rgb\(\d+,\s*\d+,\s*\d+\)|'
                               r'rgba\(\d+,\s*\d+,\s*\d+,\s*(\d?\.|)\d+\))$')
REST_PY = os.path.join(HERE, os.pardir, 'dsa_csv_plugin', 'rest.py')


def elements_of(annotations):
    return [element for annotation in annotations for element in annotation['elements']]


def test_colors_follow_vocabulary_then_file_then_default_palette():
    classes = {'a': {'color': '#112233'}, 'b': {}, 'c': {'color': '#445566'}, 'd': {}}
    vocabulary = {'c': {'color': '#aabbcc'}, 'b': {}, 'd': {}, 'a': {}}
    assert af.resolve_colors(classes, vocabulary) == {
        'a': '#112233',   # file color; vocabulary sets none
        'b': '#FFFFFF',   # default palette, position 1 in vocabulary order
        'c': '#AABBCC',   # vocabulary beats the file
        'd': '#000000',   # default palette, position 2
    }


def test_default_color_is_the_same_on_every_slide_in_a_collection():
    vocabulary = {'x': {}, 'y': {}, 'z': {}}
    assert af.resolve_colors({'z': {}}, vocabulary)['z'] == '#000000'
    assert af.resolve_colors({'y': {}, 'z': {}}, vocabulary) == {'y': '#FFFFFF', 'z': '#000000'}


def test_malformed_vocabulary_color_falls_through_to_the_file():
    assert af.resolve_colors({'a': {'color': '#010203'}}, {'a': {'color': 'red'}}) == {'a': '#010203'}


def test_polygon_with_hole_becomes_a_polyline_with_holes():
    hole = [[20, 20], [40, 20], [40, 40], [20, 20]]
    annotations = af.to_large_image_annotations(make_doc([polygon([SQUARE, hole])]), 'slide')
    assert [a['name'] for a in annotations] == ['slide - tumor']
    element, = elements_of(annotations)
    assert element['type'] == 'polyline' and element['closed'] is True
    assert element['points'] == [[0.0, 0.0, 0.0], [100.0, 0.0, 0.0], [100.0, 100.0, 0.0], [0.0, 100.0, 0.0]]
    assert element['holes'] == [[[20.0, 20.0, 0.0], [40.0, 20.0, 0.0], [40.0, 40.0, 0.0]]]
    assert element['group'] == 'tumor'
    assert (element['lineColor'], element['fillColor']) == ('rgb(0,0,0)', 'rgba(0,0,0,0.25)')


def test_multipolygon_becomes_one_element_per_part():
    feature = {'type': 'Feature', 'geometry': {'type': 'MultiPolygon', 'coordinates': [[SQUARE], [SQUARE]]},
               'properties': {'class': 'tumor'}}
    elements = elements_of(af.to_large_image_annotations(make_doc([feature]), 'slide'))
    assert [e['type'] for e in elements] == ['polyline', 'polyline']
    assert all('holes' not in e for e in elements)


def test_cell_detection_becomes_a_point_element():
    feature = point(12.5, 40, cls='lymphocyte')
    feature['properties']['attributes'] = {'confidence': 0.93}
    doc = make_doc([feature], classes={'lymphocyte': {'color': '#9600C8'}})
    element, = elements_of(af.to_large_image_annotations(doc, 'slide'))
    assert element == {'type': 'point', 'center': [12.5, 40.0, 0.0], 'group': 'lymphocyte',
                       'lineColor': 'rgb(150,0,200)', 'lineWidth': 2,
                       'fillColor': 'rgba(150,0,200,0.25)', 'user': {'confidence': 0.93}}


def test_label_and_provenance_carry_over():
    feature = polygon([SQUARE])
    feature['properties']['label'] = 'region 0'
    doc = make_doc([feature])
    doc['properties']['provenance'] = {'source_format': 'beetle'}
    annotation, = af.to_large_image_annotations(doc, 'slide')
    assert annotation['elements'][0]['label'] == {'value': 'region 0'}
    assert annotation['attributes'] == {'provenance': {'source_format': 'beetle'}}
    assert annotation['description'] == 'Imported from dsa-annotation 1.0 (1 element)'


def test_layers_follow_vocabulary_order():
    doc = make_doc([polygon([SQUARE], 'tumor'), polygon([SQUARE], 'necrosis')],
                   classes={'tumor': {}, 'necrosis': {}})
    names = [a['name'] for a in af.to_large_image_annotations(doc, 's', {'necrosis': {}, 'tumor': {}})]
    assert names == ['s - necrosis', 's - tumor']


def test_every_generated_color_is_accepted_by_large_image():
    doc = make_doc([polygon([SQUARE], c) for c in 'abc'] + [point(1, 1, 'd')],
                   classes={'a': {}, 'b': {'color': '#0a0B0c'}, 'c': {}, 'd': {'color': '#FFFFFF'}})
    for element in elements_of(af.to_large_image_annotations(doc, 's')):
        assert LARGE_IMAGE_COLOR.match(element['lineColor']), element['lineColor']
        assert LARGE_IMAGE_COLOR.match(element['fillColor']), element['fillColor']


# --- upload: prepare_upload ------------------------------------------------

TARGET = {'name': 'patient1_wsi1.tif', 'sizeX': 1000, 'sizeY': 1000}


def test_valid_upload_names_layers_after_the_target_item():
    report, annotations = af.prepare_upload(json.dumps(make_doc()), slide=TARGET)
    assert report.ok
    assert [a['name'] for a in annotations] == ['patient1_wsi1 - tumor']
    assert messages(report, 'W-SLIDE') == ['file names "slide.tif" but the target item is "patient1_wsi1.tif"']


def test_invalid_upload_produces_no_annotations():
    report, annotations = af.prepare_upload(json.dumps(make_doc([polygon([scaled(SQUARE, 40)])])),
                                            slide=TARGET)
    assert {issue.code for issue in report.errors} == {'E-BOUNDS'}
    assert annotations == []


def test_class_outside_vocabulary_blocks_the_upload():
    report, annotations = af.prepare_upload(json.dumps(make_doc()), slide=TARGET,
                                            vocabulary={'necrosis': {}}, collection_name='BEETLE')
    assert 'E-VOCAB' in report.codes
    assert annotations == []


def test_unreadable_slide_size_warns_but_uploads():
    report, annotations = af.prepare_upload(json.dumps(make_doc()), slide={'name': 'slide.tif'})
    assert report.ok and len(annotations) == 1
    assert messages(report, 'W-NOTILES') == [
        'could not read the slide size; coordinates were not bounds-checked']


def test_bad_json_upload():
    report, annotations = af.prepare_upload('not json', slide=TARGET)
    assert report.codes == {'E-JSON'} and annotations == []


def test_beetle_example_uploads_with_beetle_vocabulary_colors():
    with open(EXAMPLE) as handle:
        text = handle.read()
    vocabulary = {'invasive epithelium': {'color': '#FF0000'},
                  'non-invasive epithelium': {'color': '#C87800'},
                  'necrosis': {'color': '#5A5A5A'}, 'other': {'color': '#9600C8'}}
    report, annotations = af.prepare_upload(
        text, slide={'name': 'patient104_wsi1.tif', 'sizeX': 100000, 'sizeY': 120000},
        vocabulary=vocabulary, collection_name='BEETLE')
    assert report.issues == []
    assert {a['name']: a['elements'][0]['lineColor'] for a in annotations} == {
        'patient104_wsi1 - non-invasive epithelium': 'rgb(200,120,0)',
        'patient104_wsi1 - other': 'rgb(150,0,200)',
    }


def test_upload_route_uses_the_strict_validator():
    with open(REST_PY) as handle:
        source = handle.read()
    compile(source, REST_PY, 'exec')
    assert 'annotation_format.prepare_upload(' in source
    assert 'def json_to_annotations' not in source


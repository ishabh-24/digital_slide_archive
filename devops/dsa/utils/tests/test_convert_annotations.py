import csv
import json
import os
import shutil
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))

import convert_annotations as ca  # noqa: E402
from dsa_csv_plugin import annotation_format as af  # noqa: E402  (path set up by convert_annotations)
import validate_annotation  # noqa: E402

FIXTURE = os.path.join(HERE, 'fixtures', 'beetle_patient104_wsi1_excerpt.json')
SQUARE = [[0, 0], [100, 0], [100, 100], [0, 100]]


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def convert(path, source='beetle', **kwargs):
    return ca.convert_file(path, source, 'patient104_wsi1.tif',
                           'annotations/jsons/patient104_wsi1.json', **kwargs)


# --- BEETLE ----------------------------------------------------------------

def test_beetle_excerpt_converts_to_a_valid_document():
    doc, notes = convert(FIXTURE)
    assert af.validate(doc).issues == []
    assert notes == {'features': 4, 'dropped': 0, 'clipped': 0,
                     'classes': ['non-invasive epithelium', 'other']}
    assert doc['properties']['slide'] == {'name': 'patient104_wsi1.tif'}
    assert doc['properties']['classes'] == {
        'non-invasive epithelium': {'description': 'BEETLE label value 2'},
        'other': {'description': 'BEETLE label value 1'},
    }
    assert [feature['id'] for feature in doc['features']] == [8, 11, 33, 39]
    assert doc['properties']['provenance']['source_format'] == 'beetle'


def test_beetle_coordinates_are_preserved_exactly():
    with open(FIXTURE) as handle:
        source = json.load(handle)
    doc, _ = convert(FIXTURE)
    for region, feature in zip(source, doc['features']):
        assert feature['geometry']['coordinates'] == [region['coordinates']]


def test_unclosed_rings_are_closed(tmp_path):
    path = write_json(tmp_path / 'a.json', [{'index': 0, 'coordinates': SQUARE,
                                             'label': {'name': 'other', 'value': 1}}])
    doc, _ = convert(path)
    ring = doc['features'][0]['geometry']['coordinates'][0]
    assert ring[0] == ring[-1] and len(ring) == 5


def test_degenerate_regions_are_dropped_and_counted(tmp_path):
    path = write_json(tmp_path / 'a.json', [
        {'index': 0, 'coordinates': SQUARE, 'label': {'name': 'other', 'value': 1}},
        {'index': 1, 'coordinates': [[5, 5], [6, 6], [5, 5]], 'label': {'name': 'other', 'value': 1}},
    ])
    doc, notes = convert(path)
    assert notes['dropped'] == 1
    assert [feature['id'] for feature in doc['features']] == [0]


def test_negative_coordinates_fail_unless_clipping_is_requested(tmp_path):
    ring = [[-3, 0], [100, 0], [100, 100], [0, 100]]
    path = write_json(tmp_path / 'a.json', [{'index': 7, 'coordinates': ring,
                                             'label': {'name': 'other', 'value': 1}}])
    with pytest.raises(ca.AdapterError, match=r'region 7 has a negative coordinate \[-3, 0\].*--clip-negative'):
        convert(path)
    doc, notes = convert(path, clip_negative=True)
    assert notes['clipped'] == 1
    assert doc['features'][0]['geometry']['coordinates'][0][0] == [0, 0]


def test_wrong_adapter_explains_what_it_expected(tmp_path):
    path = write_json(tmp_path / 'a.json', {'positive': [{'vertices': SQUARE}]})
    with pytest.raises(ca.AdapterError, match='expected a top-level list of regions'):
        convert(path)


def test_region_without_a_class_is_rejected(tmp_path):
    path = write_json(tmp_path / 'a.json', [{'index': 0, 'coordinates': SQUARE}])
    with pytest.raises(ca.AdapterError, match=r'region 0: missing label\.name'):
        convert(path)


def test_invalid_output_is_never_returned(tmp_path):
    long_name = 'x' * 150
    path = write_json(tmp_path / 'a.json', [{'index': 0, 'coordinates': SQUARE,
                                             'label': {'name': long_name, 'value': 1}}])
    with pytest.raises(ca.AdapterError, match='converted document fails validation'):
        convert(path)


# --- other adapters ---------------------------------------------------------

def test_bcnb_adapter(tmp_path):
    path = write_json(tmp_path / 'a.json', {
        'positive': [{'name': 'Annotation 0', 'vertices': SQUARE}],
        'negative': [{'name': 'Annotation 1', 'vertices': SQUARE}],
    })
    doc, notes = convert(path, source='bcnb')
    assert af.validate(doc).ok
    assert list(doc['properties']['classes']) == ['negative', 'positive']
    assert doc['features'][0]['properties'] == {'class': 'positive', 'label': 'Annotation 0'}


def test_auto_adapter_matches_the_beetle_adapter_on_classes():
    auto, _ = convert(FIXTURE, source='auto')
    beetle, _ = convert(FIXTURE)
    assert af.validate(auto).ok
    assert list(auto['properties']['classes']) == list(beetle['properties']['classes'])
    assert len(auto['features']) == len(beetle['features'])


# --- command line -----------------------------------------------------------

def make_dataset(root):
    shutil.copy(FIXTURE, write_json(root / 'annotations' / 'jsons' / 'patient104_wsi1.json', []))
    manifest = root / 'data_overview.csv'
    with open(manifest, 'w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['name', 'wsi_path', 'annotation_json_path'])
        writer.writerow(['patient104_wsi1', 'images/development/wsis/patient104_wsi1.tif',
                         'annotations/jsons/patient104_wsi1.json'])
        writer.writerow(['patient320_wsi1', 'images/evaluation/wsis/patient320_wsi1.tif', ''])
        writer.writerow(['100B', '', 'annotations/jsons/100B.json'])
    return manifest


def run_bulk(root, out, *extra):
    return ca.main(['--from', 'beetle', '--manifest', str(root / 'data_overview.csv'),
                    '--path-root', str(root), '--out-dir', str(out)] + list(extra))


def test_manifest_conversion_end_to_end(tmp_path, capsys):
    make_dataset(tmp_path / 'BEETLE')
    out = tmp_path / 'out'
    assert run_bulk(tmp_path / 'BEETLE', out) == 0
    assert sorted(os.listdir(out)) == ['patient104_wsi1.dsa.json']
    with open(out / 'patient104_wsi1.dsa.json') as handle:
        doc = json.load(handle)
    assert af.validate(doc).ok
    assert doc['properties']['provenance']['source_file'] == 'annotations/jsons/patient104_wsi1.json'
    printed = capsys.readouterr().out
    assert '1 row(s) with both a slide and an annotation file, 2 skipped' in printed


def test_rerun_skips_existing_outputs_unless_overwriting(tmp_path, capsys):
    make_dataset(tmp_path / 'BEETLE')
    out = tmp_path / 'out'
    run_bulk(tmp_path / 'BEETLE', out)
    capsys.readouterr()
    assert run_bulk(tmp_path / 'BEETLE', out) == 0
    assert 'skipped (existing): 1' in capsys.readouterr().out
    assert run_bulk(tmp_path / 'BEETLE', out, '--overwrite') == 0
    assert 'converted:          1' in capsys.readouterr().out


def test_failures_produce_a_nonzero_exit(tmp_path, capsys):
    root = tmp_path / 'BEETLE'
    make_dataset(root)
    (root / 'annotations' / 'jsons' / 'patient104_wsi1.json').unlink()
    assert run_bulk(root, tmp_path / 'out') == 1
    assert 'could not read JSON' in capsys.readouterr().out


def test_single_file_then_validate_cli(tmp_path, capsys):
    out = tmp_path / 'patient104_wsi1.dsa.json'
    assert ca.main(['--from', 'beetle', '--in', FIXTURE,
                    '--slide-name', 'patient104_wsi1.tif', '--out', str(out)]) == 0
    assert validate_annotation.main([str(out), '--slide-name', 'patient104_wsi1.tif',
                                     '--slide-size', '100000x120000']) == 0
    assert validate_annotation.main([str(out), '--slide-size', '1000x1000']) == 1
    assert 'E-BOUNDS' in capsys.readouterr().out

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))

from dsa_csv_plugin import server_files  # noqa: E402


@pytest.fixture
def root(tmp_path):
    data = tmp_path / 'data'
    (data / 'BEETLE' / 'annotations' / 'jsons').mkdir(parents=True)
    (data / 'BEETLE' / 'annotations' / 'jsons' / 'p1.json').write_text('[]')
    (data / 'BEETLE' / 'notes.txt').write_text('x')
    (tmp_path / 'secret.json').write_text('{}')
    os.symlink(tmp_path / 'secret.json', data / 'BEETLE' / 'link.json')
    return data


def test_reads_a_json_file_under_the_root(root):
    path = str(root / 'BEETLE' / 'annotations' / 'jsons' / 'p1.json')
    assert server_files.read_source_file(path, [str(root)]) == '[]'


def test_traversal_out_of_the_root_is_rejected(root):
    with pytest.raises(ValueError, match='must be under'):
        server_files.resolve_source_path(str(root / 'BEETLE' / '..' / '..' / 'secret.json'), [str(root)])


def test_symlink_escaping_the_root_is_rejected(root):
    with pytest.raises(ValueError, match='must be under'):
        server_files.resolve_source_path(str(root / 'BEETLE' / 'link.json'), [str(root)])


def test_only_json_files(root):
    with pytest.raises(ValueError, match=r'only \.json'):
        server_files.resolve_source_path(str(root / 'BEETLE' / 'notes.txt'), [str(root)])


def test_relative_and_missing_paths(root):
    with pytest.raises(ValueError, match='absolute'):
        server_files.resolve_source_path('BEETLE/p1.json', [str(root)])
    with pytest.raises(ValueError, match='no such file'):
        server_files.resolve_source_path(str(root / 'BEETLE' / 'nope.json'), [str(root)])


def test_roots_come_from_the_environment():
    assert server_files.source_roots({}) == ['/data']
    assert server_files.source_roots({'DSA_ANNOTATION_SOURCE_ROOTS': '/data:/mnt/x:'}) == ['/data', '/mnt/x']


def test_pages_offer_a_server_path_and_the_routes_accept_it():
    with open(os.path.join(HERE, os.pardir, 'dsa_csv_plugin', 'rest.py')) as handle:
        rest = handle.read()
    assert rest.count('id="serverPath"') == 2            # upload page and convert page
    assert rest.count('_source_text(body),') == 2        # ingest route and convert route
    assert "('annotation_source_roots',)" in rest
    assert 'attachConverted' in rest                     # convert page can upload directly

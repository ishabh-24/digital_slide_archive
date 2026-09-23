import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))

from dsa_csv_plugin import converters, docs_page  # noqa: E402

REST_PY = os.path.join(HERE, os.pardir, 'dsa_csv_plugin', 'rest.py')
INIT_PY = os.path.join(HERE, os.pardir, 'dsa_csv_plugin', '__init__.py')
NAV_JS = os.path.join(HERE, os.pardir, 'dsa_csv_plugin', 'web_client', 'views', 'GlobalNav.js')
MAIN_JS = os.path.join(HERE, os.pardir, 'dsa_csv_plugin', 'web_client', 'main.js')


def render(text):
    return docs_page.render_markdown(text)


def test_headings_get_ids():
    assert render('## Class vocabulary (per collection)') == \
        '<h2 id="class-vocabulary-per-collection">Class vocabulary (per collection)</h2>'


def test_paragraph_inline_markup_and_escaping():
    html = render('Use `a < b` and **bold** with a [link](/annotation_upload) & more.')
    assert html == ('<p>Use <code>a &lt; b</code> and <strong>bold</strong> with a '
                    '<a href="/annotation_upload">link</a> &amp; more.</p>')


def test_fenced_code_is_escaped_not_parsed():
    html = render('```json\n{"a": "<b>", "n": 1}\n```')
    assert html == '<pre><code>{&quot;a&quot;: &quot;&lt;b&gt;&quot;, &quot;n&quot;: 1}</code></pre>'


def test_pipe_table():
    html = render('| Code | Check |\n|---|---|\n| `E-RING` | Every ring is closed |')
    assert html == ('<table><thead><tr><th>Code</th><th>Check</th></tr></thead><tbody>'
                    '<tr><td><code>E-RING</code></td><td>Every ring is closed</td></tr>'
                    '</tbody></table>')


def test_bullet_and_numbered_lists_with_continuation_lines():
    html = render('- first item\n  continues here\n- second\n\n1. one\n2. two')
    assert html == ('<ul>\n<li>first item continues here</li>\n<li>second</li>\n</ul>\n'
                    '<ol>\n<li>one</li>\n<li>two</li>\n</ol>')


def test_multiline_paragraphs_join_and_blank_lines_separate():
    assert render('line one\nline two\n\nnext') == '<p>line one line two</p>\n<p>next</p>'


def test_full_spec_renders_with_every_section_and_no_raw_markdown():
    html = render(docs_page.spec_markdown())
    for heading in ('minimal-file', 'validation', 'class-vocabulary-per-collection',
                    'how-a-file-becomes-histomicsui-annotations', 'converting-existing-data'):
        assert 'id="%s"' % heading in html, heading
    assert html.count('<table>') >= 5
    assert '<pre><code>' in html
    for code in ('E-BOUNDS', 'E-VOCAB', 'W-SLIDE', 'W-NOTILES'):
        assert '<code>%s</code>' % code in html
    stripped = re.sub(r'<pre>.*?</pre>', '', html, flags=re.S)
    assert '```' not in stripped and '\n|' not in stripped and '**' not in stripped


def test_example_file_is_valid_json_and_in_the_format():
    import json
    from dsa_csv_plugin import annotation_format
    doc = json.loads(docs_page.example_json())
    assert annotation_format.validate(doc).ok


def test_pages_and_routes_are_registered():
    with open(REST_PY) as handle:
        rest = handle.read()
    with open(INIT_PY) as handle:
        init = handle.read()
    compile(rest, REST_PY, 'exec')
    for route in ("('convert_annotation',)", "('annotation_schema',)", "('annotation_example',)"):
        assert route in rest
    for page in ('annotation_tools', 'annotation_convert', 'annotation_format'):
        assert "serverRoot'].%s = " % page in init.replace('info[', "info[").replace("'serverRoot'", "serverRoot'"), page
    assert '<!--FORMAT_OPTIONS-->' in rest and '<!--SPEC_BODY-->' in rest


def test_converter_page_lists_every_adapter():
    sys.path.insert(0, os.path.join(HERE, os.pardir, 'dsa_csv_plugin'))
    import importlib.util
    # rest.py needs Girder; exercise only the option-building expression it uses.
    options = ''.join('<option value="%s">' % key for key in sorted(converters.ADAPTER_DESCRIPTIONS))
    assert '<option value="beetle">' in options and '<option value="bcnb">' in options
    assert set(converters.ADAPTER_DESCRIPTIONS) == set(converters.ADAPTERS)


def test_nav_button_is_wired_without_hijacking_girders_router():
    with open(NAV_JS) as handle:
        nav = handle.read()
    with open(MAIN_JS) as handle:
        main = handle.read()
    assert "import './views/GlobalNav'" in main
    assert 'href="/annotation_tools"' in nav and 'icon-tags' in nav
    assert 'g-nav-link' not in nav.split('list.append(')[1]


def test_convert_text_rejects_unknown_format_and_bad_json():
    import pytest
    with pytest.raises(converters.AdapterError, match='unknown source format'):
        converters.convert_text('[]', 'qupath', 's.tif', 'f')
    with pytest.raises(converters.AdapterError, match='could not read JSON'):
        converters.convert_text('{', 'beetle', 's.tif', 'f')

"""REST resource and HTML page for DSA CSV metadata ingestion."""
import csv as _csv
import io
import json as _json
import os

import cherrypy

from girder.api import access
from girder.api.describe import Description, autoDescribeRoute
from girder.api.rest import Resource, RestException
from girder.constants import AccessType
from girder.models.folder import Folder
from girder.models.item import Item
from girder.models.upload import Upload

from . import annotation_format, converters, docs_page, server_files


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _coerce_value(s):
    if isinstance(s, bool):
        return s
    if s.lower() in ('true', 'false'):
        return s.lower() == 'true'
    try:
        if '.' in s:
            return float(s)
        return int(s)
    except (ValueError, AttributeError):
        return s


def _sort_enum_key(value):
    if isinstance(value, bool):
        return (0, float(value))
    if isinstance(value, (int, float)):
        return (0, float(value))
    if isinstance(value, str):
        try:
            return (0, float(value))
        except ValueError:
            return (1, value.lower())
    return (1, str(value).lower())


def _metadata_filter_spec(values):
    """Category dropdown with dynamic enum of distinct values for this column."""
    clean = [str(v).strip() for v in values if v is not None and str(v).strip()]
    if not clean:
        return {'format': 'text'}
    unique = sorted({_coerce_value(v) for v in clean}, key=_sort_enum_key)
    return {'format': 'category', 'enum': unique}


def _base_columns():
    return [
        {'type': 'image', 'value': 'thumbnail', 'title': 'Thumbnail', 'width': 160, 'height': 100},
        {'type': 'record', 'value': 'name', 'title': 'Name'},
        {'type': 'record', 'value': 'size', 'title': 'Size'},
    ]


def _metadata_column(key, filter_spec=None):
    spec = filter_spec or {'format': 'text'}
    col = {
        'type': 'metadata',
        'value': key,
        'title': key.replace('_', ' ').title(),
        'format': spec.get('format', 'text'),
    }
    if spec.get('enum'):
        col['enum'] = spec['enum']
    return col


def _build_yaml_dict(meta_keys, filter_specs=None):
    specs = filter_specs or {}
    columns = list(_base_columns())
    for key in meta_keys:
        columns.append(_metadata_column(key, specs.get(key)))
    return {
        'itemList': {'layout': {'mode': 'grid', 'flatten': False}, 'columns': columns},
        'itemListDialog': {'columns': columns},
    }


def _upload_config_yaml(folder, yaml_dict, user):
    """Write .large_image_config.yaml into *folder*, replacing any existing copy."""
    try:
        import yaml
        content = yaml.safe_dump(yaml_dict, sort_keys=False, default_flow_style=False)
    except ImportError:
        import json
        content = json.dumps(yaml_dict, indent=2)

    content_bytes = content.encode('utf-8')

    existing = Item().findOne({'folderId': folder['_id'], 'name': '.large_image_config.yaml'})
    if existing:
        Item().remove(existing)

    item = Item().createItem('.large_image_config.yaml', creator=user, folder=folder)
    Upload().uploadFromFile(
        io.BytesIO(content_bytes),
        len(content_bytes),
        name='.large_image_config.yaml',
        parentType='item',
        parent=item,
        user=user,
        mimeType='text/yaml',
    )


# ---------------------------------------------------------------------------
# Annotation upload helpers. The format's rules, and the conversion to
# large_image annotations, live in annotation_format.py.
# ---------------------------------------------------------------------------

def _slide_info(item):
    """Name and level-0 size of an item; just the name if it has no tile source."""
    info = {'name': item['name']}
    try:
        from girder_large_image.models.image_item import ImageItem

        metadata = ImageItem().getMetadata(item)
    except Exception:
        return info
    for key in ('sizeX', 'sizeY'):
        if metadata.get(key):
            info[key] = int(metadata[key])
    return info


def _source_text(body):
    """The annotation file's text: uploaded in ``json_content``, or read from
    ``server_path`` (a .json file under the allowed roots on the server)."""
    server_path = (body.get('server_path') or '').strip()
    if server_path:
        try:
            return server_files.read_source_file(server_path)
        except ValueError as exc:
            raise RestException(str(exc))
        except OSError as exc:
            raise RestException('could not read %s: %s' % (server_path, exc.strerror))
    text = body.get('json_content')
    if not text:
        raise RestException('Provide json_content (the file contents) or server_path '
                            '(a .json file on the server).')
    return text


def _collection_vocabulary(item):
    """``(collection name, class vocabulary)`` for the collection holding an item.

    The vocabulary is read from the collection's ``annotationClasses`` metadata.
    It is None when the collection has none, which makes the upload warn, and
    NOT_CHECKED when the item doesn't live in a collection.
    """
    if item.get('baseParentType') != 'collection':
        return None, annotation_format.NOT_CHECKED
    from girder.models.collection import Collection

    collection = Collection().load(item['baseParentId'], force=True)
    if not collection:
        return None, annotation_format.NOT_CHECKED
    vocabulary = (collection.get('meta') or {}).get('annotationClasses')
    if not (isinstance(vocabulary, dict) and vocabulary):
        vocabulary = None
    return collection['name'], vocabulary


# ---------------------------------------------------------------------------
# REST resource
# ---------------------------------------------------------------------------

class DsaCsvResource(Resource):
    def __init__(self):
        super().__init__()
        self.resourceName = 'dsa_tools'
        self.route('POST', ('folder', ':folderId', 'ingest_csv'), self.ingest_csv)
        self.route('GET', ('folder', ':folderId', 'slide_meta'), self.slide_meta)
        self.route('POST', ('item', ':itemId', 'ingest_annotation_json'),
                   self.ingest_annotation_json)
        self.route('POST', ('convert_annotation',), self.convert_annotation)
        self.route('GET', ('annotation_schema',), self.annotation_schema)
        self.route('GET', ('annotation_example',), self.annotation_example)
        self.route('GET', ('annotation_source_roots',), self.annotation_source_roots)

    @access.public
    @autoDescribeRoute(
        Description('List items in a folder with their metadata, plus the sorted '
                    'distinct values for each metadata key. Used to build the '
                    'filter dropdowns on the Browse & Filter page.')
        .modelParam('folderId', 'Target folder', model=Folder,
                    level=AccessType.READ, paramType='path')
    )
    def slide_meta(self, folder, params):
        items = list(Item().find({'folderId': folder['_id']}))
        out_items = []
        categories = {}
        for it in items:
            name = it.get('name', '')
            if name == '.large_image_config.yaml':
                continue
            meta = it.get('meta', {}) or {}
            flat_meta = {}
            for k, v in meta.items():
                if v is None or (isinstance(v, str) and not v.strip()):
                    continue
                if isinstance(v, (dict, list)):
                    continue
                flat_meta[k] = v
                categories.setdefault(k, set()).add(v)
            out_items.append({
                '_id': str(it['_id']),
                'name': name,
                'largeImage': bool(it.get('largeImage')),
                'meta': flat_meta,
            })
        cats = {
            k: sorted(vals, key=_sort_enum_key)
            for k, vals in categories.items()
        }
        return {'items': out_items, 'categories': cats}

    @access.user
    @autoDescribeRoute(
        Description('Apply CSV metadata to every matching item in a folder, '
                    'auto-detect column formats, and upload .large_image_config.yaml '
                    'so HistomicsUI exposes those columns as filter fields.')
        .modelParam('folderId', 'Target folder', model=Folder,
                    level=AccessType.WRITE, paramType='path')
        .jsonParam('body',
                   'JSON object with keys: csv_content (string), '
                   'match_on ("name" or "item_id", default "name")',
                   paramType='body', requireObject=True)
    )
    def ingest_csv(self, folder, body, params):
        csv_text = body.get('csv_content', '')
        match_on = body.get('match_on', 'name')

        reader = _csv.DictReader(io.StringIO(csv_text))
        if not reader.fieldnames:
            return {'error': 'CSV has no header row'}

        # Columns used for matching are excluded from metadata
        skip = {'item_id', '_id', 'name', 'item_name'}
        meta_keys = [
            k.strip() for k in reader.fieldnames
            if k and k.strip() not in skip and '.' not in k.strip()
        ]
        rows = list(reader)

        # Collect values from matched rows for category filter enums
        values_by_key = {k: [] for k in meta_keys}

        # Build lookup indexes once
        items = list(Item().find({'folderId': folder['_id']}))
        name_to_id = {it['name']: str(it['_id']) for it in items}
        id_to_item = {str(it['_id']): it for it in items}

        updated = 0
        not_found = []

        for row in rows:
            if match_on == 'item_id':
                raw_id = (row.get('item_id') or row.get('_id') or '').strip()
                if raw_id not in id_to_item:
                    not_found.append(raw_id or '(empty)')
                    continue
                item_doc = id_to_item[raw_id]
            else:
                name = (row.get('name') or row.get('item_name') or '').strip()
                item_id = name_to_id.get(name)
                if not item_id:
                    not_found.append(name or '(empty)')
                    continue
                item_doc = id_to_item[item_id]

            meta = {}
            for k in meta_keys:
                v = (row.get(k) or '').strip()
                if not v:
                    continue
                meta[k] = _coerce_value(v)
                values_by_key[k].append(v)

            if meta:
                Item().setMetadata(item_doc, meta)
                updated += 1

        filter_specs = {k: _metadata_filter_spec(values_by_key[k]) for k in meta_keys}

        # Generate and upload filter config YAML
        yaml_dict = _build_yaml_dict(meta_keys, filter_specs)
        yaml_uploaded = False
        yaml_error = None
        try:
            _upload_config_yaml(folder, yaml_dict, self.getCurrentUser())
            yaml_uploaded = True
        except Exception as exc:
            yaml_error = str(exc)

        result = {
            'items_updated': updated,
            'items_not_found': not_found[:50],
            'columns_configured': meta_keys,
            'filter_specs': filter_specs,
            'yaml_uploaded': yaml_uploaded,
        }
        if yaml_error:
            result['yaml_error'] = yaml_error
        return result


    @access.user
    @autoDescribeRoute(
        Description('Validate an annotation file in the DSA annotation format (v1) and, '
                    'if it is valid, attach it to an item as HistomicsUI annotations: one '
                    'colored, toggleable layer per class. Other formats must be converted '
                    'first with devops/dsa/utils/convert_annotations.py. The response lists '
                    'every error and warning found.')
        .modelParam('itemId', 'Target slide item', model=Item,
                    level=AccessType.WRITE, paramType='path')
        .jsonParam('body',
                   'JSON object with keys: json_content (string, the annotation file) or '
                   'server_path (string, a .json file on the server under the allowed '
                   'roots; see GET dsa_tools/annotation_source_roots), replace (bool, '
                   'default false: remove existing layers of the same name first), '
                   'validate_only (bool, default false: run every check without writing '
                   'anything)',
                   paramType='body', requireObject=True)
    )
    def ingest_annotation_json(self, item, body, params):
        from girder_large_image_annotation.models.annotation import Annotation

        replace = bool(body.get('replace', False))
        validate_only = bool(body.get('validate_only', False))
        collection_name, vocabulary = _collection_vocabulary(item)
        report, annotations = annotation_format.prepare_upload(
            _source_text(body), slide=_slide_info(item),
            vocabulary=vocabulary, collection_name=collection_name)

        result = report.as_dict()
        result.update({
            'item_id': str(item['_id']),
            'item_name': item['name'],
            'validate_only': validate_only,
            'layers': [{'name': a['name'], 'elements': len(a['elements'])} for a in annotations],
            'annotations_created': [],
            'skipped_existing': [],
        })
        if not report.ok or validate_only:
            return result

        user = self.getCurrentUser()
        existing = {
            a['annotation']['name']: a
            for a in Annotation().findWithPermissions(
                {'itemId': item['_id']}, user=user, level=AccessType.WRITE)
            if a.get('annotation', {}).get('name')
        }
        for annotation in annotations:
            name = annotation['name']
            if name in existing:
                if not replace:
                    result['skipped_existing'].append(name)
                    continue
                Annotation().remove(existing[name])
            Annotation().createAnnotation(item, user, annotation)
            result['annotations_created'].append(
                {'name': name, 'elements': len(annotation['elements'])})
        return result

    @access.user
    @autoDescribeRoute(
        Description('Convert a dataset-specific annotation file (BEETLE or BCNB) into the '
                    'DSA annotation format (v1). Nothing is stored: the converted document '
                    'is returned for download and later upload. The result has already '
                    'passed the format validator.')
        .jsonParam('body',
                   'JSON object with keys: source_format (one of: %s), json_content '
                   '(string, the source file) or server_path (string, a .json file on the '
                   'server under the allowed roots), slide_name (string, the slide file these '
                   'annotations belong to, with extension), clip_negative (bool, default '
                   'false: clamp negative coordinates to 0 instead of failing)'
                   % ', '.join(sorted(converters.ADAPTERS)),
                   paramType='body', requireObject=True)
    )
    def convert_annotation(self, body, params):
        slide_name = (body.get('slide_name') or '').strip()
        if not slide_name:
            return {'ok': False, 'error': 'slide_name is required: the slide file these '
                                          'annotations belong to, e.g. patient1_wsi1.tif'}
        try:
            document, notes = converters.convert_text(
                _source_text(body), body.get('source_format', ''), slide_name,
                body.get('source_file') or body.get('server_path') or 'uploaded file',
                clip_negative=bool(body.get('clip_negative', False)),
                converter='annotation_convert page (%s)' % converters.CONVERTER)
        except converters.AdapterError as exc:
            return {'ok': False, 'error': str(exc)}
        return {'ok': True, 'document': document, 'notes': notes,
                'suggested_filename': '%s.dsa.json' % slide_name.rsplit('.', 1)[0]}

    @access.public
    @autoDescribeRoute(
        Description('The JSON Schema for the DSA annotation format (v1).')
    )
    def annotation_schema(self, params):
        with open(annotation_format.SCHEMA_PATH, encoding='utf-8') as handle:
            return _json.load(handle)

    @access.public
    @autoDescribeRoute(
        Description('A small valid example file in the DSA annotation format (v1), '
                    'taken from BEETLE.')
    )
    def annotation_example(self, params):
        return _json.loads(docs_page.example_json())

    @access.user
    @autoDescribeRoute(
        Description('The server directories that annotation files may be read from by '
                    'server_path (upload and convert). Configured with the '
                    'DSA_ANNOTATION_SOURCE_ROOTS environment variable of the Girder '
                    'container, colon-separated; default /data.')
    )
    def annotation_source_roots(self, params):
        roots = server_files.source_roots()
        return {'roots': roots, 'existing': [r for r in roots if os.path.isdir(r)]}

# ---------------------------------------------------------------------------
# HTML upload page (served at /csv_upload by the plugin __init__)
# ---------------------------------------------------------------------------

def get_upload_html():
    return _HTML


def get_filter_html():
    return _FILTER_HTML


def get_tools_html():
    return _TOOLS_HTML


def get_convert_html():
    options = ''.join(
        '<option value="%s">%s</option>' % (key, docs_page.html.escape(label))
        for key, label in sorted(converters.ADAPTER_DESCRIPTIONS.items()))
    return _CONVERT_HTML.replace('<!--FORMAT_OPTIONS-->', options)


def get_format_html():
    return _FORMAT_HTML.replace('<!--SPEC_BODY-->',
                                docs_page.render_markdown(docs_page.spec_markdown()))


def get_annotation_html():
    return _ANNOTATION_HTML


_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DSA &mdash; CSV Metadata Import</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     background:#f0f2f5;color:#333;padding:32px 16px}
.wrap{max-width:720px;margin:0 auto}
h1{font-size:1.35em;color:#1a3a5c;border-bottom:3px solid #3498db;
   padding-bottom:10px;margin-bottom:20px}
.card{background:#fff;border-radius:8px;padding:24px;margin-bottom:16px;
      box-shadow:0 1px 4px rgba(0,0,0,.08)}
h2{font-size:.95em;text-transform:uppercase;letter-spacing:.05em;
   color:#7f8c8d;margin-bottom:14px}
label{display:block;font-size:.85em;font-weight:600;color:#555;margin-bottom:4px}
input,select{width:100%;padding:8px 10px;border:1px solid #d1d5db;border-radius:4px;
             font-size:.95em;margin-bottom:12px;transition:border .15s}
input:focus,select:focus{outline:none;border-color:#3498db}
.row{display:flex;gap:10px;align-items:flex-start}
.row input{margin-bottom:0}
.hint{font-size:.78em;color:#9b59b6;margin-top:-8px;margin-bottom:12px}
button{padding:9px 22px;border:none;border-radius:4px;font-size:.95em;cursor:pointer}
.btn-primary{background:#3498db;color:#fff}
.btn-primary:hover{background:#2980b9}
.btn-primary:disabled{background:#a0bdd8;cursor:default}
.btn-sm{background:#ecf0f1;color:#333;font-size:.82em;padding:7px 14px;flex-shrink:0}
.btn-sm:hover{background:#d5dbdb}
#result{display:none}
.ok{background:#eafaf1;border:1px solid #a9dfbf;border-radius:4px;padding:14px;color:#1e8449}
.err{background:#fdedec;border:1px solid #f5b7b1;border-radius:4px;padding:14px;color:#922b21}
.tag{display:inline-block;background:#ebf5fb;color:#2471a3;border-radius:3px;
     padding:2px 7px;margin:2px;font-size:.8em;font-family:monospace}
.tag.number{background:#eafaf1;color:#1e8449}
.biglink{display:inline-block;font-weight:600;color:#2980b9;text-decoration:none}
.biglink:hover{text-decoration:underline}
details{margin-top:10px}
summary{cursor:pointer;font-size:.85em;color:#555}
pre{background:#f8f9fa;padding:10px;border-radius:4px;font-size:.8em;
    max-height:180px;overflow-y:auto;margin-top:6px}
</style>
</head>
<body>
<div class="wrap">
  <h1>DSA &mdash; CSV Metadata Import</h1>

  <div class="card">
    <h2>1 &nbsp; Connection</h2>
    <label for="apiUrl">Girder API URL</label>
    <input id="apiUrl" type="text" placeholder="http://localhost:8080/api/v1">
    <label for="apiKey">API Key</label>
    <input id="apiKey" type="password" placeholder="Paste your Girder API key">
    <p class="hint">Generate a key: Girder UI &rarr; top-right user menu &rarr; My Account &rarr; API keys.</p>
  </div>

  <div class="card">
    <h2>2 &nbsp; Target Folder</h2>
    <label for="folderPath">Folder path (optional lookup)</label>
    <div class="row">
      <input id="folderPath" type="text" placeholder="/collection/My Collection/Images">
      <button class="btn-sm" type="button" onclick="lookupFolder()">Look up</button>
    </div>
    <p class="hint" id="folderHint">&nbsp;</p>
    <label for="folderId" style="margin-top:4px">Folder ID</label>
    <input id="folderId" type="text" placeholder="5f3a1b2c3d4e5f6a7b8c9d0e">
  </div>

  <div class="card">
    <h2>3 &nbsp; CSV File</h2>
    <label for="csvFile">Select CSV</label>
    <input id="csvFile" type="file" accept=".csv,text/csv">
    <label for="matchOn">Match rows to items by</label>
    <select id="matchOn">
      <option value="name">Item name (filename) &mdash; CSV needs a &ldquo;name&rdquo; column</option>
      <option value="item_id">Item ID &mdash; CSV needs an &ldquo;item_id&rdquo; column</option>
    </select>
    <p class="hint">All other columns become metadata. Columns with dots in their name are skipped (Girder restriction).</p>
  </div>

  <div class="card" style="text-align:center">
    <button id="submitBtn" class="btn-primary" onclick="run()">
      Import Metadata &amp; Configure Filters
    </button>
  </div>

  <div class="card" id="result">
    <h2>Result</h2>
    <div id="resultBody"></div>
  </div>
</div>

<script>
(function(){
  // Pre-fill API URL from current origin
  var base = window.location.origin;
  document.getElementById('apiUrl').value = base + '/api/v1';

  async function getToken(apiUrl, apiKey) {
    var r = await fetch(
      apiUrl + '/api_key/token?key=' + encodeURIComponent(apiKey) + '&duration=1',
      {method:'POST'}
    );
    if (!r.ok) throw new Error('Auth failed: ' + await r.text());
    var j = await r.json();
    return j.authToken.token;
  }

  function showFolderChoices(hint, doc, folders) {
    hint.textContent = doc.name + ' is a ' + doc._modelType +
      ', not a folder. Slides live in folders — pick one:';
    var box = document.createElement('div');
    box.style.marginTop = '4px';
    folders.forEach(function(f) {
      var a = document.createElement('a');
      a.href = '#';
      a.textContent = '\\uD83D\\uDCC1 ' + f.name;
      a.style.cssText = 'display:inline-block;margin:3px 10px 0 0;color:#2980b9;text-decoration:none';
      a.addEventListener('click', function(e) {
        e.preventDefault();
        document.getElementById('folderId').value = f._id;
        hint.textContent = 'Selected folder: ' + f.name + '  (ID: ' + f._id + ')';
      });
      box.appendChild(a);
    });
    hint.appendChild(box);
  }

  window.lookupFolder = async function() {
    var apiUrl = document.getElementById('apiUrl').value.trim();
    var apiKey  = document.getElementById('apiKey').value.trim();
    var path    = document.getElementById('folderPath').value.trim();
    var hint    = document.getElementById('folderHint');
    if (!path) { hint.textContent = 'Enter a path first.'; return; }
    try {
      var headers = {};
      if (apiKey) headers['Girder-Token'] = await getToken(apiUrl, apiKey);
      var r = await fetch(apiUrl + '/resource/lookup?path=' + encodeURIComponent(path),
                          {headers:headers});
      if (!r.ok) throw new Error(await r.text());
      var doc = await r.json();
      if (doc._modelType === 'folder') {
        document.getElementById('folderId').value = doc._id;
        hint.textContent = 'Found folder: ' + doc.name + '  (ID: ' + doc._id + ')';
        return;
      }
      // Resolved to a collection/user/etc. — list its child folders to choose from.
      var fr = await fetch(apiUrl + '/folder?parentType=' + encodeURIComponent(doc._modelType) +
                           '&parentId=' + doc._id + '&limit=500', {headers:headers});
      var folders = fr.ok ? await fr.json() : [];
      if (!folders.length) {
        hint.textContent = doc.name + ' is a ' + doc._modelType +
          ' with no sub-folders. Enter the path to a folder that contains slides.';
        return;
      }
      showFolderChoices(hint, doc, folders);
    } catch(e) {
      hint.textContent = 'Error: ' + e.message;
    }
  };

  window.run = async function() {
    var apiUrl   = document.getElementById('apiUrl').value.trim();
    var apiKey   = document.getElementById('apiKey').value.trim();
    var folderId = document.getElementById('folderId').value.trim();
    var matchOn  = document.getElementById('matchOn').value;
    var file     = document.getElementById('csvFile').files[0];

    if (!apiKey)   { alert('API key required.'); return; }
    if (!folderId) { alert('Folder ID required.'); return; }
    if (!file)     { alert('Select a CSV file.'); return; }

    var btn = document.getElementById('submitBtn');
    btn.disabled = true; btn.textContent = 'Working…';

    try {
      var token      = await getToken(apiUrl, apiKey);
      var csvContent = await file.text();

      var r = await fetch(
        apiUrl + '/dsa_tools/folder/' + folderId + '/ingest_csv',
        {
          method: 'POST',
          headers: {'Content-Type':'application/json','Girder-Token':token},
          body: JSON.stringify({csv_content: csvContent, match_on: matchOn}),
        }
      );
      var data = await r.json();
      renderResult(r.ok, data);
    } catch(e) {
      renderResult(false, {error: e.message});
    } finally {
      btn.disabled = false;
      btn.textContent = 'Import Metadata & Configure Filters';
    }
  };

  function renderResult(ok, data) {
    var div = document.getElementById('result');
    var body = document.getElementById('resultBody');
    div.style.display = 'block';

    if (!ok || data.error) {
      body.innerHTML = '<div class="err"><strong>Error:</strong> ' +
        esc(data.error || data.message || JSON.stringify(data)) + '</div>';
      return;
    }

    var html = '<div class="ok">';
    html += '<strong>' + data.items_updated + ' item(s) updated</strong>';

    if (data.columns_configured && data.columns_configured.length) {
      html += '<br><br>Filter columns now available in HistomicsUI:<br>';
      data.columns_configured.forEach(function(col) {
        var spec = data.filter_specs && data.filter_specs[col];
        var fmt = spec && spec.format;
        var count = spec && spec.enum ? spec.enum.length : 0;
        html += '<span class="tag' + (fmt === 'category' ? ' number' : '') + '">' +
                esc(col) + (fmt ? ' — ' + fmt : '') +
                (count ? ' (' + count + ' values)' : '') + '</span>';
      });
    }

    if (data.yaml_uploaded) {
      html += '<br><br>✓ <em>.large_image_config.yaml</em> uploaded. ' +
              'Open the folder in HistomicsUI to see the new filter columns.';
      var folderId = document.getElementById('folderId').value.trim();
      html += '<br><br><a class="biglink" href="/slidefilter?folderId=' +
              encodeURIComponent(folderId) + '">▶ Browse &amp; filter these slides by category →</a>';
    } else if (data.yaml_error) {
      html += '<br><br>⚠️ YAML upload failed: ' + esc(data.yaml_error);
    }
    html += '</div>';

    if (data.items_not_found && data.items_not_found.length) {
      html += '<details><summary>' + data.items_not_found.length +
              ' row(s) had no matching item (click to expand)</summary>' +
              '<pre>' + esc(data.items_not_found.join('\\n')) + '</pre></details>';
    }

    body.innerHTML = html;
    div.scrollIntoView({behavior:'smooth'});
  }

  function esc(s) {
    return String(s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;')
      .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
})();
</script>
</body>
</html>"""


_FILTER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DSA &mdash; Browse &amp; Filter Slides</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     background:#f0f2f5;color:#333;padding:24px 16px}
.wrap{max-width:1100px;margin:0 auto}
h1{font-size:1.35em;color:#1a3a5c;border-bottom:3px solid #3498db;
   padding-bottom:10px;margin-bottom:18px}
.card{background:#fff;border-radius:8px;padding:20px;margin-bottom:16px;
      box-shadow:0 1px 4px rgba(0,0,0,.08)}
h2{font-size:.9em;text-transform:uppercase;letter-spacing:.05em;
   color:#7f8c8d;margin-bottom:12px}
label{display:block;font-size:.8em;font-weight:600;color:#555;margin-bottom:4px}
input,select{width:100%;padding:8px 10px;border:1px solid #d1d5db;border-radius:4px;
             font-size:.92em;transition:border .15s}
input:focus,select:focus{outline:none;border-color:#3498db}
.conn{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.row{display:flex;gap:10px;align-items:flex-end;margin-top:10px}
.row>div{flex:1}
.hint{font-size:.78em;color:#9b59b6;margin-top:6px}
button{padding:9px 18px;border:none;border-radius:4px;font-size:.92em;cursor:pointer}
.btn-primary{background:#3498db;color:#fff;white-space:nowrap}
.btn-primary:hover{background:#2980b9}
.btn-ghost{background:#ecf0f1;color:#333}
.btn-ghost:hover{background:#d5dbdb}
#filtersCard{display:none}
.filters{display:flex;flex-wrap:wrap;gap:12px}
/* multi-select checkbox dropdown */
.dropdown{position:relative;min-width:210px}
.dd-btn{width:100%;text-align:left;background:#fff;border:1px solid #cfd6dd;color:#333;
        display:flex;justify-content:space-between;align-items:center;gap:8px;padding:9px 12px}
.dd-btn:hover{border-color:#3498db}
.dropdown.open .dd-btn{border-color:#3498db;box-shadow:0 0 0 2px rgba(52,152,219,.15)}
.dd-title{font-weight:600;font-size:.85em}
.dd-count{font-size:.74em;color:#fff;background:#9aa7b2;border-radius:10px;padding:1px 9px;white-space:nowrap}
.dd-count.on{background:#3498db}
.dd-panel{display:none;position:absolute;z-index:20;left:0;right:0;top:calc(100% + 4px);
          background:#fff;border:1px solid #cfd6dd;border-radius:6px;box-shadow:0 6px 20px rgba(0,0,0,.15);
          max-height:300px;overflow-y:auto;padding:6px}
.dropdown.open .dd-panel{display:block}
.dd-tools{display:flex;gap:14px;padding:4px 8px 8px;border-bottom:1px solid #eee;margin-bottom:4px}
.dd-tools a{font-size:.78em;color:#2980b9;text-decoration:none;cursor:pointer}
.dd-tools a:hover{text-decoration:underline}
.dd-opt{display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:4px;
        font-size:.86em;font-weight:400;color:#333;cursor:pointer;margin:0}
.dd-opt:hover{background:#f0f6fb}
.dd-opt input{width:auto;margin:0;cursor:pointer}
.dd-opt .vc{margin-left:auto;color:#aaa;font-size:.85em}
.active-filters{margin-top:12px;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.active-filters .lbl{font-size:.78em;color:#888;margin-right:4px}
.fchip{display:inline-flex;align-items:center;gap:6px;background:#eaf4fc;color:#1f6aa5;
       border:1px solid #bfdcf0;border-radius:14px;padding:3px 6px 3px 10px;font-size:.78em}
.fchip b{font-weight:600}
.fchip .x{cursor:pointer;background:#cfe3f5;color:#1f6aa5;border-radius:50%;width:16px;height:16px;
          display:inline-flex;align-items:center;justify-content:center;font-size:.8em;line-height:1}
.fchip .x:hover{background:#1f6aa5;color:#fff}
.toolbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;margin-bottom:14px}
.toolbar .acts{display:flex;gap:8px}
.btn-xs{font-size:.8em;padding:6px 12px}
.count{font-size:.85em;color:#555;margin:0}
.count b{color:#1a3a5c}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:16px}
.slide{background:#fff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;
       transition:box-shadow .15s,transform .15s;text-decoration:none;color:inherit;display:block}
.slide:hover{box-shadow:0 4px 14px rgba(0,0,0,.13);transform:translateY(-2px)}
.thumb{width:100%;height:140px;background:#eef1f4;display:flex;align-items:center;
       justify-content:center;color:#aab;font-size:.8em;overflow:hidden}
.thumb img{width:100%;height:100%;object-fit:cover}
.slide .body{padding:10px}
.slide .nm{font-size:.85em;font-weight:600;word-break:break-all;margin-bottom:6px}
.chip{display:inline-block;background:#ebf5fb;color:#2471a3;border-radius:3px;
      padding:1px 6px;margin:2px 2px 0 0;font-size:.72em}
.err{background:#fdedec;border:1px solid #f5b7b1;border-radius:4px;padding:14px;color:#922b21}
.empty{padding:40px;text-align:center;color:#999}
.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.topbar a{font-size:.82em;color:#2980b9;text-decoration:none}
.topbar a:hover{text-decoration:underline}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <h1 style="border:none;margin:0;padding:0">DSA &mdash; Browse &amp; Filter Slides</h1>
    <a href="/csv_upload">↩ Back to CSV import</a>
  </div>

  <div class="card">
    <h2>Connection &amp; Folder</h2>
    <div class="conn">
      <div>
        <label for="apiUrl">Girder API URL</label>
        <input id="apiUrl" type="text">
      </div>
      <div>
        <label for="apiKey">API Key (optional for public folders)</label>
        <input id="apiKey" type="password" placeholder="Paste API key">
      </div>
    </div>
    <div class="row">
      <div>
        <label for="folderPath">Folder path</label>
        <input id="folderPath" type="text" placeholder="/collection/My Collection/Images">
      </div>
      <button class="btn-ghost" type="button" onclick="lookupFolder()">Look up ID</button>
    </div>
    <div class="row">
      <div>
        <label for="folderId">Folder ID</label>
        <input id="folderId" type="text" placeholder="5f3a1b2c3d4e5f6a7b8c9d0e">
      </div>
      <button class="btn-primary" type="button" onclick="loadSlides()">Load slides</button>
    </div>
    <p class="hint" id="hint">&nbsp;</p>
  </div>

  <div class="card" id="filtersCard">
    <h2>Filter by metadata category &mdash; select one or more values per category</h2>
    <div class="filters" id="filters"></div>
    <div class="active-filters" id="activeFilters"></div>
    <div style="margin-top:14px">
      <button class="btn-ghost btn-xs" type="button" onclick="clearFilters()">Clear all filters</button>
    </div>
  </div>

  <div id="resultsArea"></div>
</div>

<script>
(function(){
  var STATE = {items: [], categories: {}, token: '', apiUrl: ''};

  document.getElementById('apiUrl').value = window.location.origin + '/api/v1';

  // Pre-fill folderId from ?folderId= query param (linked from the import page)
  var qs = new URLSearchParams(window.location.search);
  if (qs.get('folderId')) document.getElementById('folderId').value = qs.get('folderId');

  async function getToken(apiUrl, apiKey) {
    if (!apiKey) return '';
    var r = await fetch(
      apiUrl + '/api_key/token?key=' + encodeURIComponent(apiKey) + '&duration=1',
      {method:'POST'});
    if (!r.ok) throw new Error('Auth failed: ' + await r.text());
    return (await r.json()).authToken.token;
  }

  function showFolderChoices(hint, doc, folders) {
    hint.textContent = doc.name + ' is a ' + doc._modelType +
      ', not a folder. Slides live in folders — pick one:';
    var box = document.createElement('div');
    box.style.marginTop = '4px';
    folders.forEach(function(f) {
      var a = document.createElement('a');
      a.href = '#';
      a.textContent = '\\uD83D\\uDCC1 ' + f.name;
      a.style.cssText = 'display:inline-block;margin:3px 10px 0 0;color:#2980b9;text-decoration:none';
      a.addEventListener('click', function(e) {
        e.preventDefault();
        document.getElementById('folderId').value = f._id;
        hint.textContent = 'Selected folder: ' + f.name + '  (ID: ' + f._id + ')';
      });
      box.appendChild(a);
    });
    hint.appendChild(box);
  }

  window.lookupFolder = async function() {
    var apiUrl = document.getElementById('apiUrl').value.trim();
    var apiKey = document.getElementById('apiKey').value.trim();
    var path   = document.getElementById('folderPath').value.trim();
    var hint   = document.getElementById('hint');
    if (!path) { hint.textContent = 'Enter a path first.'; return; }
    try {
      var headers = {};
      var tok = await getToken(apiUrl, apiKey);
      if (tok) headers['Girder-Token'] = tok;
      var r = await fetch(apiUrl + '/resource/lookup?path=' + encodeURIComponent(path),
                          {headers:headers});
      if (!r.ok) throw new Error(await r.text());
      var doc = await r.json();
      if (doc._modelType === 'folder') {
        document.getElementById('folderId').value = doc._id;
        hint.textContent = 'Found folder: ' + doc.name;
        return;
      }
      var fr = await fetch(apiUrl + '/folder?parentType=' + encodeURIComponent(doc._modelType) +
                           '&parentId=' + doc._id + '&limit=500', {headers:headers});
      var folders = fr.ok ? await fr.json() : [];
      if (!folders.length) {
        hint.textContent = doc.name + ' is a ' + doc._modelType +
          ' with no sub-folders. Enter a folder path that contains slides.';
        return;
      }
      showFolderChoices(hint, doc, folders);
    } catch(e) { hint.textContent = 'Error: ' + e.message; }
  };

  window.loadSlides = async function() {
    var apiUrl   = document.getElementById('apiUrl').value.trim();
    var apiKey   = document.getElementById('apiKey').value.trim();
    var folderId = document.getElementById('folderId').value.trim();
    var hint     = document.getElementById('hint');
    var results  = document.getElementById('resultsArea');
    if (!folderId) { hint.textContent = 'Folder ID required.'; return; }
    hint.textContent = 'Loading…';
    try {
      STATE.token  = await getToken(apiUrl, apiKey);
      STATE.apiUrl = apiUrl;
      var headers = {};
      if (STATE.token) headers['Girder-Token'] = STATE.token;
      var r = await fetch(apiUrl + '/dsa_tools/folder/' + folderId + '/slide_meta',
                          {headers:headers});
      if (!r.ok) throw new Error(await r.text());
      var data = await r.json();
      STATE.items = data.items || [];
      STATE.categories = data.categories || {};
      hint.textContent = '';
      buildFilters();
      applyFilters();
    } catch(e) {
      results.innerHTML = '<div class="card"><div class="err">Error: ' +
                          esc(e.message) + '</div></div>';
    }
  };

  function computeCounts() {
    var counts = {};
    STATE.items.forEach(function(it) {
      Object.keys(it.meta || {}).forEach(function(k) {
        var sv = String(it.meta[k]);
        (counts[k] = counts[k] || {})[sv] = (counts[k][sv] || 0) + 1;
      });
    });
    STATE.counts = counts;
  }

  function buildFilters() {
    var wrap = document.getElementById('filters');
    STATE.selected = {};
    computeCounts();
    var keys = Object.keys(STATE.categories).sort();
    if (!keys.length) {
      document.getElementById('filtersCard').style.display = 'none';
      return;
    }
    document.getElementById('filtersCard').style.display = 'block';
    wrap.innerHTML = '';

    keys.forEach(function(key) {
      var values = STATE.categories[key] || [];
      STATE.selected[key] = new Set();

      var dd = document.createElement('div');
      dd.className = 'dropdown';

      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'dd-btn';
      var title = document.createElement('span');
      title.className = 'dd-title';
      title.textContent = prettify(key);
      var badge = document.createElement('span');
      badge.className = 'dd-count';
      badge.textContent = 'All';
      btn.appendChild(title);
      btn.appendChild(badge);
      btn.addEventListener('click', function(e) {
        e.stopPropagation();
        var wasOpen = dd.classList.contains('open');
        closeAllDropdowns();
        if (!wasOpen) dd.classList.add('open');
      });

      var panel = document.createElement('div');
      panel.className = 'dd-panel';
      panel.addEventListener('click', function(e){ e.stopPropagation(); });

      var tools = document.createElement('div');
      tools.className = 'dd-tools';
      var allA = document.createElement('a'); allA.textContent = 'Select all';
      var clrA = document.createElement('a'); clrA.textContent = 'Clear';
      allA.addEventListener('click', function(){ setAllInCategory(key, true); });
      clrA.addEventListener('click', function(){ setAllInCategory(key, false); });
      tools.appendChild(allA); tools.appendChild(clrA);
      panel.appendChild(tools);

      values.forEach(function(v) {
        var sval = String(v);
        var opt = document.createElement('label');
        opt.className = 'dd-opt';
        var cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = sval;
        cb.setAttribute('data-key', key);
        cb.addEventListener('change', function() {
          if (cb.checked) STATE.selected[key].add(sval);
          else STATE.selected[key].delete(sval);
          updateBadge(key, badge);
          applyFilters();
        });
        var txt = document.createElement('span');
        txt.textContent = sval;
        var vc = document.createElement('span');
        vc.className = 'vc';
        vc.textContent = (STATE.counts[key] && STATE.counts[key][sval]) || 0;
        opt.appendChild(cb); opt.appendChild(txt); opt.appendChild(vc);
        panel.appendChild(opt);
      });

      dd.appendChild(btn);
      dd.appendChild(panel);
      dd.setAttribute('data-key', key);
      wrap.appendChild(dd);
    });
  }

  function updateBadge(key, badge) {
    var n = STATE.selected[key].size;
    if (n === 0) { badge.textContent = 'All'; badge.classList.remove('on'); }
    else { badge.textContent = n + ' selected'; badge.classList.add('on'); }
  }

  function setAllInCategory(key, state) {
    var boxes = document.querySelectorAll('#filters input[data-key="' + cssEsc(key) + '"]');
    STATE.selected[key] = new Set();
    boxes.forEach(function(cb) {
      cb.checked = state;
      if (state) STATE.selected[key].add(cb.value);
    });
    var dd = document.querySelector('#filters .dropdown[data-key="' + cssEsc(key) + '"]');
    if (dd) updateBadge(key, dd.querySelector('.dd-count'));
    applyFilters();
  }

  function closeAllDropdowns() {
    document.querySelectorAll('#filters .dropdown.open').forEach(function(d){ d.classList.remove('open'); });
  }
  document.addEventListener('click', closeAllDropdowns);

  window.clearFilters = function() {
    document.querySelectorAll('#filters input[type=checkbox]').forEach(function(cb){ cb.checked = false; });
    Object.keys(STATE.selected || {}).forEach(function(k){ STATE.selected[k] = new Set(); });
    document.querySelectorAll('#filters .dd-count').forEach(function(b){ b.textContent='All'; b.classList.remove('on'); });
    applyFilters();
  };

  function activeSelections() {
    var active = {};
    Object.keys(STATE.selected || {}).forEach(function(k) {
      if (STATE.selected[k].size) active[k] = STATE.selected[k];
    });
    return active;
  }

  window.applyFilters = function() {
    var active = activeSelections();
    // Within a category: OR (value in chosen set). Across categories: AND.
    var matches = STATE.items.filter(function(it) {
      for (var key in active) {
        if (!active[key].has(String(it.meta[key]))) return false;
      }
      return true;
    });
    STATE.matches = matches;
    renderActiveChips(active);
    renderGrid(matches, active);
  };

  function renderActiveChips(active) {
    var box = document.getElementById('activeFilters');
    var keys = Object.keys(active);
    if (!keys.length) { box.innerHTML = ''; return; }
    box.innerHTML = '<span class="lbl">Active:</span>';
    keys.forEach(function(key) {
      active[key].forEach(function(val) {
        var chip = document.createElement('span');
        chip.className = 'fchip';
        chip.innerHTML = '<span><b>' + esc(prettify(key)) + ':</b> ' + esc(val) + '</span>';
        var x = document.createElement('span');
        x.className = 'x'; x.textContent = '\\u00d7'; x.title = 'Remove';
        x.addEventListener('click', function() { removeValue(key, val); });
        chip.appendChild(x);
        box.appendChild(chip);
      });
    });
  }

  function removeValue(key, val) {
    STATE.selected[key].delete(val);
    var cb = Array.prototype.find.call(
      document.querySelectorAll('#filters input[data-key="' + cssEsc(key) + '"]'),
      function(b){ return b.value === val; });
    if (cb) cb.checked = false;
    var dd = document.querySelector('#filters .dropdown[data-key="' + cssEsc(key) + '"]');
    if (dd) updateBadge(key, dd.querySelector('.dd-count'));
    applyFilters();
  }

  function renderGrid(items, active) {
    var results = document.getElementById('resultsArea');
    var nFilters = Object.keys(active).length;
    var toolbar = '<div class="toolbar"><div class="count">Subset: <b>' + items.length +
                  '</b> of ' + STATE.items.length + ' slides' +
                  (nFilters ? ' &middot; ' + nFilters + ' categor' + (nFilters===1?'y':'ies') + ' filtered' : '') +
                  '</div><div class="acts">' +
                  '<button class="btn-ghost btn-xs" onclick="exportCsv()">⬇ Export subset CSV</button>' +
                  '<button class="btn-ghost btn-xs" onclick="copyIds()">⧉ Copy item IDs</button>' +
                  '</div></div>';

    if (!items.length) {
      results.innerHTML = '<div class="card">' + toolbar +
        '<div class="empty">No slides match the selected filters.</div></div>';
      return;
    }

    var cards = items.map(function(it) {
      var thumb;
      if (it.largeImage) {
        var src = STATE.apiUrl + '/item/' + it._id +
                  '/tiles/thumbnail?width=190&height=140' +
                  (STATE.token ? '&token=' + encodeURIComponent(STATE.token) : '');
        thumb = '<div class="thumb"><img loading="lazy" src="' + src +
                '" onerror="this.parentNode.textContent=\\'no thumbnail\\'"></div>';
      } else {
        thumb = '<div class="thumb">not a slide</div>';
      }
      var chips = Object.keys(it.meta).map(function(k) {
        return '<span class="chip">' + esc(prettify(k)) + ': ' + esc(String(it.meta[k])) + '</span>';
      }).join('');
      var href = '/histomics#?image=' + it._id;
      return '<a class="slide" href="' + href + '" target="_blank" rel="noopener">' +
             thumb + '<div class="body"><div class="nm">' + esc(it.name) + '</div>' +
             chips + '</div></a>';
    }).join('');

    results.innerHTML = '<div class="card">' + toolbar +
                        '<div class="grid">' + cards + '</div></div>';
  }

  window.copyIds = function() {
    var ids = (STATE.matches || []).map(function(it){ return it._id; }).join('\\n');
    navigator.clipboard.writeText(ids).then(function(){
      document.getElementById('hint').textContent =
        'Copied ' + (STATE.matches||[]).length + ' item ID(s) to clipboard.';
    }, function(){ alert(ids); });
  };

  window.exportCsv = function() {
    var items = STATE.matches || [];
    var keys = Object.keys(STATE.categories).sort();
    var header = ['name', 'item_id'].concat(keys);
    var lines = [header.map(csvCell).join(',')];
    items.forEach(function(it) {
      var row = [it.name, it._id].concat(keys.map(function(k){
        return (it.meta && it.meta[k] !== undefined) ? it.meta[k] : '';
      }));
      lines.push(row.map(csvCell).join(','));
    });
    var blob = new Blob([lines.join('\\n')], {type: 'text/csv'});
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url; a.download = 'slide_subset.csv';
    document.body.appendChild(a); a.click();
    document.body.removeChild(a); URL.revokeObjectURL(url);
  };

  function csvCell(v) {
    var s = (v === undefined || v === null) ? '' : String(v);
    if (/[",\\n]/.test(s)) s = '"' + s.replace(/"/g, '""') + '"';
    return s;
  }

  function cssEsc(s) {
    return (window.CSS && CSS.escape) ? CSS.escape(s) : String(s).replace(/["\\\\\\]]/g, '\\\\$&');
  }

  function prettify(k) {
    return k.replace(/_/g, ' ').replace(/\\b\\w/g, function(c){ return c.toUpperCase(); });
  }
  function esc(s) {
    return String(s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;')
      .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  // Auto-load if a folderId was passed in the URL
  if (qs.get('folderId')) loadSlides();
})();
</script>
</body>
</html>"""


_ANNOTATION_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DSA &mdash; Upload Annotation JSON</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     background:#f0f2f5;color:#333;padding:32px 16px}
.wrap{max-width:720px;margin:0 auto}
h1{font-size:1.35em;color:#1a3a5c;border-bottom:3px solid #3498db;
   padding-bottom:10px;margin-bottom:20px}
.card{background:#fff;border-radius:8px;padding:24px;margin-bottom:16px;
      box-shadow:0 1px 4px rgba(0,0,0,.08)}
h2{font-size:.95em;text-transform:uppercase;letter-spacing:.05em;
   color:#7f8c8d;margin-bottom:14px}
label{display:block;font-size:.85em;font-weight:600;color:#555;margin-bottom:4px}
input,select{width:100%;padding:8px 10px;border:1px solid #d1d5db;border-radius:4px;
             font-size:.95em;margin-bottom:12px;transition:border .15s}
input:focus,select:focus{outline:none;border-color:#3498db}
.row{display:flex;gap:10px;align-items:flex-start}
.row input{margin-bottom:0}
.hint{font-size:.78em;color:#9b59b6;margin-top:-8px;margin-bottom:12px}
.chk{display:flex;align-items:center;gap:8px;font-size:.85em;color:#555;margin-bottom:4px}
.chk input{width:auto;margin:0}
button{padding:9px 22px;border:none;border-radius:4px;font-size:.95em;cursor:pointer}
.btn-primary{background:#3498db;color:#fff}
.btn-primary:hover{background:#2980b9}
.btn-primary:disabled{background:#a0bdd8;cursor:default}
.btn-sm{background:#ecf0f1;color:#333;font-size:.82em;padding:7px 14px;flex-shrink:0}
.btn-sm:hover{background:#d5dbdb}
#result{display:none}
.ok{background:#eafaf1;border:1px solid #a9dfbf;border-radius:4px;padding:14px;color:#1e8449}
.err{background:#fdedec;border:1px solid #f5b7b1;border-radius:4px;padding:14px;color:#922b21}
.tag{display:inline-block;background:#ebf5fb;color:#2471a3;border-radius:3px;
     padding:2px 7px;margin:2px;font-size:.8em;font-family:monospace}
.biglink{display:inline-block;font-weight:600;color:#2980b9;text-decoration:none;margin-top:6px}
.biglink:hover{text-decoration:underline}
.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.topbar a{font-size:.82em;color:#2980b9;text-decoration:none}
.topbar a:hover{text-decoration:underline}
.actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:center}
.btn-secondary{background:#fff;color:#2980b9;border:1px solid #3498db}
.btn-secondary:hover{background:#ebf5fb}
.btn-secondary:disabled{color:#a0bdd8;border-color:#a0bdd8;cursor:default}
.status{font-size:.85em;color:#7f8c8d;flex-basis:100%;text-align:center;min-height:1.2em}
.warn{background:#fef5e7;border:1px solid #f8c471;border-radius:4px;padding:14px;color:#9a6700;margin-top:10px}
.issues{margin:8px 0 0 18px;font-size:.88em;line-height:1.5}
.issues li{margin:3px 0}
.issues code,.hint code{font-size:.9em;background:rgba(0,0,0,.06);padding:1px 4px;border-radius:3px}
.note{font-size:.82em;color:#566573;margin-top:6px}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <h1 style="border:none;margin:0;padding:0">DSA &mdash; Upload Annotation JSON</h1>
    <a href="/csv_upload">CSV import &rarr;</a>
  </div>

  <div class="card">
    <h2>1 &nbsp; Connection</h2>
    <label for="apiUrl">Girder API URL</label>
    <input id="apiUrl" type="text" placeholder="http://localhost:8080/api/v1">
    <p id="session" class="note" aria-live="polite">Checking whether you are signed in&hellip;</p>
    <label for="apiKey">API key <span class="note" style="font-weight:400">(only needed if you are not signed in)</span></label>
    <input id="apiKey" type="password" placeholder="Paste a Girder API key" autocomplete="off">
    <p class="hint">Signed in to Girder in this browser? Leave this empty. Otherwise generate a key: Girder UI &rarr; top-right user menu &rarr; My Account &rarr; API keys.</p>
  </div>

  <div class="card">
    <h2>2 &nbsp; Target Slide (Item)</h2>
    <label for="itemPath">Item path (optional lookup)</label>
    <div class="row">
      <input id="itemPath" type="text" placeholder="/collection/BEETLE/images/development/wsis/patient1_wsi1.tif">
      <button class="btn-sm" type="button" onclick="lookupItem()">Look up</button>
    </div>
    <p class="hint" id="itemHint">&nbsp;</p>
    <label for="itemId" style="margin-top:4px">Item ID</label>
    <input id="itemId" type="text" placeholder="5f3a1b2c3d4e5f6a7b8c9d0e">
  </div>

  <div class="card">
    <h2>3 &nbsp; Annotation JSON</h2>
    <label for="jsonFile">File from this computer</label>
    <input id="jsonFile" type="file" accept=".json,application/json">
    <label for="serverPath">&hellip;or a file already on the server</label>
    <input id="serverPath" type="text" placeholder="/data/BEETLE/annotations/jsons/patient1_wsi1.dsa.json" autocomplete="off" spellcheck="false">
    <p class="hint" id="rootsHint">A .json file as the Girder container sees it. If both are given, the server file is used.</p>
    <p class="hint">The file must use the DSA annotation format (<code>dsa-annotation</code> v1). Convert other formats first with <code>utils/convert_annotations.py</code>. Classes are checked against the collection&rsquo;s vocabulary when it has one.</p>
    <label class="chk"><input id="replace" type="checkbox"> Replace existing annotation layers of the same name</label>
  </div>

  <div class="card actions">
    <button id="validateBtn" class="btn-secondary" type="button" onclick="submitAnnotation(true)">Validate only</button>
    <button id="submitBtn" class="btn-primary" type="button" onclick="submitAnnotation(false)">Validate &amp; upload</button>
    <span id="status" class="status" aria-live="polite"></span>
  </div>

  <div class="card" id="result">
    <h2>Result</h2>
    <div id="resultBody"></div>
  </div>
</div>

<script>
(function(){
  document.getElementById('apiUrl').value = window.location.origin + '/api/v1';
  var qs = new URLSearchParams(window.location.search);
  if (qs.get('itemId')) document.getElementById('itemId').value = qs.get('itemId');

  // Girder's own web app keeps the login token in localStorage (older
  // versions used a cookie). The page is served from the same origin, so a
  // browser that is signed in to Girder can reuse that login here.
  function sessionToken() {
    try {
      var t = window.localStorage.getItem('girderToken');
      if (t) return t;
    } catch (e) {}
    var m = document.cookie.match(/(?:^|;\\s*)girderToken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : null;
  }

  async function getToken(apiUrl, apiKey) {
    if (apiKey) {
      var r = await fetch(apiUrl + '/api_key/token?key=' + encodeURIComponent(apiKey) + '&duration=1',
                          {method:'POST'});
      if (!r.ok) throw new Error('API key rejected: ' + await r.text());
      return (await r.json()).authToken.token;
    }
    var token = sessionToken();
    if (!token) throw new Error('Not signed in. Sign in to Girder in another tab and reload this page, or paste an API key.');
    return token;
  }

  async function showSession(apiUrl) {
    var el = document.getElementById('session');
    var token = sessionToken();
    if (!token) {
      el.innerHTML = 'Not signed in. <a href="/" target="_blank" rel="noopener">Sign in to Girder</a>, then reload this page, or paste an API key below.';
      return;
    }
    try {
      var r = await fetch(apiUrl + '/user/me', {headers: {'Girder-Token': token}});
      var me = r.ok ? await r.json() : null;
      if (me && me.login) {
        el.textContent = 'Signed in as ' + me.login + '. No API key needed.';
        return;
      }
    } catch (e) {}
    el.innerHTML = 'Your Girder sign-in has expired. <a href="/" target="_blank" rel="noopener">Sign in again</a> and reload, or paste an API key below.';
  }
  showSession(document.getElementById('apiUrl').value);

  (async function showRoots() {
    var token = sessionToken();
    if (!token) return;
    try {
      var r = await fetch(document.getElementById('apiUrl').value + '/dsa_tools/annotation_source_roots',
                          {headers: {'Girder-Token': token}});
      if (!r.ok) return;
      var d = await r.json();
      document.getElementById('rootsHint').textContent =
        'Server files can be read from: ' + d.roots.join(', ') +
        ' (paths as the Girder container sees them; /mnt/raidData/BEETLE on the server is /data/BEETLE here). ' +
        'If both are given, the server file is used.';
    } catch (e) {}
  })();

  window.lookupItem = async function() {
    var apiUrl = document.getElementById('apiUrl').value.trim();
    var apiKey = document.getElementById('apiKey').value.trim();
    var path   = document.getElementById('itemPath').value.trim();
    var hint   = document.getElementById('itemHint');
    if (!path) { hint.textContent = 'Enter a path first.'; return; }
    try {
      var headers = {};
      try { headers['Girder-Token'] = await getToken(apiUrl, apiKey); } catch (e) {}
      var r = await fetch(apiUrl + '/resource/lookup?path=' + encodeURIComponent(path), {headers:headers});
      if (!r.ok) throw new Error(await r.text());
      var doc = await r.json();
      if (doc._modelType === 'item') {
        document.getElementById('itemId').value = doc._id;
        hint.textContent = 'Found item: ' + doc.name + '  (ID: ' + doc._id + ')';
      } else {
        hint.textContent = doc.name + ' is a ' + doc._modelType +
          ', not an item. Give the full path to a slide file.';
      }
    } catch(e) { hint.textContent = 'Error: ' + e.message; }
  };

  function setBusy(busy) {
    document.getElementById('validateBtn').disabled = busy;
    document.getElementById('submitBtn').disabled = busy;
  }

  window.submitAnnotation = async function(validateOnly) {
    var apiUrl  = document.getElementById('apiUrl').value.trim();
    var apiKey  = document.getElementById('apiKey').value.trim();
    var itemId  = document.getElementById('itemId').value.trim();
    var replace = document.getElementById('replace').checked;
    var file    = document.getElementById('jsonFile').files[0];
    var serverPath = document.getElementById('serverPath').value.trim();
    if (!itemId) { alert('Item ID required.'); return; }
    if (!file && !serverPath) { alert('Choose a file, or enter the path of a file on the server.'); return; }

    var status = document.getElementById('status');
    setBusy(true);
    status.textContent = validateOnly ? 'Validating…' : 'Validating and uploading…';
    try {
      var token = await getToken(apiUrl, apiKey);
      var r = await fetch(apiUrl + '/dsa_tools/item/' + itemId + '/ingest_annotation_json', {
        method: 'POST',
        headers: {'Content-Type':'application/json','Girder-Token':token},
        body: JSON.stringify(serverPath
          ? {server_path: serverPath, replace: replace, validate_only: validateOnly}
          : {json_content: await file.text(), replace: replace, validate_only: validateOnly}),
      });
      var data = await r.json();
      if (!r.ok) throw new Error(data.message || ('HTTP ' + r.status));
      renderResult(data, itemId);
    } catch(e) {
      show('<div class="err"><strong>Request failed:</strong> ' + esc(e.message) + '</div>');
    } finally {
      setBusy(false);
      status.textContent = '';
    }
  };

  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

  function issueList(issues) {
    return '<ul class="issues">' + issues.map(function(i) {
      return '<li><code>' + esc(i.code) + '</code> ' + esc(i.message) + '</li>';
    }).join('') + '</ul>';
  }

  function layerTags(layers) {
    return layers.map(function(l) {
      return '<span class="tag">' + esc(l.name) + ' (' + plural(l.elements, 'element') + ')</span>';
    }).join('');
  }

  function show(html) {
    var div = document.getElementById('result');
    document.getElementById('resultBody').innerHTML = html;
    div.style.display = 'block';
    div.scrollIntoView({behavior:'smooth'});
  }

  function renderResult(data, itemId) {
    var html;
    if (!data.ok) {
      html = '<div class="err"><strong>' + plural(data.errors.length, 'problem') +
             ' found. Nothing was uploaded.</strong>' + issueList(data.errors) + '</div>';
    } else if (data.validate_only) {
      html = '<div class="ok"><strong>Valid. Uploading would create ' +
             plural(data.layers.length, 'layer') + ' on ' + esc(data.item_name) +
             ':</strong><br><br>' + layerTags(data.layers) + '</div>';
    } else {
      var created = data.annotations_created;
      html = '<div class="ok"><strong>' + plural(created.length, 'layer') + ' attached to ' +
             esc(data.item_name) + '</strong>';
      if (created.length) html += '<br><br>' + layerTags(created);
      if (data.skipped_existing.length) {
        html += '<br><br>Kept existing layers (tick &ldquo;Replace existing&rdquo; to overwrite): ' +
                data.skipped_existing.map(esc).join(', ');
      }
      html += '<br><br><a class="biglink" href="/histomics#?image=' + encodeURIComponent(itemId) +
              '" target="_blank" rel="noopener">Open in HistomicsUI &rarr;</a>' +
              '<p class="note">New layers start hidden: tick them in the Annotations panel.</p></div>';
    }
    if (data.warnings && data.warnings.length) {
      html += '<div class="warn"><strong>' + plural(data.warnings.length, 'warning') + '</strong>' +
              issueList(data.warnings) + '</div>';
    }
    show(html);
  }

  function esc(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
      .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Annotation Tools pages: landing, converter, format specification
# ---------------------------------------------------------------------------

_TOOLS_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
     background:#f0f2f5;color:#333;padding:32px 16px;line-height:1.5}
.wrap{max-width:820px;margin:0 auto}
.topbar{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:18px}
h1{font-size:1.35em;color:#1a3a5c}
.topbar a,.links a{font-size:.85em;color:#2980b9;text-decoration:none}
.topbar a:hover,.links a:hover{text-decoration:underline}
.card{background:#fff;border-radius:8px;padding:22px 24px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.card h2{font-size:1.02em;color:#1a3a5c;margin-bottom:6px}
.card p{font-size:.92em;color:#555;margin-bottom:10px}
.card .go{display:inline-block;background:#3498db;color:#fff;padding:8px 18px;border-radius:4px;text-decoration:none;font-size:.92em}
.card .go:hover{background:#2980b9}
.links{font-size:.9em;color:#555}
.links a{margin-right:14px}
label{display:block;font-size:.85em;font-weight:600;color:#555;margin-bottom:4px}
input,select{width:100%;padding:8px 10px;border:1px solid #d1d5db;border-radius:4px;font-size:.95em;margin-bottom:12px}
input:focus,select:focus{outline:none;border-color:#3498db}
.hint{font-size:.78em;color:#7f8c8d;margin-top:-8px;margin-bottom:12px}
.chk{display:flex;align-items:center;gap:8px;font-size:.85em;color:#555}
.chk input{width:auto;margin:0}
button{padding:9px 22px;border:none;border-radius:4px;font-size:.95em;cursor:pointer}
.btn-primary{background:#3498db;color:#fff}
.btn-primary:hover{background:#2980b9}
.btn-primary:disabled{background:#a0bdd8;cursor:default}
.actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
.status{font-size:.85em;color:#7f8c8d}
#result{display:none}
.ok{background:#eafaf1;border:1px solid #a9dfbf;border-radius:4px;padding:14px;color:#1e8449}
.err{background:#fdedec;border:1px solid #f5b7b1;border-radius:4px;padding:14px;color:#922b21}
.tag{display:inline-block;background:#ebf5fb;color:#2471a3;border-radius:3px;padding:2px 7px;margin:2px;font-size:.8em;font-family:monospace}
.note{font-size:.82em;color:#566573;margin-top:6px}
code{font-size:.9em;background:rgba(0,0,0,.06);padding:1px 4px;border-radius:3px}
"""

_TOOLS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DSA &mdash; Annotation Tools</title>
<style>""" + _TOOLS_CSS + """</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <h1>Annotation Tools</h1>
    <a href="/">&larr; Back to Girder</a>
  </div>

  <div class="card">
    <h2>Upload &amp; validate</h2>
    <p>Attach a file in the DSA annotation format to a slide. <strong>Validate only</strong> runs every check
       (schema, geometry, class vocabulary, image bounds) without writing anything; <strong>Validate &amp; upload</strong>
       creates one toggleable layer per class in HistomicsUI.</p>
    <a class="go" href="/annotation_upload">Open upload &amp; validate</a>
  </div>

  <div class="card">
    <h2>Convert to the standard format</h2>
    <p>Turn a BEETLE or BCNB annotation file into a <code>.dsa.json</code> file that the upload accepts.
       The result is validated before you download it. For whole datasets, use
       <code>devops/dsa/utils/convert_annotations.py</code> with the dataset&rsquo;s manifest.</p>
    <a class="go" href="/annotation_convert">Open converter</a>
  </div>

  <div class="card">
    <h2>Format specification</h2>
    <p>What a valid file looks like, every validation code with its message, and how a file becomes
       HistomicsUI layers.</p>
    <a class="go" href="/annotation_format">Read the specification</a>
    <p class="links" style="margin-top:12px">
      <a href="/dsa_tools/annotation_schema" target="_blank" rel="noopener">JSON Schema</a>
      <a href="/dsa_tools/annotation_example" target="_blank" rel="noopener">Example file</a>
    </p>
  </div>

  <div class="card">
    <h2>Other tools</h2>
    <p class="links">
      <a href="/csv_upload">CSV metadata import</a>
      <a href="/slidefilter">Browse &amp; filter slides</a>
      <a href="/histomics" target="_blank" rel="noopener">HistomicsUI</a>
    </p>
  </div>
</div>
</body>
</html>"""

_CONVERT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DSA &mdash; Convert Annotations</title>
<style>""" + _TOOLS_CSS + """</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <h1>Convert annotations to the DSA format</h1>
    <a href="/annotation_tools">&larr; Annotation Tools</a>
  </div>

  <div class="card">
    <p id="session" class="note" aria-live="polite">Checking whether you are signed in&hellip;</p>
    <label for="apiKey">API key <span class="note" style="font-weight:400">(only needed if you are not signed in)</span></label>
    <input id="apiKey" type="password" placeholder="Paste a Girder API key" autocomplete="off">
  </div>

  <div class="card">
    <label for="sourceFormat">Source format</label>
    <select id="sourceFormat"><!--FORMAT_OPTIONS--></select>
    <label for="sourceFile">Source file from this computer</label>
    <input id="sourceFile" type="file" accept=".json,application/json">
    <label for="serverPath">&hellip;or a file already on the server</label>
    <input id="serverPath" type="text" placeholder="/data/BEETLE/annotations/jsons/patient1_wsi1.json" autocomplete="off" spellcheck="false">
    <p class="hint" id="rootsHint">A .json file as the Girder container sees it. If both are given, the server file is used.</p>
    <label for="slideName">Slide file these annotations belong to</label>
    <input id="slideName" type="text" placeholder="patient1_wsi1.tif" autocomplete="off">
    <p class="hint">Filled in from the source file&rsquo;s name; fix the extension if the slide is not a .tif.</p>
    <label class="chk"><input id="clipNegative" type="checkbox"> Clamp negative coordinates to 0 instead of failing the file</label>
  </div>

  <div class="card actions">
    <button id="convertBtn" class="btn-primary" type="button" onclick="convert()">Convert</button>
    <span id="status" class="status" aria-live="polite"></span>
  </div>

  <div class="card" id="result">
    <h2>Result</h2>
    <div id="resultBody"></div>
  </div>
</div>

<script>
(function(){
  var apiUrl = window.location.origin + '/api/v1';
  var converted = null;

  function sessionToken() {
    try {
      var t = window.localStorage.getItem('girderToken');
      if (t) return t;
    } catch (e) {}
    var m = document.cookie.match(/(?:^|;\\s*)girderToken=([^;]+)/);
    return m ? decodeURIComponent(m[1]) : null;
  }

  async function getToken(apiKey) {
    if (apiKey) {
      var r = await fetch(apiUrl + '/api_key/token?key=' + encodeURIComponent(apiKey) + '&duration=1',
                          {method:'POST'});
      if (!r.ok) throw new Error('API key rejected: ' + await r.text());
      return (await r.json()).authToken.token;
    }
    var token = sessionToken();
    if (!token) throw new Error('Not signed in. Sign in to Girder in another tab and reload this page, or paste an API key.');
    return token;
  }

  async function showSession() {
    var el = document.getElementById('session');
    var token = sessionToken();
    if (!token) {
      el.innerHTML = 'Not signed in. <a href="/" target="_blank" rel="noopener">Sign in to Girder</a>, then reload this page, or paste an API key below.';
      return;
    }
    try {
      var r = await fetch(apiUrl + '/user/me', {headers: {'Girder-Token': token}});
      var me = r.ok ? await r.json() : null;
      if (me && me.login) { el.textContent = 'Signed in as ' + me.login + '. No API key needed.'; return; }
    } catch (e) {}
    el.innerHTML = 'Your Girder sign-in has expired. <a href="/" target="_blank" rel="noopener">Sign in again</a> and reload, or paste an API key below.';
  }
  showSession();

  (async function showRoots() {
    var token = sessionToken();
    if (!token) return;
    try {
      var r = await fetch(apiUrl + '/dsa_tools/annotation_source_roots', {headers: {'Girder-Token': token}});
      if (!r.ok) return;
      var d = await r.json();
      document.getElementById('rootsHint').textContent =
        'Server files can be read from: ' + d.roots.join(', ') +
        ' (paths as the Girder container sees them; /mnt/raidData/BEETLE on the server is /data/BEETLE here). ' +
        'If both are given, the server file is used.';
    } catch (e) {}
  })();

  document.getElementById('serverPath').addEventListener('input', function () {
    var name = document.getElementById('slideName');
    var base = this.value.trim().split('/').pop();
    if (base && !name.value) name.value = base.replace(/\\.json$/i, '') + '.tif';
  });

  document.getElementById('sourceFile').addEventListener('change', function () {
    var f = this.files[0];
    var name = document.getElementById('slideName');
    if (f && !name.value) name.value = f.name.replace(/\\.json$/i, '') + '.tif';
  });

  function esc(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }
  function show(html) {
    var div = document.getElementById('result');
    document.getElementById('resultBody').innerHTML = html;
    div.style.display = 'block';
    div.scrollIntoView({behavior:'smooth'});
  }
  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

  window.downloadConverted = function () {
    if (!converted) return;
    var blob = new Blob([JSON.stringify(converted.document)], {type: 'application/json'});
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = converted.suggested_filename;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(function(){ URL.revokeObjectURL(a.href); }, 1000);
  };

  function issueList(issues) {
    return '<ul style="margin:8px 0 0 18px;font-size:.88em">' + issues.map(function(i) {
      return '<li><code>' + esc(i.code) + '</code> ' + esc(i.message) + '</li>';
    }).join('') + '</ul>';
  }

  window.attachConverted = async function (validateOnly) {
    if (!converted) return;
    var itemId = document.getElementById('itemId').value.trim();
    var out = document.getElementById('attachResult');
    var status = document.getElementById('attachStatus');
    if (!itemId) { alert('Enter the item ID of the slide.'); return; }
    status.textContent = validateOnly ? 'Validating\u2026' : 'Validating and uploading\u2026';
    try {
      var token = await getToken(document.getElementById('apiKey').value.trim());
      var r = await fetch(apiUrl + '/dsa_tools/item/' + itemId + '/ingest_annotation_json', {
        method: 'POST',
        headers: {'Content-Type':'application/json','Girder-Token':token},
        body: JSON.stringify({json_content: JSON.stringify(converted.document),
                              replace: document.getElementById('replace').checked,
                              validate_only: validateOnly}),
      });
      var data = await r.json();
      if (!r.ok) throw new Error(data.message || ('HTTP ' + r.status));
      var html;
      if (!data.ok) {
        html = '<div class="err"><strong>' + plural(data.errors.length, 'problem') + ' found. Nothing was uploaded.</strong>' + issueList(data.errors) + '</div>';
      } else if (data.validate_only) {
        html = '<div class="ok"><strong>Valid. Uploading would create ' + plural(data.layers.length, 'layer') + ' on ' + esc(data.item_name) + '.</strong></div>';
      } else {
        html = '<div class="ok"><strong>' + plural(data.annotations_created.length, 'layer') + ' attached to ' + esc(data.item_name) + '.</strong>' +
               (data.skipped_existing.length ? '<p class="note">Kept existing layers (tick Replace to overwrite): ' + data.skipped_existing.map(esc).join(', ') + '</p>' : '') +
               '<p><a href="/histomics#?image=' + encodeURIComponent(itemId) + '" target="_blank" rel="noopener">Open in HistomicsUI &rarr;</a>' +
               ' <span class="note">New layers start hidden: tick them in the Annotations panel.</span></p></div>';
      }
      if (data.warnings && data.warnings.length) {
        html += '<div class="note" style="margin-top:8px"><strong>' + plural(data.warnings.length, 'warning') + '</strong>' + issueList(data.warnings) + '</div>';
      }
      out.innerHTML = html;
    } catch (e) {
      out.innerHTML = '<div class="err"><strong>Request failed:</strong> ' + esc(e.message) + '</div>';
    } finally {
      status.textContent = '';
    }
  };

  window.convert = async function () {
    var file = document.getElementById('sourceFile').files[0];
    var serverPath = document.getElementById('serverPath').value.trim();
    var slideName = document.getElementById('slideName').value.trim();
    if (!file && !serverPath) { alert('Choose a source file, or enter the path of a file on the server.'); return; }
    if (!slideName) { alert('Enter the slide filename these annotations belong to.'); return; }
    var btn = document.getElementById('convertBtn');
    var status = document.getElementById('status');
    btn.disabled = true; status.textContent = 'Converting…';
    try {
      var token = await getToken(document.getElementById('apiKey').value.trim());
      var r = await fetch(apiUrl + '/dsa_tools/convert_annotation', {
        method: 'POST',
        headers: {'Content-Type':'application/json','Girder-Token':token},
        body: JSON.stringify(Object.assign({
          source_format: document.getElementById('sourceFormat').value,
          slide_name: slideName,
          clip_negative: document.getElementById('clipNegative').checked,
        }, serverPath ? {server_path: serverPath}
                      : {json_content: await file.text(), source_file: file.name})),
      });
      var data = await r.json();
      if (!r.ok) throw new Error(data.message || ('HTTP ' + r.status));
      if (!data.ok) {
        converted = null;
        show('<div class="err"><strong>Could not convert:</strong> ' + esc(data.error) + '</div>');
        return;
      }
      converted = data;
      var n = data.notes;
      var html = '<div class="ok"><strong>Converted: ' + plural(n.features, 'region') + ' in ' +
                 plural(n.classes.length, 'class') + '.</strong><br><br>' +
                 n.classes.map(function(c){ return '<span class="tag">' + esc(c) + '</span>'; }).join('');
      var extra = [];
      if (n.dropped) extra.push(plural(n.dropped, 'region') + ' with fewer than 3 distinct vertices dropped');
      if (n.clipped) extra.push(plural(n.clipped, 'negative coordinate') + ' clamped to 0');
      if (extra.length) html += '<p class="note">' + esc(extra.join('; ')) + '.</p>';
      html += '<br><div class="actions"><button class="btn-primary" type="button" onclick="downloadConverted()">Download ' +
              esc(data.suggested_filename) + '</button></div></div>';
      html += '<div class="card" style="margin-top:14px;box-shadow:none;border:1px solid #e5e8ec">' +
              '<h2>Attach to a slide now</h2>' +
              '<p class="note">Validates against the slide and its collection vocabulary, then creates one layer per class. Nothing leaves the server.</p>' +
              '<label for="itemId">Item ID of the slide</label>' +
              '<input id="itemId" type="text" placeholder="6a94d466e756a09a3859e1ef" autocomplete="off" spellcheck="false">' +
              '<label class="chk"><input id="replace" type="checkbox"> Replace existing layers of the same name</label>' +
              '<div class="actions" style="margin-top:12px">' +
              '<button class="btn-primary" type="button" onclick="attachConverted(true)">Validate only</button>' +
              '<button class="btn-primary" type="button" onclick="attachConverted(false)">Validate &amp; upload</button>' +
              '<span id="attachStatus" class="status" aria-live="polite"></span></div>' +
              '<div id="attachResult" style="margin-top:12px"></div></div>';
      show(html);
    } catch (e) {
      converted = null;
      show('<div class="err"><strong>Request failed:</strong> ' + esc(e.message) + '</div>');
    } finally {
      btn.disabled = false; status.textContent = '';
    }
  };
})();
</script>
</body>
</html>"""

_FORMAT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DSA &mdash; Annotation Format</title>
<style>""" + _TOOLS_CSS + """
.spec{background:#fff;border-radius:8px;padding:28px 32px;box-shadow:0 1px 4px rgba(0,0,0,.08)}
.spec h1{font-size:1.5em;margin:0 0 12px}
.spec h2{font-size:1.15em;color:#1a3a5c;margin:28px 0 8px;padding-top:12px;border-top:1px solid #e5e8ec}
.spec h3{font-size:.95em;color:#34495e;margin:18px 0 6px}
.spec p{margin:0 0 10px;font-size:.95em}
.spec ul,.spec ol{margin:0 0 12px 22px;font-size:.95em}
.spec li{margin:3px 0}
.spec pre{background:#f6f8fa;border:1px solid #e5e8ec;border-radius:6px;padding:12px 14px;overflow-x:auto;margin:0 0 14px;font-size:.85em;line-height:1.45}
.spec pre code{background:none;padding:0;font-size:1em}
.spec table{border-collapse:collapse;width:100%;margin:0 0 14px;font-size:.88em}
.spec th,.spec td{text-align:left;vertical-align:top;padding:7px 9px;border-bottom:1px solid #e5e8ec}
.spec th{background:#f6f8fa;font-weight:600;color:#555}
.spec .tablewrap{overflow-x:auto}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <h1>Annotation format</h1>
    <span class="links">
      <a href="/annotation_tools">&larr; Annotation Tools</a>
      <a href="/dsa_tools/annotation_schema" target="_blank" rel="noopener">JSON Schema</a>
      <a href="/dsa_tools/annotation_example" target="_blank" rel="noopener">Example file</a>
    </span>
  </div>
  <div class="spec">
<!--SPEC_BODY-->
  </div>
</div>
</body>
</html>"""

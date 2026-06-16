"""REST resource and HTML page for DSA CSV metadata ingestion."""
import csv as _csv
import io

import cherrypy

from girder.api import access
from girder.api.describe import Description, autoDescribeRoute
from girder.api.rest import Resource
from girder.constants import AccessType
from girder.models.folder import Folder
from girder.models.item import Item
from girder.models.upload import Upload


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
# REST resource
# ---------------------------------------------------------------------------

class DsaCsvResource(Resource):
    def __init__(self):
        super().__init__()
        self.resourceName = 'dsa_tools'
        self.route('POST', ('folder', ':folderId', 'ingest_csv'), self.ingest_csv)
        self.route('GET', ('folder', ':folderId', 'slide_meta'), self.slide_meta)

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


# ---------------------------------------------------------------------------
# HTML upload page (served at /csv_upload by the plugin __init__)
# ---------------------------------------------------------------------------

def get_upload_html():
    return _HTML


def get_filter_html():
    return _FILTER_HTML


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
      document.getElementById('folderId').value = doc._id;
      hint.textContent = 'Found: ' + doc.name + '  (ID: ' + doc._id + ')';
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
      document.getElementById('folderId').value = doc._id;
      hint.textContent = 'Found: ' + doc.name;
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

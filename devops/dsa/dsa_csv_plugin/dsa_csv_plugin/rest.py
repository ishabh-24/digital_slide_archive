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

def _detect_format(values):
    """Return 'number' if every non-empty value is numeric, else 'text'."""
    clean = [v.strip() for v in values if v and str(v).strip()]
    if not clean:
        return 'text'
    try:
        for v in clean:
            float(v)
        return 'number'
    except (ValueError, TypeError):
        return 'text'


def _build_yaml_dict(meta_keys, format_map):
    columns = [
        {'type': 'image', 'value': 'thumbnail', 'title': 'Thumbnail', 'width': 160, 'height': 100},
        {'type': 'record', 'value': 'name', 'title': 'Name'},
        {'type': 'record', 'value': 'size', 'title': 'Size'},
    ]
    for key in meta_keys:
        columns.append({
            'type': 'metadata',
            'value': key,
            'title': key.replace('_', ' ').title(),
            'format': format_map.get(key, 'text'),
        })
    return {'itemList': {'layout': {'mode': 'grid', 'flatten': False}, 'columns': columns}}


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

        # Collect all values per key for format detection
        values_by_key = {k: [] for k in meta_keys}
        for row in rows:
            for k in meta_keys:
                v = (row.get(k) or '').strip()
                if v:
                    values_by_key[k].append(v)

        format_map = {k: _detect_format(values_by_key[k]) for k in meta_keys}

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
                if format_map[k] == 'number':
                    try:
                        meta[k] = float(v) if '.' in v else int(v)
                    except ValueError:
                        meta[k] = v
                else:
                    meta[k] = v

            if meta:
                Item().setMetadata(item_doc, meta)
                updated += 1

        # Generate and upload filter config YAML
        yaml_dict = _build_yaml_dict(meta_keys, format_map)
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
            'format_map': format_map,
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
        var fmt = data.format_map && data.format_map[col];
        html += '<span class="tag' + (fmt === 'number' ? ' number' : '') + '">' +
                esc(col) + (fmt ? ' — ' + fmt : '') + '</span>';
      });
    }

    if (data.yaml_uploaded) {
      html += '<br><br>✓ <em>.large_image_config.yaml</em> uploaded. ' +
              'Open the folder in HistomicsUI to see the new filter fields.';
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

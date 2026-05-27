#!/usr/bin/env python3
"""
Apply CSV metadata to Girder items and configure large_image / HistomicsUI item lists.

Item metadata
  PUT /api/v1/item/{id}/metadata  (merge into item['meta'])

Folder item-list + filter columns (supported by upstream large_image)
  Place a file named ``.large_image_config.yaml`` in the folder (or parent).
  See: https://girder.github.io/large_image/girder_config_options.html

  This script can generate that YAML from a list of metadata keys and upload it
  to a folder.

Optional folder metadata (custom deployments only)
  Some forks may read a JSON blob from folder meta, e.g.
  ``x-histomicsui-filter-config``. Upstream HistomicsUI + large_image use the
  YAML file above, not this key. Use ``set-folder-filter-meta`` only if your
  stack documents it.

Requires: pip install girder-client
Optional: pip install python-dotenv pyyaml

Examples
--------
  export GIRDER_API_KEY=...
  python item_metadata_csv.py apply-csv --folder-id FOLDER \\
    --csv slides.csv --match-on item_id

  python item_metadata_csv.py write-large-image-yaml --keys stain,cohort,patient_id \\
    -o .large_image_config.yaml

  python item_metadata_csv.py upload-large-image-yaml --folder-id FOLDER \\
    --yaml .large_image_config.yaml
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

try:
    import girder_client
except ImportError:
    girder_client = None  # type: ignore


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore[import-not-found]
    except ImportError:
        return
    script_dir = Path(__file__).resolve().parent
    for path in (script_dir / ".env", Path.cwd() / ".env"):
        if path.is_file():
            load_dotenv(path)
            return
    load_dotenv()


def connect(api_url: str, api_key: str | None, username: str | None, password: str | None):
    if girder_client is None:
        print("Install girder-client: pip install girder-client", file=sys.stderr)
        sys.exit(1)
    client = girder_client.GirderClient(apiUrl=api_url.rstrip("/"))
    if api_key:
        client.authenticate(apiKey=api_key)
    elif username and password is not None:
        client.authenticate(username, password)
    else:
        print("Provide --api-key or GIRDER_API_KEY, or --username and --password.", file=sys.stderr)
        sys.exit(1)
    return client


def _item_metadata_put(client: Any, item_id: str, meta: dict[str, Any]) -> None:
    """Merge metadata keys onto the item (Girder 3+ JSON body)."""
    client.put("item/%s/metadata" % item_id, json=meta)


def _list_folder_items(client: Any, folder_id: str) -> list[dict[str, Any]]:
    return list(client.listItem(folder_id))


def _resolve_item_id(
    client: Any,
    folder_id: str,
    row: dict[str, str],
    match_on: str,
) -> str | None:
    if match_on == "item_id":
        raw = (row.get("item_id") or row.get("_id") or "").strip()
        if not raw:
            return None
        try:
            client.get("item/%s" % raw)
            return raw
        except Exception:
            return None
    if match_on == "name":
        name = (row.get("name") or row.get("item_name") or "").strip()
        if not name:
            return None
        for it in _list_folder_items(client, folder_id):
            if it.get("name") == name:
                return str(it["_id"])
    return None


def cmd_apply_csv(client: Any, args: argparse.Namespace) -> None:
    skip = set(args.skip_columns or []) | {"item_id", "_id", "name", "item_name"}
    with open(args.csv, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            print("CSV has no header row.", file=sys.stderr)
            sys.exit(1)
        rows = list(reader)

    updated = 0
    errors: list[str] = []
    for i, row in enumerate(rows, start=2):
        item_id = _resolve_item_id(client, args.folder_id, row, args.match_on)
        if not item_id:
            errors.append("line %d: could not resolve item (%s)" % (i, args.match_on))
            continue
        meta: dict[str, Any] = {}
        for k, v in row.items():
            if not k or k in skip:
                continue
            if v is None or str(v).strip() == "":
                continue
            # Girder meta: avoid dots in keys (Girder restriction)
            key = k.strip()
            if "." in key:
                errors.append("line %d: skip key %r (dots not allowed in Girder metadata keys)" % (i, key))
                continue
            meta[key] = _coerce_value(v.strip())
        if not meta:
            continue
        try:
            _item_metadata_put(client, item_id, meta)
            updated += 1
        except Exception as ex:
            errors.append("line %d: item %s: %s" % (i, item_id, ex))

    print("Updated metadata on %d item(s)." % updated)
    if errors:
        print("\nWarnings / errors:", file=sys.stderr)
        for e in errors[:50]:
            print("  %s" % e, file=sys.stderr)
        if len(errors) > 50:
            print("  ... and %d more" % (len(errors) - 50), file=sys.stderr)


def _coerce_value(s: str) -> Any:
    if s.lower() in ("true", "false"):
        return s.lower() == "true"
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        return s


def _detect_format(values: list[str]) -> str:
    """Return 'number' if every non-empty value parses as float, else 'text'."""
    clean = [v.strip() for v in values if v and v.strip()]
    if not clean:
        return 'text'
    try:
        for v in clean:
            float(v)
        return 'number'
    except ValueError:
        return 'text'


def build_large_image_yaml_dict(metadata_keys: list[str]) -> dict[str, Any]:
    """
    Minimal itemList that shows thumbnails, name, and metadata columns with
    text/category-style filtering (see large_image docs: format field).
    """
    columns: list[dict[str, Any]] = [
        {"type": "image", "value": "thumbnail", "title": "Thumbnail", "width": 160, "height": 100},
        {"type": "record", "value": "name", "title": "Name"},
        {"type": "record", "value": "size", "title": "Size"},
    ]
    for key in metadata_keys:
        key = key.strip()
        if not key:
            continue
        columns.append(
            {
                "type": "metadata",
                "value": key,
                "title": key.replace("_", " ").title(),
                "format": "text",
            }
        )
    return {
        "itemList": {
            "layout": {"mode": "grid", "flatten": False},
            "columns": columns,
        }
    }


def cmd_write_large_image_yaml(client: Any, args: argparse.Namespace) -> None:
    del client  # unused
    keys = [k.strip() for k in args.keys.split(",") if k.strip()]
    data = build_large_image_yaml_dict(keys)
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        print("Install PyYAML: pip install pyyaml", file=sys.stderr)
        sys.exit(1)
    out = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    Path(args.output).write_text(out, encoding="utf-8")
    print("Wrote %s" % args.output)


def cmd_upload_large_image_yaml(client: Any, args: argparse.Namespace) -> None:
    path = Path(args.yaml)
    if not path.is_file():
        print("File not found: %s" % path, file=sys.stderr)
        sys.exit(1)
    # girder_client.uploadFileToFolder(folderId, filepath, ...)
    upload = getattr(client, "uploadFileToFolder", None)
    if upload is None:
        print("girder_client.uploadFileToFolder not available", file=sys.stderr)
        sys.exit(1)
    upload(
        args.folder_id,
        str(path.resolve()),
        filename=".large_image_config.yaml",
        mimeType="text/yaml",
    )
    print("Uploaded .large_image_config.yaml to folder %s" % args.folder_id)


def cmd_set_folder_filter_meta(client: Any, args: argparse.Namespace) -> None:
    """Store JSON in folder meta under a custom key (fork-specific; not standard upstream)."""
    if args.json_file:
        payload = json.loads(Path(args.json_file).read_text(encoding="utf-8"))
    else:
        keys = [k.strip() for k in args.keys.split(",") if k.strip()]
        payload = {
            "itemList": build_large_image_yaml_dict(keys)["itemList"],
            "source": "generated by item_metadata_csv.py",
        }
    key = args.meta_key
    # Girder folder metadata merge: PUT with body as dict of meta keys
    # Value may be a nested dict; Girder stores it in folder['meta'][key]
    client.put("folder/%s/metadata" % args.folder_id, json={key: payload})
    print("Set folder metadata key %r on folder %s" % (key, args.folder_id))


def cmd_ingest_csv(client: Any, args: argparse.Namespace) -> None:
    """One-shot: apply CSV metadata + auto-generate and upload filter YAML."""
    skip = set(args.skip_columns or []) | {"item_id", "_id", "name", "item_name"}
    with open(args.csv, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            print("CSV has no header row.", file=sys.stderr)
            sys.exit(1)
        rows = list(reader)
        meta_keys = [
            k.strip() for k in reader.fieldnames
            if k and k.strip() not in skip and "." not in k.strip()
        ]

    # Collect values for format detection
    values_by_key: dict[str, list[str]] = {k: [] for k in meta_keys}
    for row in rows:
        for k in meta_keys:
            v = (row.get(k) or "").strip()
            if v:
                values_by_key[k].append(v)

    format_map = {k: _detect_format(values_by_key[k]) for k in meta_keys}

    # Apply metadata to items
    items = list(client.listItem(args.folder_id))
    name_to_id = {it["name"]: str(it["_id"]) for it in items}
    id_set = {str(it["_id"]) for it in items}

    updated = 0
    errors: list[str] = []

    for i, row in enumerate(rows, start=2):
        if args.match_on == "item_id":
            item_id = (row.get("item_id") or row.get("_id") or "").strip()
            if not item_id or item_id not in id_set:
                errors.append("line %d: item id not found: %r" % (i, item_id))
                continue
        else:
            name = (row.get("name") or row.get("item_name") or "").strip()
            item_id = name_to_id.get(name)
            if not item_id:
                errors.append("line %d: no item named %r" % (i, name))
                continue

        meta: dict[str, Any] = {}
        for k in meta_keys:
            v = (row.get(k) or "").strip()
            if not v:
                continue
            if format_map[k] == "number":
                try:
                    meta[k] = float(v) if "." in v else int(v)
                except ValueError:
                    meta[k] = v
            else:
                meta[k] = _coerce_value(v)
        if meta:
            try:
                _item_metadata_put(client, item_id, meta)
                updated += 1
            except Exception as ex:
                errors.append("line %d: item %s: %s" % (i, item_id, ex))

    print("Updated metadata on %d item(s)." % updated)

    # Generate and upload .large_image_config.yaml
    yaml_dict = build_large_image_yaml_dict_with_formats(meta_keys, format_map)
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        print("Install PyYAML to upload filter config: pip install pyyaml", file=sys.stderr)
        yaml = None  # type: ignore[assignment]

    if yaml is not None:
        yaml_content = yaml.safe_dump(yaml_dict, sort_keys=False, default_flow_style=False)
        import tempfile, os as _os
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8") as tf:
            tf.write(yaml_content)
            tmp_path = tf.name
        try:
            upload = getattr(client, "uploadFileToFolder", None)
            if upload is None:
                print("girder_client.uploadFileToFolder not available; skipping YAML upload.", file=sys.stderr)
            else:
                upload(args.folder_id, tmp_path, filename=".large_image_config.yaml", mimeType="text/yaml")
                print("Uploaded .large_image_config.yaml with columns: %s" % ", ".join(meta_keys))
                print("Formats: %s" % ", ".join("%s=%s" % (k, v) for k, v in format_map.items()))
        finally:
            _os.unlink(tmp_path)

    if errors:
        print("\nWarnings / errors:", file=sys.stderr)
        for e in errors[:50]:
            print("  %s" % e, file=sys.stderr)
        if len(errors) > 50:
            print("  ... and %d more" % (len(errors) - 50), file=sys.stderr)


def build_large_image_yaml_dict_with_formats(
    metadata_keys: list[str], format_map: dict[str, str]
) -> dict[str, Any]:
    """Like build_large_image_yaml_dict but uses per-column formats from detection."""
    columns: list[dict[str, Any]] = [
        {"type": "image", "value": "thumbnail", "title": "Thumbnail", "width": 160, "height": 100},
        {"type": "record", "value": "name", "title": "Name"},
        {"type": "record", "value": "size", "title": "Size"},
    ]
    for key in metadata_keys:
        key = key.strip()
        if not key:
            continue
        columns.append({
            "type": "metadata",
            "value": key,
            "title": key.replace("_", " ").title(),
            "format": format_map.get(key, "text"),
        })
    return {"itemList": {"layout": {"mode": "grid", "flatten": False}, "columns": columns}}


def main() -> None:
    _load_dotenv()
    p = argparse.ArgumentParser(description="CSV → item metadata; large_image YAML for Histomics item list.")
    p.add_argument(
        "--api-url",
        default=os.environ.get("GIRDER_API_URL", "http://localhost:8080/api/v1"),
    )
    p.add_argument("--api-key", default=os.environ.get("GIRDER_API_KEY"))
    p.add_argument("--username")
    p.add_argument("--password")

    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("apply-csv", help="Merge CSV columns into item meta (by item_id or name)")
    a.add_argument("--folder-id", required=True, help="Folder containing slide items")
    a.add_argument("--csv", required=True, help="CSV path")
    a.add_argument(
        "--match-on",
        choices=("item_id", "name"),
        default="item_id",
        help="Match rows to Girder items (default: item_id column)",
    )
    a.add_argument(
        "--skip-columns",
        default=None,
        help="Comma-separated CSV columns to skip (in addition to match column names)",
    )
    a.set_defaults(func=cmd_apply_csv)

    w = sub.add_parser(
        "write-large-image-yaml",
        help="Write .large_image_config.yaml listing metadata keys as columns (use with upload-large-image-yaml)",
    )
    w.add_argument("--keys", required=True, help="Comma-separated meta keys, e.g. stain,cohort")
    w.add_argument("-o", "--output", default=".large_image_config.yaml")
    w.set_defaults(func=cmd_write_large_image_yaml)

    u = sub.add_parser(
        "upload-large-image-yaml",
        help="Upload a YAML file into a folder as .large_image_config.yaml",
    )
    u.add_argument("--folder-id", required=True)
    u.add_argument("--yaml", required=True, help="Path to YAML file")
    u.set_defaults(func=cmd_upload_large_image_yaml)

    ic = sub.add_parser(
        "ingest-csv",
        help="One-shot: apply CSV metadata to items AND upload filter YAML (combines apply-csv + write/upload YAML)",
    )
    ic.add_argument("--folder-id", required=True, help="Folder containing slide items")
    ic.add_argument("--csv", required=True, help="CSV path")
    ic.add_argument(
        "--match-on",
        choices=("item_id", "name"),
        default="name",
        help="Match rows to items by name (default) or item_id column",
    )
    ic.add_argument(
        "--skip-columns",
        default=None,
        help="Comma-separated extra columns to exclude from metadata",
    )
    ic.set_defaults(func=cmd_ingest_csv)

    s = sub.add_parser(
        "set-folder-filter-meta",
        help="Optional: PUT JSON under folder meta key (custom fork; upstream uses YAML file)",
    )
    s.add_argument("--folder-id", required=True)
    s.add_argument(
        "--meta-key",
        default="x-histomicsui-filter-config",
        help="Folder metadata key (default: x-histomicsui-filter-config)",
    )
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--keys", help="Comma-separated meta keys (builds JSON like large_image itemList)")
    g.add_argument("--json-file", help="Path to full JSON blob to store under --meta-key")
    s.set_defaults(func=cmd_set_folder_filter_meta)

    args = p.parse_args()
    if getattr(args, "skip_columns", None):
        args.skip_columns = [c.strip() for c in args.skip_columns.split(",") if c.strip()]
    client = connect(args.api_url, args.api_key, args.username, args.password)
    args.func(client, args)


if __name__ == "__main__":
    main()

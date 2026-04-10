#!/usr/bin/env python3
"""
Create and read large_image annotations (points, circles, polygons, etc.) on a DSA/Girder
server via the REST API.

Same paths HistomicsUI uses: annotations are stored in MongoDB by the
girder_large_image_annotation plugin.

  POST /api/v1/annotation?itemId=<slide item id>   + JSON body { name, elements, ... }
  GET  /api/v1/annotation?itemId=<slide item id>   (list summaries)
  GET  /api/v1/annotation/<annotation id>          (full document incl. elements)

Requires: pip install girder-client

Optional: pip install python-dotenv — then copy .env.example to .env and set
GIRDER_API_URL and GIRDER_API_KEY (see devops/dsa/utils/.env.example).

Examples
--------
  cp .env.example .env   # edit .env with your key
  python annotation_points.py list --item-id 507f1f77bcf86cd799439011

  # or without .env file:
  export GIRDER_API_KEY=...
  python annotation_points.py list --item-id 507f1f77bcf86cd799439011
  python annotation_points.py create --item-id ... --name "model_cells" --points points.json
  python annotation_points.py get --annotation-id ...
  python annotation_points.py extract-points --annotation-id ...   # points only
  python annotation_points.py elements --annotation-id ...         # circles, rects, polylines, ...
  python annotation_points.py elements --item-id ...               # all annotations on a slide
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any


def _load_dotenv() -> None:
    """Populate os.environ from .env if python-dotenv is installed."""
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

try:
    import girder_client
except ImportError:
    girder_client = None  # type: ignore


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


def load_points(path: str | None) -> list[list[float]]:
    """Load points as JSON: [[x,y,z], ...] or [{\"x\":..,\"y\":..,\"z\":0}, ...]."""
    raw = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError("Points JSON must be a list")
    out: list[list[float]] = []
    for row in data:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            z = float(row[2]) if len(row) > 2 else 0.0
            out.append([float(row[0]), float(row[1]), z])
        elif isinstance(row, dict) and "x" in row and "y" in row:
            z = float(row.get("z", 0))
            out.append([float(row["x"]), float(row["y"]), z])
        else:
            raise ValueError("Each point must be [x,y,z] or {x,y,z?}")
    return out


def _seg_len(p: list[float], q: list[float]) -> float:
    dx = p[0] - q[0]
    dy = p[1] - q[1]
    dz = (p[2] - q[2]) if len(p) > 2 and len(q) > 2 else 0.0
    return math.sqrt(dx * dx + dy * dy + dz * dz)


def summarize_element(el: dict[str, Any]) -> dict[str, Any]:
    """
    Reduce a large_image annotation element to geometry useful in Python.
    Coordinates are base-layer pixels (see large_image annotation schema).
    """
    t = el.get("type")
    out: dict[str, Any] = {
        "type": t,
    }
    if el.get("id"):
        out["id"] = el["id"]
    if el.get("group"):
        out["group"] = el["group"]
    lab = el.get("label")
    if isinstance(lab, dict) and lab.get("value"):
        out["label"] = lab["value"]

    if t == "point":
        out["center"] = el.get("center")
    elif t == "circle":
        center = el.get("center")
        r = float(el["radius"])
        out["center"] = center
        out["radius"] = r
        out["circumference"] = 2.0 * math.pi * r
        out["area"] = math.pi * r * r
    elif t == "ellipse":
        out["center"] = el.get("center")
        out["width"] = float(el.get("width", 0))
        out["height"] = float(el.get("height", 0))
        out["rotation_radians"] = float(el.get("rotation", 0))
    elif t == "rectangle":
        out["center"] = el.get("center")
        out["width"] = float(el.get("width", 0))
        out["height"] = float(el.get("height", 0))
        out["rotation_radians"] = float(el.get("rotation", 0))
    elif t in ("polyline", "arrow"):
        pts = el.get("points") or []
        out["points"] = pts
        if t == "polyline":
            out["closed"] = bool(el.get("closed", False))
        if len(pts) >= 2:
            perim = sum(_seg_len(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
            if t == "polyline" and out.get("closed") and len(pts) >= 3:
                perim += _seg_len(pts[-1], pts[0])
            out["polyline_length"] = perim
    else:
        # Heatmaps, overlays, etc.: pass through a safe subset
        out["details"] = {k: v for k, v in el.items() if k not in ("user",)}
    return out


def format_elements_text(payload: dict[str, Any] | list[Any]) -> str:
    """Human-readable summary for terminal use."""
    lines: list[str] = []

    def one_ann(ann: dict[str, Any]) -> None:
        name = ann.get("name") or ""
        aid = ann.get("_id", "")
        lines.append("Annotation %s  %s" % (aid, repr(name)))
        for i, g in enumerate(ann.get("elements") or []):
            t = g.get("type")
            if t == "circle":
                c = g.get("center")
                r = g.get("radius")
                cf = g.get("circumference")
                lines.append(
                    "  [%d] circle  center=%s  radius=%s  circumference=%s"
                    % (i, c, r, cf)
                )
            elif t == "point":
                lines.append("  [%d] point  center=%s" % (i, g.get("center")))
            elif t == "ellipse":
                lines.append(
                    "  [%d] ellipse  center=%s  width=%s  height=%s  rotation_rad=%s"
                    % (
                        i,
                        g.get("center"),
                        g.get("width"),
                        g.get("height"),
                        g.get("rotation_radians"),
                    )
                )
            elif t == "rectangle":
                lines.append(
                    "  [%d] rectangle  center=%s  width=%s  height=%s  rotation_rad=%s"
                    % (
                        i,
                        g.get("center"),
                        g.get("width"),
                        g.get("height"),
                        g.get("rotation_radians"),
                    )
                )
            elif t == "polyline":
                lines.append(
                    "  [%d] polyline  closed=%s  vertices=%d  length=%s"
                    % (
                        i,
                        g.get("closed"),
                        len(g.get("points") or []),
                        g.get("polyline_length"),
                    )
                )
            elif t == "arrow":
                lines.append(
                    "  [%d] arrow  endpoints=%d" % (i, len(g.get("points") or []))
                )
            else:
                lines.append("  [%d] %s  %s" % (i, t, json.dumps(g, default=str)))

    if isinstance(payload, list):
        for ann in payload:
            one_ann(ann)
            lines.append("")
    else:
        one_ann(payload)
    return "\n".join(lines).rstrip() + "\n"


def points_to_elements(
    points: list[list[float]],
    group: str | None,
    line_color: str,
    line_width: float,
) -> list[dict[str, Any]]:
    elements = []
    for x, y, z in points:
        el: dict[str, Any] = {
            "type": "point",
            "center": [x, y, z],
            "lineColor": line_color,
            "lineWidth": line_width,
        }
        if group:
            el["group"] = group
        elements.append(el)
    return elements


def cmd_list(client, args: argparse.Namespace) -> None:
    params: dict[str, Any] = {"itemId": args.item_id, "limit": args.limit, "offset": args.offset}
    rows = client.get("annotation", parameters=params)
    if not rows:
        print("[]")
        return
    slim = []
    for r in rows:
        slim.append(
            {
                "_id": r.get("_id"),
                "name": (r.get("annotation") or {}).get("name"),
                "itemId": r.get("itemId"),
                "_elementCount": r.get("_elementCount"),
            }
        )
    print(json.dumps(slim, indent=2))


def cmd_get(client, args: argparse.Namespace) -> None:
    r = client.get("annotation/%s" % args.annotation_id)
    print(json.dumps(r, indent=2))


def cmd_create(client, args: argparse.Namespace) -> None:
    points = load_points(args.points)
    body: dict[str, Any] = {
        "name": args.name,
        "elements": points_to_elements(
            points,
            group=args.group,
            line_color=args.line_color,
            line_width=args.line_width,
        ),
    }
    if args.description:
        body["description"] = args.description
    created = client.post(
        "annotation",
        parameters={"itemId": args.item_id},
        json=body,
    )
    print(json.dumps(created, indent=2))


def cmd_elements(client, args: argparse.Namespace) -> None:
    """Fetch full annotation(s) and print geometry summaries (circles, points, etc.)."""
    if args.annotation_id:
        ids = [args.annotation_id]
    else:
        rows = client.get(
            "annotation",
            parameters={"itemId": args.item_id, "limit": args.limit, "offset": 0},
        )
        ids = [r["_id"] for r in rows]
    results: list[dict[str, Any]] = []
    for aid in ids:
        r = client.get("annotation/%s" % aid)
        ann = r.get("annotation") or r
        elems = ann.get("elements") or []
        summarized = []
        for idx, el in enumerate(elems):
            s = summarize_element(el)
            s["element_index"] = idx
            summarized.append(s)
        results.append(
            {
                "_id": r.get("_id"),
                "itemId": r.get("itemId"),
                "name": ann.get("name"),
                "description": ann.get("description", ""),
                "elements": summarized,
            }
        )
    payload: dict[str, Any] | list[dict[str, Any]]
    if len(results) == 1:
        payload = results[0]
    else:
        payload = results
    if args.format == "text":
        sys.stdout.write(format_elements_text(payload))
    else:
        print(json.dumps(payload, indent=2))


def cmd_extract_points(client, args: argparse.Namespace) -> None:
    r = client.get("annotation/%s" % args.annotation_id)
    ann = r.get("annotation") or r
    elements = ann.get("elements") or []
    centers = []
    for el in elements:
        if el.get("type") == "point" and "center" in el:
            centers.append(el["center"])
    if args.as_json:
        print(json.dumps(centers, indent=2))
    else:
        for c in centers:
            print("%s\t%s\t%s" % (c[0], c[1], c[2]))


def main() -> None:
    _load_dotenv()
    p = argparse.ArgumentParser(description="Create/read point annotations on DSA/Girder.")
    p.add_argument(
        "--api-url",
        default=os.environ.get("GIRDER_API_URL", "http://localhost:8080/api/v1"),
        help="Girder API base (default: %(default)s or GIRDER_API_URL)",
    )
    p.add_argument("--api-key", default=os.environ.get("GIRDER_API_KEY"), help="API key (or GIRDER_API_KEY)")
    p.add_argument("--username", help="Girder login (if not using API key)")
    p.add_argument("--password", help="Password (if not using API key)")

    sub = p.add_subparsers(dest="command", required=True)

    lp = sub.add_parser("list", help="List annotation summaries for a slide item")
    lp.add_argument("--item-id", required=True, help="Girder item id of the WSI (large image)")
    lp.add_argument("--limit", type=int, default=100)
    lp.add_argument("--offset", type=int, default=0)
    lp.set_defaults(func=cmd_list)

    gp = sub.add_parser("get", help="Download one full annotation JSON (includes elements)")
    gp.add_argument("--annotation-id", required=True)
    gp.set_defaults(func=cmd_get)

    cp = sub.add_parser("create", help="POST a new annotation with point elements")
    cp.add_argument("--item-id", required=True, help="Girder item id of the WSI")
    cp.add_argument("--name", required=True, help="Annotation name")
    cp.add_argument("--description", default="", help="Optional description")
    cp.add_argument(
        "--points",
        required=True,
        help='JSON file of points, or "-" for stdin. Format: [[x,y,z], ...]',
    )
    cp.add_argument("--group", default=None, help="Optional Histomics group name")
    cp.add_argument("--line-color", default="#FF0000")
    cp.add_argument("--line-width", type=float, default=2.0)
    cp.set_defaults(func=cmd_create)

    ep = sub.add_parser(
        "extract-points",
        help="Print coordinates of point elements from an annotation id",
    )
    ep.add_argument("--annotation-id", required=True)
    ep.add_argument("--as-json", action="store_true", help="Print JSON array of [x,y,z]")
    ep.set_defaults(func=cmd_extract_points)

    dp = sub.add_parser(
        "elements",
        help="Geometry summary: centers, radii, circumferences (circles), polylines, etc.",
    )
    dg = dp.add_mutually_exclusive_group(required=True)
    dg.add_argument(
        "--annotation-id",
        help="One annotation document id (from list command)",
    )
    dg.add_argument(
        "--item-id",
        help="Fetch all annotations on this slide item and summarize each",
    )
    dp.add_argument(
        "--format",
        choices=("json", "text"),
        default="json",
        help="json (default) or text (readable lines for circles and other shapes)",
    )
    dp.add_argument(
        "--limit",
        type=int,
        default=200,
        help="With --item-id, max annotations to fetch (default: %(default)s)",
    )
    dp.set_defaults(func=cmd_elements)

    args = p.parse_args()
    client = connect(args.api_url, args.api_key, args.username, args.password)
    args.func(client, args)


if __name__ == "__main__":
    main()

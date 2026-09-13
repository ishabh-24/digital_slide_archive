# DSA Annotation Format — v1 (draft)

Status: **draft, team decisions applied**. Nothing in the platform enforces this yet.

One file holds the annotations for **one whole-slide image**. The format is a
constrained profile of GeoJSON (RFC 7946): every valid file is ordinary GeoJSON,
but not every GeoJSON file is valid here. Bulk uploads pair files with slides
through a manifest, as `utils/ingest_annotations.py` does today.

- Schema: [`dsa-annotation-v1.schema.json`](../dsa_csv_plugin/dsa_csv_plugin/schemas/dsa-annotation-v1.schema.json) (inside the plugin package so the upload route can load it)
- Validator: [`annotation_format.py`](../dsa_csv_plugin/dsa_csv_plugin/annotation_format.py); command line: [`utils/validate_annotation.py`](../utils/validate_annotation.py)
- Converter: [`utils/convert_annotations.py`](../utils/convert_annotations.py)
- Example: [`example-beetle-patient104_wsi1.json`](example-beetle-patient104_wsi1.json)

## Minimal file

```json
{
  "type": "FeatureCollection",
  "properties": {
    "format": "dsa-annotation",
    "version": "1.0",
    "coordinate_space": "level0_pixels",
    "slide": { "name": "patient104_wsi1.tif" },
    "classes": { "other": {}, "lymphocyte": { "color": "#9600C8" } }
  },
  "features": [
    {
      "type": "Feature",
      "geometry": { "type": "Polygon",
                    "coordinates": [[[100, 100], [400, 100], [400, 300], [100, 100]]] },
      "properties": { "class": "other", "label": "region 0" }
    },
    {
      "type": "Feature",
      "geometry": { "type": "Point", "coordinates": [250, 180] },
      "properties": { "class": "lymphocyte", "attributes": { "confidence": 0.93 } }
    }
  ]
}
```

## Collection `properties`

| Field | Required | Rule |
|---|---|---|
| `format` | yes | Exactly `"dsa-annotation"`. |
| `version` | yes | Exactly `"1.0"`. |
| `coordinate_space` | yes | Exactly `"level0_pixels"`: full-resolution pixels, origin top-left, x right, y down. Downsampled or micron coordinates must be converted before upload. |
| `slide.name` | yes | Filename of the target slide, with extension. |
| `slide.sizeX`, `slide.sizeY` | no | Level-0 width/height. If present, must equal the target item's. |
| `classes` | yes | Object mapping each class name (1–100 chars) to `{ "color"?: "#RRGGBB", "description"?: string }`. Every name must be in the collection's vocabulary. |
| `provenance` | no | `source_format`, `source_file`, `converter`, `converted_at` (ISO 8601). |

No other keys are allowed at any level, so typos fail loudly instead of being ignored.

## Features

| Field | Required | Rule |
|---|---|---|
| `geometry` | yes | `Polygon` or `MultiPolygon` for regions; `Point` for cell detections. |
| `properties.class` | yes | A string that is a key of `classes`. The only place a class can be given. |
| `properties.label` | no | Per-region text, ≤ 200 chars. |
| `properties.attributes` | no | Free-form object (annotator, confidence, …), passed through. |
| `id` | no | String or integer. |

Positions are `[x, y]` — two non-negative numbers, no z. Polygon rings are closed
(last vertex repeats the first) with ≥ 3 distinct vertices.

### Holes

The first ring of a polygon is its outer boundary; further rings are holes. The
deployed large_image annotation schema supports this directly (`polyline.holes`,
confirmed on both the `girder-3` branch the Dockerfile checks out and on master).
large_image requires holes to lie **inside** the outer ring and **not cross each
other**, so ingest checks containment (`E-HOLE`). Crossing between holes is not
checked in v1 because it is expensive on dense geometry.

### Cell detections

Each detected cell is one `Point` feature, which keeps per-cell `attributes` such
as confidence. Cell classes (e.g. `lymphocyte`, `tumor cell`) belong to the
collection vocabulary like any other class, and each becomes its own layer.

Cell files are orders of magnitude denser than region files: a region file is
typically hundreds of features, a cell file can be hundreds of thousands. Before
committing to per-cell points at full dataset scale, ingest and open **one dense
slide** to confirm upload time and HistomicsUI rendering are acceptable.

## Class vocabulary (per collection)

Each collection defines its allowed classes once, so layers mean the same thing on
every slide in it. The vocabulary is stored as collection metadata:

```json
"annotationClasses": {
  "invasive epithelium": { "color": "#FF0000" },
  "non-invasive epithelium": {},
  "necrosis": {},
  "other": {},
  "lymphocyte": { "color": "#9600C8" }
}
```

- A class in the file that is not in the vocabulary is an error (`E-VOCAB`). Names
  are compared exactly; a case-only or whitespace-only difference gets a suggestion
  in the message.
- A collection with no vocabulary yet accepts the upload with a warning
  (`W-NOVOCAB`), so testing isn't blocked before an admin defines one.

### Colors

A class color is resolved in this order:

1. the color in the collection vocabulary,
2. the color in the file's `classes` table,
3. the **default palette**: black `#000000` and white `#FFFFFF`, alternating in
   vocabulary order (first class black, second white, third black, …).

Because the vocabulary order is fixed per collection, a class gets the same default
color on every slide.

## Validation

Ingest runs two layers and reports **every** failure, not just the first.

**1. Schema** (structure, types, allowed values) — `dsa-annotation-v1.schema.json`.

**2. Ingest checks** (need cross-field or server-side knowledge the schema can't express):

| Code | Check | Severity | Example message |
|---|---|---|---|
| `E-CLASS` | Every feature's class is declared in `classes` | error | `feature 14: class "tumour" is not declared in properties.classes` |
| `E-VOCAB` | Every class in `classes` is in the collection vocabulary | error | `class "Necrosis" is not in the BEETLE vocabulary (did you mean "necrosis"?)` |
| `E-RING` | Every ring is closed | error | `feature 3, ring 0: last vertex [120, 88] does not repeat first vertex [100, 80]` |
| `E-DEGEN` | Every ring has ≥ 3 distinct vertices | error | `feature 9, ring 0: only 2 distinct vertices` |
| `E-HOLE` | Every hole lies inside its outer ring | error | `feature 5, ring 2: hole extends outside the outer boundary` |
| `E-BOUNDS` | All coordinates fall inside the target image | error | `feature 0: y = 412903 exceeds image height 112000 (3.7×). Coordinates are probably not level-0 pixels.` |
| `E-SIZE` | `slide.sizeX/sizeY`, if given, match the item | error | `slide.sizeX is 98000 but item is 49000 wide` |
| `W-SLIDE` | `slide.name` equals the target item's name | warning | `file names "patient1_wsi1.tif" but the target item is "patient2_wsi1.tif"` |
| `W-NOVOCAB` | The collection has a class vocabulary | warning | `collection "BEETLE" has no class vocabulary; classes were not checked` |
| `W-UNUSED` | Declared class has no features | warning | `class "necrosis" is declared but unused` |

Warnings never block the upload; they are listed with the result. `E-BOUNDS` is
what catches mis-scaled coordinates, which otherwise upload silently and render
off-image. `W-SLIDE` flags a file aimed at the wrong item, such as BEETLE's mask
TIFFs, which share filenames with the slides.

## How a file becomes HistomicsUI annotations

| File | DSA annotation |
|---|---|
| Each class with ≥ 1 feature | One annotation document named `<slide stem> - <class>` (one toggleable layer) |
| `Polygon` | `polyline` element, `closed: true`; rings after the first → element `holes` |
| `MultiPolygon` | One `polyline` element per part |
| `Point` | `point` element, `center: [x, y, 0]` |
| resolved class color | `lineColor` = `rgb(...)`, `fillColor` = same at 0.25 alpha |
| `label` | element `label.value` |
| `attributes` | element `user` |
| `provenance` | annotation `attributes.provenance` |

Uploading the same file twice replaces layers of the same name only when
"Replace existing" is ticked; otherwise existing layers are kept and reported as skipped.

## Converting existing data

Converters output this format and validate the result before writing it, so a
converted file always passes the schema layer. Lenient format guessing lives only here.

| Source | Adapter | State |
|---|---|---|
| BEETLE JSON (`[{coordinates, label:{name,value}}]`) | `beetle` | Logic exists in `utils/ingest_annotations.py`; re-target output |
| BCNB JSON (`{class: [{vertices}]}`) | `bcnb` | Logic exists; re-target output |
| QuPath GeoJSON export (regions and cell detections) | `qupath-geojson` | Small: move `classification.name` into `class`, build `classes` |
| ASAP XML (BEETLE `annotations/xmls/`, CAMELYON) | `asap-xml` | New |
| Raster label masks | `mask` | New, hardest: contour tracing plus scaling from mask µm/px to level 0 |
| Existing DSA annotations | export | New; enables round-trip tests |

## Decisions

| Question | Decision |
|---|---|
| Holes | Allowed. large_image supports them; ingest checks containment (`E-HOLE`). |
| Cell detections | In scope for v1, as `Point` features. Load-test one dense slide first. |
| Slide name mismatch | Warning (`W-SLIDE`). |
| Colors | Optional. Default palette black/white, alternating in vocabulary order. |
| Class vocabulary | Fixed per collection, stored as collection metadata. |
| One slide per file | Kept; bulk uploads go through manifests. |

## Still open

1. **Confirm holes on the running server**, not just in source: the image's
   large_image layer is cached from whenever it was first built.
2. **Where admins edit the vocabulary**: a small page like `/csv_upload`, or set
   through the API only for now.
3. **Default palette with more than two classes**: black/white alternation means
   the third class looks like the first. Acceptable, or should vocabulary colors be
   required once a collection has three or more classes?

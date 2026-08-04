<p align="center">
  <img src="dog-e.png" alt="PAW" width="500">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License: Apache 2.0">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/version-0.2.0-orange.svg" alt="Version 0.2.0">
</p>

# PAW — PDF Architecture Wizard

A layout-surgery engine for PDFs on a fully permissive stack (pypdfium2 · pikepdf ·
reportlab · pdfplumber · Pillow · fontTools). Read a page's text, vector art, and images with coordinates;
erase text and art out of the content stream; draw replacements; subset and save.

**Status: v0.2.0.** Installable from source; live coverage suite; glyph-precision text
erasure (simple and CID fonts); per-glyph font fallback; pdfium thread-lock.

## Install

```bash
pip install .
```

Python 3.11+. Fonts are not bundled — supply your own (see NOTICE).

## Usage

```python
import paw

doc  = paw.open("form.pdf")
page = doc.pages[0]

# read
page.text_spans()              # text with font, size, colour, bbox
page.strokes(); page.fills()   # vector art (gridlines, highlight bars)
page.images()                  # placed images with bboxes
page.raster(dpi=200)           # a PIL image of the page
page.scene_objects()           # stable paint-order objects, including Form children
page.raster_layers(dpi=150)    # composite + diagnostic render without top-level text

# write — region is (x0, y0, x1, y1) in points, origin top-left, y down
region = (72, 100, 300, 130)
page.erase_text(region)        # text out of the content stream
page.erase_art(region)         # covered rules and highlights out
page.draw_text("Hello", at=(72, 118), font=paw.Font.load("Noto.ttf"), size=12)
page.draw_box(region)
page.place_image(pil_image, region)

doc.save("form.out.pdf")
```

Coordinates are points, origin top-left, y growing down; `draw_text(at=…)` positions the
baseline start.

## API

| Call                                                                       | Does                                         |
| -------------------------------------------------------------------------- | -------------------------------------------- |
| `paw.open` · `paw.blank` · `doc.save` · `doc.subset`                       | open · create · write · keep only some pages |
| `page.text_spans` · `page.text_lines`                                      | text with font, size, colour, bbox           |
| `page.strokes` · `page.fills` · `page.images` · `page.links`               | vector art · fills · images · links          |
| `page.raster(dpi)`                                                         | render the page to a PIL image               |
| `page.scene_objects`                                                       | painted objects · Form ancestry · RGBA · IDs |
| `page.raster_layers(dpi)`                                                  | composite · text-suppressed diagnostic render |
| `page.erase_text` · `page.erase_art`                                       | remove text · path objects inside a region   |
| `page.draw_text` · `page.draw_box` · `page.draw_rule` · `page.place_image` | draw onto the page                           |
| `paw.Font.load`                                                            | load a TTF for measuring and drawing         |

## Coverage

```bash
python tests/coverage_check.py
```

Exercises every public call against a synthetic document and prints the status report.

## Licences

Third-party component licences are listed in NOTICE.

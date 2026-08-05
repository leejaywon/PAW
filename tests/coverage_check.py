#!/usr/bin/env python3
"""PAW coverage check — exercises every public API against a synthetic PDF.

The fixture is drawn with reportlab in its NATIVE bottom-up frame on purpose:
if PAW's top-left-y-down answers agree with what was drawn, the boundary
normalisation is right, not just self-consistent. Fixture page 1 (300×200 pt),
bottom-up coords as drawn → PAW coords:

    "KEEP TOP"        baseline y=160  → PAW y ≈  40   (kept)
    "ERASE ME PLEASE" baseline y=100  → PAW y ≈ 100   (inside region, erased)
    "KEEP BOTTOM"     baseline y= 40  → PAW y ≈ 160   (kept)
    inside rule       (30,95)–(110,95) → PAW y = 105  (inside region, erased)
    straddle rule     (10,90)–(290,90) → PAW y = 110  (x exceeds region, kept)
    filled swatch     rect(200,150,40,20)            (outside region)

Region erased = REGION below; page 2 exists for subset().
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import paw  # noqa: E402

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)
GONOTO = Path.home() / ".cache/babeldoc/fonts/GoNotoKurrent-Regular.ttf"
REGION = (15, 80, 160, 118)

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))


def fixture() -> bytes:
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(300, 200))
    c.setFont("Helvetica", 12)
    c.drawString(30, 160, "KEEP TOP")
    c.setFillColorRGB(0.2, 0.7, 0.3)
    c.drawString(30, 130, "COLOR LEFT")
    split_x = 30 + c.stringWidth("COLOR LEFT", "Helvetica", 12)
    c.setFillColorRGB(0.9, 0.2, 0.1)
    c.drawString(split_x, 130, " COLOR RIGHT")
    c.setFillColorRGB(0, 0, 0)
    c.drawString(30, 100, "ERASE ME PLEASE")
    c.drawString(30, 40, "KEEP BOTTOM")
    c.line(30, 95, 110, 95)
    c.line(10, 90, 290, 90)
    c.setFillColorRGB(1, 0.8, 0)
    c.rect(200, 150, 40, 20, stroke=0, fill=1)
    c.showPage()
    c.setFont("Helvetica", 12)
    c.drawString(30, 100, "PAGE TWO")
    c.beginForm("ScaledText", 0, 0, 100, 30)
    c.setFont("Helvetica", 12)
    c.drawString(10, 10, "FORM CHILD")
    c.endForm()
    c.saveState()
    c.translate(80, 60)
    c.scale(0.5, 0.5)
    c.doForm("ScaledText")
    c.restoreState()
    c.save()
    return buf.getvalue()


def main() -> int:
    data = fixture()

    doc = paw.open(data)
    check("open()", doc.page_count == 2, f"pages={doc.page_count}")
    page = doc.pages[0]
    w, h = page.size
    check("page.size", (round(w), round(h)) == (300, 200), f"{w:.0f}×{h:.0f}")

    spans = page.text_spans()
    texts = [s.text for s in spans]
    erase_span = next((s for s in spans if "ERASE" in s.text), None)
    check("text_spans()", erase_span is not None and len(spans) >= 3,
          f"{len(spans)} spans: {texts}")
    ok_geom = (erase_span is not None
               and 80 < erase_span.bbox[1] < erase_span.bbox[3] < 118
               and erase_span.font.endswith("Helvetica")
               and abs(erase_span.size - 12) < 0.5)
    check("  span geometry+style", ok_geom,
          f"bbox={tuple(round(v, 1) for v in erase_span.bbox)} "
          f"font={erase_span.font} size={erase_span.size}" if erase_span else "no span")

    colour_spans = [s for s in spans if "COLOR" in s.text or "RIGHT" in s.text]
    colour_lines = [ln for ln in page.text_lines()
                    if "COLOR LEFT COLOR RIGHT" in "".join(s.text for s in ln["spans"])]
    check("  text_spans preserves colour runs",
          len(colour_spans) == 2 and colour_spans[0].color != colour_spans[1].color,
          f"{[(s.text, s.color) for s in colour_spans]}")
    check("  text_lines preserves inline colour runs",
          len(colour_lines) == 1 and len(colour_lines[0]["spans"]) == 2
          and colour_lines[0]["spans"][0].color != colour_lines[0]["spans"][1].color,
          f"{[(s.text, s.color) for s in colour_lines[0]['spans']] if colour_lines else []}")

    st0 = page.strokes()
    check("strokes()", len(st0) == 2,
          f"{len(st0)} — y={[round(s['y0'], 1) for s in st0]} (want 105, 110)")
    fl = page.fills()
    check("fills()", len(fl) >= 1, f"{len(fl)} — first={fl[0] if fl else None}")

    scene = page.scene_objects()
    scene_again = page.scene_objects()
    positioned = page.positioned_text_objects()
    yellow = next((o for o in scene if o.kind == "path"
                   and o.fill_rgba is not None and o.fill_rgba[0] > 240
                   and 180 < o.fill_rgba[1] < 230), None)
    check("scene_objects()",
          any(o.kind == "text" and "ERASE" in (o.text or "") for o in scene)
          and yellow is not None
          and [o.id for o in scene] == [o.id for o in scene_again],
          f"{len(scene)} objects · ids={[o.id for o in scene]}")
    check("  scene paint colour+geometry",
          yellow is not None and yellow.paint_order == (len(scene) - 1,)
          and 29 < yellow.bbox[1] < 31 and yellow.fill_rgba[3] == 255,
          f"{yellow}")
    check("positioned_text_objects() is unmerged paint order",
          [o.id for o in positioned] == [o.id for o in scene if o.kind == "text"]
          and [o.text for o in positioned] == ["KEEP TOP", "COLOR LEFT ",
                                                " COLOR RIGHT", "ERASE ME PLEASE",
                                                "KEEP BOTTOM"],
          f"{[(o.id, o.text) for o in positioned]}")
    top = positioned[0] if positioned else None
    check("  effective page matrix+font size",
          top is not None and top.matrix is not None
          and top.effective_matrix is not None
          and abs((top.effective_font_size or 0) - 12) < 0.01
          and abs(top.effective_matrix[4] - 30) < 0.01
          and abs(top.effective_matrix[5] - 40) < 0.01,
          f"{top}")
    form_child = next((o for o in doc.pages[1].positioned_text_objects()
                       if o.text == "FORM CHILD"), None)
    check("  nested Form matrix composes into page space",
          form_child is not None and bool(form_child.parent_ids)
          and abs((form_child.effective_font_size or 0) - 6) < 0.01
          and form_child.effective_matrix is not None
          and abs(form_child.effective_matrix[0] - 0.5) < 0.01
          and abs(form_child.effective_matrix[3] + 0.5) < 0.01,
          f"{form_child}")

    layers = page.raster_layers(dpi=144, crisp=True)
    from PIL import ImageChops
    check("raster_layers()",
          layers.removed_text_objects == 5 and layers.unreachable_text_objects == 0
          and ImageChops.difference(layers.composite, layers.without_text).getbbox() is not None,
          f"removed={layers.removed_text_objects} unreachable={layers.unreachable_text_objects}")

    img = page.raster(dpi=144)
    check("raster(dpi=144)", img.size == (600, 400), f"{img.size}")

    if not GONOTO.exists():
        check("Font.load(GoNoto)", False, f"font missing: {GONOTO}")
        return report()
    font = paw.Font.load(GONOTO)
    w_ko = font.width("사업 신청 안내", 12)
    check("Font.load / width()", w_ko > 40, f"width('사업 신청 안내',12)={w_ko:.1f}pt")

    r = page.erase_text(REGION)
    after = [s.text for s in page.text_spans()]
    check("erase_text(region)",
          r["removed"] >= 1 and not any("ERASE" in t for t in after)
          and any("KEEP TOP" in t for t in after)
          and any("KEEP BOTTOM" in t for t in after),
          f"{r} · spans now {after}")
    check("  art survives erase_text", len(page.strokes()) == 2,
          f"{len(page.strokes())} strokes (want 2)")

    r2 = page.erase_art(REGION)
    st1 = page.strokes()
    survived = [round(s["y0"], 1) for s in st1]
    check("erase_art(region)",
          r2["removed"] == 1 and len(st1) == 1 and 109 < st1[0]["y0"] < 111,
          f"{r2} · surviving rule y={survived} (want [110])")
    check("  text survives erase_art",
          any("KEEP TOP" in s.text for s in page.text_spans()), "")

    page.draw_text("사업 신청 안내 Application", at=(30, 100), font=font, size=12,
                   color=(0.8, 0, 0))
    page.draw_box((20, 78, 165, 120), stroke=(0, 0, 0.8), width=0.7)
    page.draw_rule((30, 116), (150, 116), width=0.5, color=(0, 0.5, 0))
    from PIL import Image
    page.place_image(Image.new("RGB", (24, 24), (200, 30, 30)), (250, 20, 286, 56))

    out_pdf = OUT / "coverage.out.pdf"
    doc.save(out_pdf)

    doc2 = paw.open(out_pdf)
    p2 = doc2.pages[0]
    spans2 = p2.text_spans()
    ko = next((s for s in spans2 if "사업" in s.text), None)
    check("draw_text → Korean survives round-trip", ko is not None,
          f"spans={[s.text for s in spans2]}")
    check("  drawn at the asked position",
          ko is not None and abs(ko.bbox[0] - 30) < 2 and 88 < ko.bbox[1] < 102,
          f"bbox={tuple(round(v, 1) for v in ko.bbox)}" if ko else "")
    ims = p2.images()
    check("place_image → images()", len(ims) == 1 and abs(ims[0]["x0"] - 250) < 2
          and abs(ims[0]["y0"] - 20) < 2,
          f"{[{k: round(v, 1) if isinstance(v, float) else v for k, v in im.items()} for im in ims]}")
    nested = p2.scene_objects()
    positioned_nested = p2.positioned_text_objects()
    nested_ko = next((o for o in nested if o.kind == "text" and "사업" in (o.text or "")), None)
    check("scene_objects() Form children",
          nested_ko is not None and bool(nested_ko.parent_ids)
          and abs(nested_ko.bbox[0] - 30) < 2 and 88 < nested_ko.bbox[1] < 102,
          f"{nested_ko}")
    check("  Form effective matrix+font size",
          nested_ko is not None and nested_ko.effective_matrix is not None
          and nested_ko.effective_font_size is not None
          and 10 < nested_ko.effective_font_size < 14
          and any(o.id == nested_ko.id for o in positioned_nested),
          f"{nested_ko}")
    p2.raster(dpi=150).save(OUT / "coverage.out.png")
    check("purity: no ERASE text anywhere",
          not any("ERASE" in s.text for s in spans2), "")

    doc3 = paw.open(data)
    doc3.subset([0])
    check("subset([0])", doc3.page_count == 1, f"pages={doc3.page_count}")

    import pikepdf
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(300, 200))
    pg_ = pdf.pages[0]
    AFM = {"L": 556, "E": 667, "F": 611, "T": 611, "M": 833, "I": 278,
           "D": 722, "R": 722, "G": 778, "H": 722, " ": 278}
    fnt = pdf.make_indirect(pikepdf.Dictionary({
        "/Type": pikepdf.Name("/Font"), "/Subtype": pikepdf.Name("/Type1"),
        "/BaseFont": pikepdf.Name("/Helvetica"), "/FirstChar": 32, "/LastChar": 90,
        "/Widths": [AFM.get(chr(c), 500) for c in range(32, 91)]}))
    pg_.Resources = pikepdf.Dictionary({"/Font": pikepdf.Dictionary({"/F1": fnt})})
    pg_.Contents = pdf.make_stream(
        b"BT /F1 16 Tf 1 0 0 1 20 100 Tm [(LEFT) -300 (MIDDLE) -300 (RIGHT)] TJ ET\n"
        b"0 0 0 RG 1 w 20 80 m 200 80 l S")
    tjbuf = io.BytesIO()
    pdf.save(tjbuf)
    d4 = paw.open(tjbuf.getvalue())
    r4 = d4.pages[0].erase_text((63, 82, 126, 102))
    a4 = {sp.text: round(sp.bbox[0], 1) for sp in d4.pages[0].text_spans()}
    check("erase_text Path B (straddling TJ)",
          r4.get("glyphs") == 6 and a4 == {"LEFT": 20.0, "RIGHT": 129.2}
          and len(d4.pages[0].strokes()) == 1,
          f"{r4} · spans {a4}")

    fb_path = Path("/System/Library/Fonts/Supplemental/AppleGothic.ttf")
    if fb_path.exists():
        fb = paw.Font.load(fb_path)
        d5 = paw.blank(300, 100)
        d5.pages[0].draw_text("사업 ○ 신청 ●", at=(20, 55), font=font, size=14,
                              fallbacks=[fb])
        d5x = paw.open(d5.tobytes())
        t5 = "".join(sp.text for sp in d5x.pages[0].text_spans())
        check("draw_text fallbacks (per-glyph routing)",
              "○" in t5 and "●" in t5 and "사업" in t5, repr(t5))
    else:
        check("draw_text fallbacks (per-glyph routing)", True,
              "skipped — no dev fallback font on this machine")

    return report()


def report() -> int:
    wname = max(len(n) for n, _, _ in results)
    fails = 0
    print(f"\nPAW coverage — {len(results)} checks\n" + "─" * (wname + 50))
    for name, ok, note in results:
        mark = "✓" if ok else "✗ FAIL"
        fails += 0 if ok else 1
        print(f"  {name:<{wname}}  {mark:<7} {note}")
    print("─" * (wname + 50))
    print(f"  {len(results) - fails}/{len(results)} passed"
          + (f" · artifacts in {OUT}/" if fails == 0 else ""))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())

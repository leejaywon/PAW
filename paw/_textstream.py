"""Glyph-precision text erasure — Path B of `Page.erase_text`.

The object-level pass (Path A, pdfium) removes text objects that lie FULLY inside a region.
One show operator can straddle the boundary though —
`[(LEFT) -300 (MIDDLE) -300 (RIGHT)] TJ` is a single object, and only MIDDLE should die.
This module walks the content stream,
computes each glyph's device box from the spec's composition (ISO 32000-1 §9.4.4):

    Trm = [Tfs·Th 0, 0 Tfs, 0 Trise] × Tm × CTM
    adv = ((w0 − Tj/1000)·Tfs + Tc + Tw)·Th

and rewrites show operators so that erased glyphs become KERNS of the same advance.
Two position-safety rules:

  * a partially-erased operator keeps its survivors in place to 2 dp
    by substituting each dropped glyph's advance as a TJ kern number;
  * a FULLY-erased operator is not dropped but replaced by a kern-only TJ —
    dropping it would shift any later show op in the same line leftward.

Word spacing (`Tw`) applies to the SINGLE-BYTE code 32 only —
never to byte 32 inside a multi-byte CID code (§9.3.3).
Korean Identity-H text therefore takes no Tw at all;
getting this wrong drifts every glyph box on a CJK page.

Coverage limits, reported rather than hidden:
text inside Form XObjects is not walked (counted in `xobject_ops`),
and Type3 fonts fall back to default widths.
"""
from __future__ import annotations

import pikepdf
from pikepdf import Array, ContentStreamInstruction, Operator, String

_ID = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _concat(a, b):
    return (a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
            a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5])


def _apply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


class _FontInfo:
    """Per-glyph advance widths for one /Font resource.

    Two shapes matter here:
    simple fonts (1-byte codes, /Widths array)
    and Type0/CID fonts (2-byte codes under Identity encoding, /W ranges + /DW default) —
    the latter is what Korean text actually uses.
    """

    __slots__ = ("cid", "widths", "default")

    def __init__(self, fontdict):
        self.cid = False
        self.widths: dict[int, float] = {}
        self.default = 0.5
        try:
            if str(fontdict.get("/Subtype", "")) == "/Type0":
                self.cid = True
                desc = fontdict["/DescendantFonts"][0]
                self.default = float(desc.get("/DW", 1000)) / 1000.0
                self._parse_w(desc.get("/W"))
            else:
                fc = int(fontdict.get("/FirstChar", 0))
                arr = fontdict.get("/Widths")
                if arr is not None:
                    for i, v in enumerate(arr):
                        self.widths[fc + i] = float(v) / 1000.0
                fd = fontdict.get("/FontDescriptor")
                if fd is not None and "/MissingWidth" in fd:
                    self.default = float(fd["/MissingWidth"]) / 1000.0
        except Exception:      # noqa: BLE001 — a broken font dict falls back to defaults
            pass

    def _parse_w(self, w) -> None:
        """CID /W: `c [w…]` lists and `c1 c2 w` ranges, freely mixed."""
        if w is None:
            return
        items = list(w)
        i = 0
        while i < len(items):
            c = int(items[i])
            if i + 1 < len(items) and isinstance(items[i + 1], pikepdf.Array):
                for j, v in enumerate(items[i + 1]):
                    self.widths[c + j] = float(v) / 1000.0
                i += 2
            elif i + 2 < len(items):
                c2, v = int(items[i + 1]), float(items[i + 2]) / 1000.0
                for code in range(c, min(c2, c + 65535) + 1):
                    self.widths[code] = v
                i += 3
            else:
                break

    def codes(self, raw: bytes):
        """→ (code, is_single_byte_space) per glyph, honouring code width."""
        if self.cid:
            for k in range(0, len(raw) - 1, 2):
                yield (raw[k] << 8) | raw[k + 1], False
        else:
            for b in raw:
                yield b, b == 32

    def advance(self, code: int, is_space: bool, Tfs, Tc, Tw, Th) -> float:
        w0 = self.widths.get(code, self.default)
        return (w0 * Tfs + Tc + (Tw if is_space else 0.0)) * Th


def uses_fragile_color(pdf_bytes: bytes, page_index: int) -> bool:
    """Whether this page paints in colour that pdfium's content regeneration would not round-trip.

    FPDFPage_GenerateContent re-encodes colour through pdfium's own model, so anything outside
    DeviceRGB/DeviceGray — CMYK, Separation/DeviceN, shadings, pattern fills — comes back solid black.
    A page answering True must not be edited through object-level removal, whatever the edit is worth.
    """
    import io

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        page = pdf.pages[page_index]
        for ins in pikepdf.parse_content_stream(page):
            if isinstance(ins, pikepdf.ContentStreamInlineImage):
                continue
            op, o = str(ins.operator), ins.operands
            if op in ("k", "K", "sh"):
                return True
            if op in ("cs", "CS") and o and str(o[0]) not in ("/DeviceRGB", "/DeviceGray"):
                return True
            if op in ("scn", "SCN") and o and isinstance(o[-1], pikepdf.Name):
                return True
    return False


def _is_form_xobject(page, operands) -> bool:
    """Whether a `Do` names a Form XObject — the one place glyphs hide from this walker."""
    try:
        return str(page.Resources.XObject[str(operands[0])].Subtype) == "/Form"
    except Exception:      # noqa: BLE001 — a name we cannot resolve is not a form we could have walked
        return False


def erase_glyphs(pdf_bytes: bytes, page_index: int,
                 regions: list[tuple]) -> tuple[bytes, dict]:
    """Erase every glyph whose device box intersects any region (bottom-up device space).
    → (new_bytes, {"ops_rewritten", "glyphs", "xobject_ops"}).
    """
    import io

    stats = {"ops_rewritten": 0, "glyphs": 0, "xobject_ops": 0,
             "form_xobject_ops": 0, "fragile_color": False}
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        page = pdf.pages[page_index]
        fonts: dict[str, _FontInfo] = {}
        try:
            for k, v in page.Resources.Font.items():
                fonts[str(k)] = _FontInfo(v)
        except Exception:      # noqa: BLE001 — no fonts, nothing to erase
            pass

        ctm, stack = _ID, []
        Tm = Tlm = _ID
        Tf, Tfs, Tc, Tw, Th, TL, Trise = None, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0
        out = []

        def hit(box) -> bool:
            return any(not (box[2] < r[0] or box[0] > r[2]
                            or box[3] < r[1] or box[1] > r[3]) for r in regions)

        for ins in pikepdf.parse_content_stream(page):
            if isinstance(ins, pikepdf.ContentStreamInlineImage):
                out.append(ins)
                continue
            op, o = str(ins.operator), ins.operands
            if op == "q":
                stack.append(ctm)
            elif op == "Q" and stack:
                ctm = stack.pop()
            elif op == "cm":
                ctm = _concat(tuple(float(x) for x in o), ctm)
            elif op == "BT":
                Tm = Tlm = _ID
            elif op == "Tf" and len(o) == 2:
                Tf, Tfs = str(o[0]), float(o[1])
            elif op == "Tc":
                Tc = float(o[0])
            elif op == "Tw":
                Tw = float(o[0])
            elif op == "Tz":
                Th = float(o[0]) / 100.0
            elif op == "TL":
                TL = float(o[0])
            elif op == "Ts":
                Trise = float(o[0])
            elif op == "Tm":
                Tm = Tlm = tuple(float(x) for x in o)
            elif op in ("Td", "TD"):
                if op == "TD":
                    TL = -float(o[1])
                Tlm = _concat((1, 0, 0, 1, float(o[0]), float(o[1])), Tlm)
                Tm = Tlm
            elif op == "T*":
                Tlm = _concat((1, 0, 0, 1, 0, -TL), Tlm)
                Tm = Tlm
            elif op == "Do":
                stats["xobject_ops"] += 1
                if _is_form_xobject(page, o):
                    stats["form_xobject_ops"] += 1

            # Colour the object-level route would lose if it regenerated this stream.
            # pdfium re-encodes colour through its own model, and anything outside DeviceRGB/DeviceGray
            # comes back black: CMYK, Separation/DeviceN, shadings, pattern fills.
            if op in ("k", "K", "sh"):
                stats["fragile_color"] = True
            elif op in ("cs", "CS") and o and str(o[0]) not in ("/DeviceRGB", "/DeviceGray"):
                stats["fragile_color"] = True
            elif op in ("scn", "SCN") and o and isinstance(o[-1], pikepdf.Name):
                stats["fragile_color"] = True

            if op not in ("Tj", "TJ", "'", '"') or Tfs == 0:
                out.append(ins)
                continue

            # ── a show operator ──
            if op == "'":
                Tlm = _concat((1, 0, 0, 1, 0, -TL), Tlm)
                Tm = Tlm
                items, prefix = [o[0]], [ContentStreamInstruction([], Operator("T*"))]
            elif op == '"':
                Tw, Tc = float(o[0]), float(o[1])
                Tlm = _concat((1, 0, 0, 1, 0, -TL), Tlm)
                Tm = Tlm
                items = [o[2]]
                prefix = [ContentStreamInstruction([o[0]], Operator("Tw")),
                          ContentStreamInstruction([o[1]], Operator("Tc")),
                          ContentStreamInstruction([], Operator("T*"))]
            else:
                items = list(o[0]) if op == "TJ" else [o[0]]
                prefix = []

            font = fonts.get(Tf) or _FontInfo({})
            txt_mat = _concat(Tm, ctm)
            x = 0.0                      # text-space cursor, post-scale units
            new_items, pending_kern, dropped = [], 0.0, 0
            keep = bytearray()

            def flush_keep():
                nonlocal pending_kern
                if keep:
                    if abs(pending_kern) > 1e-6:
                        new_items.append(round(pending_kern, 2))
                        pending_kern = 0.0
                    new_items.append(String(bytes(keep)))
                    keep.clear()

            for it in items:
                # Explicit type split: `bytes(-300)` is a ZERO-FILLED BUFFER request,
                # not an error, so duck-typing eats kern numbers —
                # and `float(String(b"123"))` would eat digit-only text.
                if not isinstance(it, (pikepdf.String, bytes, bytearray)):
                    k = float(it)
                    pending_kern += k
                    x += (-k / 1000.0) * Tfs * Th
                    continue
                raw = bytes(it)
                unit = 2 if font.cid else 1
                for code, sp in font.codes(raw):
                    adv = font.advance(code, sp, Tfs, Tc, Tw, Th)
                    c0 = _apply(txt_mat, x, Trise)
                    c1 = _apply(txt_mat, x + adv, Trise + Tfs)
                    box = (min(c0[0], c1[0]), min(c0[1], c1[1]),
                           max(c0[0], c1[0]), max(c0[1], c1[1]))
                    if hit(box):
                        flush_keep()
                        pending_kern += -adv * 1000.0 / (Tfs * Th)
                        dropped += 1
                    else:
                        keep.extend(code.to_bytes(unit, "big"))
                    x += adv
                # Flush at the ITEM boundary: kern numbers live BETWEEN items,
                # so `pending_kern` is only ever "before these kept glyphs" while inside one item.
                # Holding survivors across items would emit a later kern ahead of earlier text
                # and shift it (measured: LEFT slid +4.8pt — the following kern's advance).
                flush_keep()

            Tm = _concat((1, 0, 0, 1, x, 0), Tm)     # tracker stays truthful
            if not dropped:
                out.append(ins)
                continue
            stats["ops_rewritten"] += 1
            stats["glyphs"] += dropped
            out.extend(prefix)
            if abs(pending_kern) > 1e-6 or not new_items:
                # trailing (or total) erasure still advances —
                # a kern-only TJ keeps any LATER show op on this line from sliding left
                new_items.append(round(pending_kern, 2))
            out.append(ContentStreamInstruction([Array(new_items)], Operator("TJ")))

        page.Contents = pdf.make_stream(pikepdf.unparse_content_stream(out))
        buf = io.BytesIO()
        pdf.save(buf)
    return buf.getvalue(), stats

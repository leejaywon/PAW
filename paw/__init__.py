"""PAW — PDF Architecture Wizard.

A layout-surgery engine for PDFs on a fully permissive stack
(pypdfium2 · pikepdf · reportlab · pdfplumber · Pillow),
so it can ship inside a closed-source commercial service.
Every backend is permissively licensed; see NOTICE for the per-package licences.

    import paw

    doc  = paw.open("form.pdf")
    page = doc.pages[0]

    page.text_spans()              # read: text + font + size + colour + bbox
    page.strokes(); page.fills()   # read: vector art, for gridline clustering
    page.images()                  # read: placed images + bboxes
    page.raster(dpi=200)           # read: PIL image of the page

    page.erase_text(region)        # write: text out of the content stream
    page.erase_art(region)         # write: covered rules/highlights out
    page.draw_text("…", at=(x, y), font=f, size=12)
    page.draw_box(region); page.draw_rule((x0, y0), (x1, y1))
    page.place_image(pil, region)

    doc.save("form.en.pdf")

Conventions, fixed once so three backends cannot disagree:

  * Coordinates are **points, origin top-left, y growing DOWN** —
    the convention the layout fitter already speaks.
    pdfium answers bottom-up and pdfplumber carries both;
    every value is normalised AT THIS BOUNDARY and nowhere else.
  * A `region` is a plain `(x0, y0, x1, y1)` tuple in that space.
  * `draw_text(at=…)` positions the BASELINE start, like a PDF `Td`.

Engine model (v0, deliberately simple): the document lives as **bytes**.
Readers open a fresh backend handle on those bytes;
erase operations run a pdfium pass and replace the bytes; 
draw operations accumulate per page and are flushed through one reportlab overlay per page at save time 
(merged by pikepdf's `add_overlay`, which isolates graphics state).
A bytes round-trip per erase is not the fast path —
it is the CORRECT path while the engine is being proven against the caller's regression suite;
profiling comes after.
"""
from __future__ import annotations

import ctypes
import io
from dataclasses import dataclass, field
from pathlib import Path

import threading

import pikepdf
import pypdfium2 as pdfium
import pypdfium2.raw as _fp

# PDFium is NOT thread-safe — not even across different documents
# (documented upstream, won't-fix, global state).
# A consumer that threads must not crash mysteriously,
# so every pdfium section in this module holds this lock.
# True parallelism belongs to processes, not threads.
_PDFIUM = threading.Lock()

__all__ = ["open", "blank", "Document", "Page", "Font", "Span", "SceneObject",
           "LayerRender", "Rect"]

_EPS = 0.5          # pt tolerance on containment — object bounds are loose


# ── geometry ─────────────────────────────────────────────────────────────────

class Rect:
    """An axis-aligned rectangle in PAW's frame (points, top-left, y-down).

    Independent design note: a rectangle type is arithmetic, not API design —
    intersection, union, containment and padding follow from the definition.
    Semantics chosen here:
    `&` intersection (empty stays empty), `|` union,
    `+ (a, b, c, d)` adds componentwise
    (so a NEGATIVE first pair grows the box leftward/upward and a positive one insets it),
    `contains` accepts a point or a rect,
    and an empty rect intersects nothing.
    """

    __slots__ = ("x0", "y0", "x1", "y1")

    def __init__(self, x0=None, y0=None, x1=None, y1=None):
        if x0 is None:                       # Rect() — the empty zero rect
            x0 = y0 = x1 = y1 = 0.0
        elif y0 is None:                     # Rect((x0, y0, x1, y1)) or Rect(Rect)
            x0, y0, x1, y1 = x0
        self.x0, self.y0, self.x1, self.y1 = float(x0), float(y0), float(x1), float(y1)

    # iteration/indexing make `tuple(r)`, `r[1]` and unpacking work
    def __iter__(self):
        return iter((self.x0, self.y0, self.x1, self.y1))

    def __getitem__(self, i):
        return (self.x0, self.y0, self.x1, self.y1)[i]

    def __len__(self):
        return 4

    def __eq__(self, other):
        try:
            return tuple(self) == tuple(Rect(other))
        except (TypeError, ValueError):
            return NotImplemented

    def __hash__(self):
        return hash(tuple(self))

    def __lt__(self, other):                 # sortable: reading order-ish (y, x)
        return (self.y0, self.x0, self.y1, self.x1) < (other.y0, other.x0, other.y1, other.x1)

    def __repr__(self):
        return (f"Rect({self.x0:.4g}, {self.y0:.4g}, "
                f"{self.x1:.4g}, {self.y1:.4g})")

    # ── measures ──
    @property
    def width(self):
        return max(0.0, self.x1 - self.x0)

    @property
    def height(self):
        return max(0.0, self.y1 - self.y0)

    @property
    def area(self):
        return self.width * self.height

    @property
    def empty(self):
        return self.x1 <= self.x0 or self.y1 <= self.y0

    # ── predicates ──
    def intersects(self, other) -> bool:
        o = Rect(other)
        if self.empty or o.empty:
            return False
        return not (o.x1 <= self.x0 or o.x0 >= self.x1
                    or o.y1 <= self.y0 or o.y0 >= self.y1)

    def contains(self, other) -> bool:
        """True if `other` (a rect, or an (x, y) point) lies inside."""
        if isinstance(other, (tuple, list)) and len(other) == 2:
            x, y = other
            return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1
        o = Rect(other)
        return (o.x0 >= self.x0 and o.x1 <= self.x1
                and o.y0 >= self.y0 and o.y1 <= self.y1)

    def normalize(self) -> "Rect":
        """Reorder corners in place so x0≤x1, y0≤y1. → self."""
        if self.x0 > self.x1:
            self.x0, self.x1 = self.x1, self.x0
        if self.y0 > self.y1:
            self.y0, self.y1 = self.y1, self.y0
        return self

    # ── constructions ──
    def __and__(self, other):
        o = Rect(other)
        return Rect(max(self.x0, o.x0), max(self.y0, o.y0),
                    min(self.x1, o.x1), min(self.y1, o.y1))

    def __or__(self, other):
        o = Rect(other)
        if self.empty:
            return Rect(o)
        if o.empty:
            return Rect(self)
        return Rect(min(self.x0, o.x0), min(self.y0, o.y0),
                    max(self.x1, o.x1), max(self.y1, o.y1))

    def __add__(self, pad):
        a, b, c, d = pad
        return Rect(self.x0 + a, self.y0 + b, self.x1 + c, self.y1 + d)


# ── fonts ────────────────────────────────────────────────────────────────────

class Font:
    """An embeddable TTF and its metrics.

    reportlab both measures (`stringWidth`) and subsets+embeds the file at draw time,
    so one registration serves `width()` and `draw_text()` alike.
    """

    _registered: dict[str, "Font"] = {}

    def __init__(self, rl_name: str, path: Path):
        self.rl_name = rl_name
        self.path = path

    @classmethod
    def load(cls, path: str | Path) -> "Font":
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        path = Path(path)
        key = str(path.resolve())
        if key in cls._registered:
            return cls._registered[key]
        name = f"paw-{path.stem}-{len(cls._registered)}"
        pdfmetrics.registerFont(TTFont(name, str(path)))
        font = cls(name, path)
        cls._registered[key] = font
        return font

    def width(self, text: str, size: float) -> float:
        """Advance width of `text` at `size`, in points."""
        from reportlab.pdfbase import pdfmetrics
        return pdfmetrics.stringWidth(text, self.rl_name, size)

    def has_glyph(self, codepoint: int) -> bool:
        """Does the font carry a real glyph for this codepoint?
        Drives fallback routing —
        GoNoto lacks a surprising set of everyday marks (○ ● ▣ Ⅰ)."""
        if not hasattr(self, "_cmap"):
            from fontTools.ttLib import TTFont as _FT
            ft = _FT(str(self.path), fontNumber=0, lazy=True)
            object.__setattr__(self, "_cmap", set(ft.getBestCmap()))
        return codepoint in self._cmap

    def covers(self, text: str) -> bool:
        """Can every visible character render without fallback?"""
        return all(self.has_glyph(ord(c)) for c in text if not c.isspace())


def _color_tuple(c):
    """pdfminer colours arrive as a scalar (grey), a tuple, or None —
    normalise to a float tuple ONCE at this boundary
    (measured: a bare `0.5` grey fill crashed every consumer that iterated it)."""
    if c is None:
        return None
    if isinstance(c, (int, float)):
        return (float(c),)
    return tuple(float(v) for v in c)


# ── read-side value types ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Span:
    """One run of same-styled text. bbox is (x0, y0, x1, y1), top-left y-down."""
    text: str
    font: str
    size: float
    color: tuple | None
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True)
class SceneObject:
    """One painted PDF object in stable paint order.

    ``bbox`` is always in PAW's top-left frame, including children of Form
    XObjects.  ``paint_order`` is the object's path through the page object
    tree; it is stable while the source content streams stay unchanged and is
    therefore a suitable source identifier for a translation/editor manifest.

    Colours are the RGBA values PDFium resolves for display.  They preserve
    opacity and paint intent without claiming to preserve the source PDF colour
    space; callers that need DeviceCMYK/Separation identities must inspect the
    content stream separately.
    """

    id: str
    page: int
    kind: str
    bbox: tuple[float, float, float, float]
    paint_order: tuple[int, ...]
    parent_ids: tuple[str, ...] = ()
    fill_rgba: tuple[int, int, int, int] | None = None
    stroke_rgba: tuple[int, int, int, int] | None = None
    stroke_width: float | None = None
    matrix: tuple[float, float, float, float, float, float] | None = None
    marked_content_id: int | None = None
    text: str | None = None
    font_size: float | None = None
    path_fill_mode: int | None = None
    path_stroke: bool | None = None


@dataclass(frozen=True)
class LayerRender:
    """Rendered page plus the same page with removable text suppressed.

    PDFium cannot regenerate a changed Form XObject stream after removing one
    of its children.  Text nested in forms is therefore reported as
    ``unreachable_text_objects`` and remains visible in ``without_text``.  The
    explicit count prevents a background detector from treating that image as
    a complete text-free ground truth.
    """

    composite: object
    without_text: object
    removed_text_objects: int
    unreachable_text_objects: int


_SCENE_KINDS = {
    _fp.FPDF_PAGEOBJ_UNKNOWN: "unknown",
    _fp.FPDF_PAGEOBJ_TEXT: "text",
    _fp.FPDF_PAGEOBJ_PATH: "path",
    _fp.FPDF_PAGEOBJ_IMAGE: "image",
    _fp.FPDF_PAGEOBJ_SHADING: "shading",
    _fp.FPDF_PAGEOBJ_FORM: "form",
}


def _scene_rgba(obj, getter) -> tuple[int, int, int, int] | None:
    channels = [ctypes.c_uint() for _ in range(4)]
    if not getter(obj, *(ctypes.byref(v) for v in channels)):
        return None
    return tuple(int(v.value) for v in channels)


def _scene_matrix(obj) -> tuple[float, float, float, float, float, float] | None:
    matrix = _fp.FS_MATRIX()
    if not _fp.FPDFPageObj_GetMatrix(obj, ctypes.byref(matrix)):
        return None
    return tuple(float(v) for v in (matrix.a, matrix.b, matrix.c,
                                    matrix.d, matrix.e, matrix.f))


def _compose_matrix(outer: tuple[float, ...], inner: tuple[float, ...]) -> tuple[float, ...]:
    """Return a matrix that applies ``inner`` first and ``outer`` second."""
    oa, ob, oc, od, oe, of = outer
    ia, ib, ic, id_, ie, iff = inner
    return (oa * ia + oc * ib, ob * ia + od * ib,
            oa * ic + oc * id_, ob * ic + od * id_,
            oa * ie + oc * iff + oe, ob * ie + od * iff + of)


def _scene_bbox(obj, height: float, ancestor: tuple[float, ...]) -> tuple[float, float, float, float] | None:
    values = [ctypes.c_float() for _ in range(4)]
    if not _fp.FPDFPageObj_GetBounds(obj, *(ctypes.byref(v) for v in values)):
        return None
    left, bottom, right, top = (float(v.value) for v in values)
    a, b, c, d, e, f = ancestor
    corners = [(a * x + c * y + e, b * x + d * y + f)
               for x in (left, right) for y in (bottom, top)]
    xs = [p[0] for p in corners]
    ys = [p[1] for p in corners]
    return (min(xs), height - max(ys), max(xs), height - min(ys))


def _scene_text(obj, text_page) -> str | None:
    length = _fp.FPDFTextObj_GetText(obj, text_page, None, 0)
    if length <= 1:
        return None
    buf = (ctypes.c_ushort * length)()
    got = _fp.FPDFTextObj_GetText(obj, text_page, buf, length)
    if got <= 1:
        return None
    return bytes(buf).decode("utf-16-le", "replace").rstrip("\x00")


def _route_glyphs(text: str, font: Font,
                  fallbacks: tuple[Font, ...]) -> list[tuple[Font, str]]:
    """Split `text` into (font, run) pieces, longest runs per covering font.
    Whitespace never forces a switch — it rides with the current run."""
    if not fallbacks or font.covers(text):
        return [(font, text)] if text else []
    out: list[tuple[Font, str]] = []
    cur_font, cur = None, ""
    for ch in text:
        pick = font if (ch.isspace() or font.has_glyph(ord(ch))) else next(
            (fb for fb in fallbacks if fb.has_glyph(ord(ch))), font)
        if ch.isspace() and cur_font is not None:
            pick = cur_font                      # spaces stay with the run
        if pick is not cur_font and cur:
            out.append((cur_font, cur))
            cur = ""
        cur_font, cur = pick, cur + ch
    if cur:
        out.append((cur_font, cur))
    return out


def _spans_of(chars: list) -> list[Span]:
    """Chars of ONE line → styled Spans (font/size/colour runs, left to right)."""
    chars = sorted(chars, key=lambda c: c["x0"])
    out: list[Span] = []
    run: list = []

    def close():
        if not run:
            return
        out.append(Span(
            text="".join(c["text"] for c in run),
            font=run[0].get("fontname", ""),
            size=round(float(run[0].get("size", 0.0)), 2),
            color=_color_tuple(run[0].get("non_stroking_color")),
            bbox=(min(c["x0"] for c in run), min(c["top"] for c in run),
                  max(c["x1"] for c in run), max(c["bottom"] for c in run))))
        run.clear()

    prev = None
    for ch in chars:
        if prev is not None and (
                ch.get("fontname") != prev.get("fontname")
                or abs(float(ch.get("size", 0)) - float(prev.get("size", 0))) > 0.1
                or _color_tuple(ch.get("non_stroking_color"))
                != _color_tuple(prev.get("non_stroking_color"))):
            close()
        run.append(ch)
        prev = ch
    close()
    return out


def _stray_line(run: list) -> dict:
    return {"bbox": (min(c["x0"] for c in run), min(c["top"] for c in run),
                     max(c["x1"] for c in run), max(c["bottom"] for c in run)),
            "spans": _spans_of(run)}


# ── document / page ──────────────────────────────────────────────────────────

def open(source: str | Path | bytes) -> "Document":   # noqa: A001 — deliberate
    """Open a PDF from a path or raw bytes. → Document."""
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    return Document(data)


def blank(width: float, height: float) -> "Document":
    """A new one-page document — the photo front-door wraps an image in one."""
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(width, height))
    buf = io.BytesIO()
    pdf.save(buf)
    return Document(buf.getvalue())


class Document:
    def __init__(self, data: bytes):
        self._bytes = data
        # draw ops queued per page index, flushed at save() — see module doc
        self._pending: dict[int, list[dict]] = {}

    # ── structure ──
    @property
    def page_count(self) -> int:
        with _PDFIUM:
            pdf = pdfium.PdfDocument(self._bytes)
            try:
                return len(pdf)
            finally:
                pdf.close()

    @property
    def pages(self) -> list["Page"]:
        return [Page(self, i) for i in range(self.page_count)]

    def __len__(self) -> int:
        return self.page_count

    def __getitem__(self, i: int) -> "Page":
        n = self.page_count
        if not -n <= i < n:
            raise IndexError(i)
        return Page(self, i % n if i < 0 else i)

    def __iter__(self):
        return iter(self.pages)

    def subset(self, keep: list[int]) -> None:
        """Keep only the 0-based pages in `keep`, in their original order."""
        with pikepdf.open(io.BytesIO(self._bytes)) as pdf:
            for i in reversed(range(len(pdf.pages))):
                if i not in keep:
                    del pdf.pages[i]
            buf = io.BytesIO()
            pdf.save(buf)
        self._bytes = buf.getvalue()
        self._pending = {keep.index(i): ops for i, ops in self._pending.items()
                         if i in keep}

    def tobytes(self) -> bytes:
        """The document with all pending draws applied."""
        self._flush()
        return self._bytes

    def save(self, path: str | Path) -> None:
        Path(path).write_bytes(self.tobytes())

    # ── draw flush: one reportlab overlay per page, merged by pikepdf ──
    def _flush(self) -> None:
        if not self._pending:
            return
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas as rl_canvas

        with pikepdf.open(io.BytesIO(self._bytes)) as base:
            for pi, ops in sorted(self._pending.items()):
                box = base.pages[pi].mediabox
                w, h = float(box[2]) - float(box[0]), float(box[3]) - float(box[1])
                buf = io.BytesIO()
                # bottomup=0 → reportlab speaks PAW's top-left y-down convention natively:
                # a page-level flip `cm` plus a counter-flipped text matrix,
                # so glyphs stay upright and rects/lines just work.
                cv = rl_canvas.Canvas(buf, pagesize=(w, h), bottomup=0)
                for op in ops:
                    self._draw_op(cv, op, ImageReader)
                cv.save()
                buf.seek(0)
                with pikepdf.open(buf) as ov:
                    # add_overlay wraps the page in q…Q
                    # and scopes resources inside a Form XObject under a random name —
                    # state cannot leak and /F1 cannot collide.
                    base.pages[pi].add_overlay(ov.pages[0])
            out = io.BytesIO()
            base.save(out)
        self._bytes = out.getvalue()
        self._pending = {}

    @staticmethod
    def _draw_op(cv, op: dict, ImageReader) -> None:
        kind = op["kind"]
        if kind == "text":
            cv.setFont(op["font"].rl_name, op["size"])
            cv.setFillColorRGB(*op["color"])
            if op.get("angle"):
                # Under the page-level y-flip a positive canvas rotation runs clockwise on screen;
                # negate so angle=90 reads bottom-to-top,
                # the direction sidebar labels use.
                cv.saveState()
                cv.translate(op["at"][0], op["at"][1])
                cv.rotate(-op["angle"])
                cv.drawString(0, 0, op["text"])
                cv.restoreState()
            else:
                cv.drawString(op["at"][0], op["at"][1], op["text"])
        elif kind == "box":
            x0, y0, x1, y1 = op["region"]
            cv.setLineWidth(op["width"])
            if op["fill"]:
                cv.setFillColorRGB(*op["fill"])
            if op["stroke"]:
                cv.setStrokeColorRGB(*op["stroke"])
            cv.rect(x0, y0, x1 - x0, y1 - y0,
                    stroke=1 if op["stroke"] else 0, fill=1 if op["fill"] else 0)
        elif kind == "rule":
            cv.setLineWidth(op["width"])
            cv.setStrokeColorRGB(*op["color"])
            cv.line(*op["a"], *op["b"])
        elif kind == "image":
            x0, y0, x1, y1 = op["region"]
            w, h = x1 - x0, y1 - y0
            # Under the global y-flip an image would come out mirrored;
            # a local counter-flip (translate + scale(1,-1)) restores it upright
            # while keeping the top-left placement contract.
            cv.saveState()
            cv.translate(x0, y1)
            cv.scale(1, -1)
            cv.drawImage(ImageReader(op["image"]), 0, 0, w, h)
            cv.restoreState()


class Page:
    def __init__(self, doc: Document, index: int):
        self.doc = doc
        self.index = index

    # ── geometry ──
    @property
    def size(self) -> tuple[float, float]:
        with _PDFIUM:
            pdf = pdfium.PdfDocument(self.doc._bytes)
            try:
                return pdf[self.index].get_size()
            finally:
                pdf.close()

    @property
    def box(self) -> Rect:
        """The page box as a Rect at origin (0, 0), PAW frame."""
        w, h = self.size
        return Rect(0, 0, w, h)

    # ── readers ──
    def raster(self, dpi: int = 150, clip: Rect | tuple | None = None,
               crisp: bool = False):
        """Render the page (or just `clip` of it). → PIL.Image (RGB).

        `crisp=True` disables text/path/image anti-aliasing —
        for consumers that MEASURE pixels (walls, background probes) rather than display them:
        AA gradients at art edges read as phantom walls to a row-comparison walk,
        and they differ between renderers besides.

        v0 renders the whole page and crops — correct first;
        a matrix render for large-dpi small clips is a later optimisation.
        """
        self.doc._flush()   # readers see every queued write
        kw = ({"no_smoothtext": True, "no_smoothimage": True,
               "no_smoothpath": True} if crisp else {})
        with _PDFIUM:
            pdf = pdfium.PdfDocument(self.doc._bytes)
            try:
                img = pdf[self.index].render(scale=dpi / 72, **kw).to_pil().convert("RGB")
            finally:
                pdf.close()
        if clip is None:
            return img
        c = Rect(clip) & self.box
        k = dpi / 72
        return img.crop((int(c.x0 * k), int(c.y0 * k),
                         max(int(c.x0 * k) + 1, round(c.x1 * k)),
                         max(int(c.y0 * k) + 1, round(c.y1 * k))))

    def scene_objects(self) -> list[SceneObject]:
        """Return the page's painted object tree in stable paint order.

        Text, paths, images, shadings and Form XObjects all use the same
        top-left page frame.  Form children are included after their parent and
        carry ``parent_ids`` so a consumer can retain both the visual page
        geometry and the content-stream boundary that controls editability.
        """
        self.doc._flush()
        identity = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        out: list[SceneObject] = []
        with _PDFIUM:
            pdf = pdfium.PdfDocument(self.doc._bytes)
            try:
                page = pdf[self.index]
                _, height = page.get_size()
                text_page = page.get_textpage()
                try:
                    def walk(count: int, get_object, order: tuple[int, ...],
                             parents: tuple[str, ...], ancestor: tuple[float, ...]) -> None:
                        for index in range(count):
                            obj = get_object(index)
                            kind_id = _fp.FPDFPageObj_GetType(obj)
                            path = (*order, index)
                            object_id = f"p{self.index + 1}:" + ".".join(
                                f"{part:04d}" for part in path)
                            bbox = _scene_bbox(obj, height, ancestor)
                            if bbox is None:
                                continue
                            width = ctypes.c_float()
                            stroke_width = (float(width.value)
                                            if _fp.FPDFPageObj_GetStrokeWidth(
                                                obj, ctypes.byref(width)) else None)
                            font_size = None
                            text = None
                            if kind_id == _fp.FPDF_PAGEOBJ_TEXT:
                                size = ctypes.c_float()
                                if _fp.FPDFTextObj_GetFontSize(obj, ctypes.byref(size)):
                                    font_size = float(size.value)
                                text = _scene_text(obj, text_page.raw)
                            fill_mode = None
                            path_stroke = None
                            if kind_id == _fp.FPDF_PAGEOBJ_PATH:
                                fill = ctypes.c_int()
                                stroke = ctypes.c_int()
                                if _fp.FPDFPath_GetDrawMode(
                                        obj, ctypes.byref(fill), ctypes.byref(stroke)):
                                    fill_mode = int(fill.value)
                                    path_stroke = bool(stroke.value)
                            mcid = _fp.FPDFPageObj_GetMarkedContentID(obj)
                            out.append(SceneObject(
                                id=object_id,
                                page=self.index,
                                kind=_SCENE_KINDS.get(kind_id, f"unknown-{kind_id}"),
                                bbox=bbox,
                                paint_order=path,
                                parent_ids=parents,
                                fill_rgba=_scene_rgba(obj, _fp.FPDFPageObj_GetFillColor),
                                stroke_rgba=_scene_rgba(obj, _fp.FPDFPageObj_GetStrokeColor),
                                stroke_width=stroke_width,
                                matrix=_scene_matrix(obj),
                                marked_content_id=(mcid if mcid >= 0 else None),
                                text=text,
                                font_size=font_size,
                                path_fill_mode=fill_mode,
                                path_stroke=path_stroke,
                            ))
                            if kind_id == _fp.FPDF_PAGEOBJ_FORM:
                                matrix = _scene_matrix(obj) or identity
                                child_ancestor = _compose_matrix(ancestor, matrix)
                                walk(_fp.FPDFFormObj_CountObjects(obj),
                                     lambda child_index, form=obj: _fp.FPDFFormObj_GetObject(
                                         form, child_index),
                                     path, (*parents, object_id), child_ancestor)

                    walk(_fp.FPDFPage_CountObjects(page.raw),
                         lambda index: _fp.FPDFPage_GetObject(page.raw, index),
                         (), (), identity)
                finally:
                    text_page.close()
            finally:
                pdf.close()
        return out

    def raster_layers(self, dpi: int = 150, clip: Rect | tuple | None = None,
                      crisp: bool = False) -> LayerRender:
        """Render the normal page and a diagnostic copy without top-level text.

        The second image exposes the composite pixels under ordinary text, so a
        caller can measure backgrounds without sampling through glyph ink.
        Form-XObject text remains and is counted explicitly because PDFium does
        not regenerate modified Form streams when saving the page.
        """
        self.doc._flush()
        composite = self.raster(dpi=dpi, clip=clip, crisp=crisp)
        removed = 0
        unreachable = 0
        with _PDFIUM:
            pdf = pdfium.PdfDocument(self.doc._bytes)
            try:
                page = pdf[self.index]

                def count_form_text(form) -> int:
                    count = 0
                    for child_index in range(_fp.FPDFFormObj_CountObjects(form)):
                        child = _fp.FPDFFormObj_GetObject(form, child_index)
                        child_kind = _fp.FPDFPageObj_GetType(child)
                        if child_kind == _fp.FPDF_PAGEOBJ_TEXT:
                            count += 1
                        elif child_kind == _fp.FPDF_PAGEOBJ_FORM:
                            count += count_form_text(child)
                    return count

                for index in reversed(range(_fp.FPDFPage_CountObjects(page.raw))):
                    obj = _fp.FPDFPage_GetObject(page.raw, index)
                    kind = _fp.FPDFPageObj_GetType(obj)
                    if kind == _fp.FPDF_PAGEOBJ_TEXT:
                        if _fp.FPDFPage_RemoveObject(page.raw, obj):
                            _fp.FPDFPageObj_Destroy(obj)
                            removed += 1
                    elif kind == _fp.FPDF_PAGEOBJ_FORM:
                        unreachable += count_form_text(obj)
                if removed:
                    _fp.FPDFPage_GenerateContent(page.raw)
                buf = io.BytesIO()
                pdf.save(buf)
            finally:
                pdf.close()
        without_text = Document(buf.getvalue())[self.index].raster(
            dpi=dpi, clip=clip, crisp=crisp)
        return LayerRender(composite, without_text, removed, unreachable)

    def text_spans(self) -> list[Span]:
        """Same-styled text runs with bbox, font, size, colour (top-left y-down)."""
        self.doc._flush()   # readers see every queued write
        import pdfplumber
        spans: list[Span] = []
        with pdfplumber.open(io.BytesIO(self.doc._bytes)) as pdf:
            pg = pdf.pages[self.index]
            W, H = float(pg.width), float(pg.height)
            run: list[dict] = []

            def close_run():
                if not run:
                    return
                spans.append(Span(
                    text="".join(ch["text"] for ch in run),
                    font=run[0].get("fontname", ""),
                    size=round(float(run[0].get("size", 0.0)), 2),
                    color=_color_tuple(run[0].get("non_stroking_color")),
                    bbox=(min(ch["x0"] for ch in run), min(ch["top"] for ch in run),
                          max(ch["x1"] for ch in run), max(ch["bottom"] for ch in run)),
                ))
                run.clear()

            prev = None
            for ch in pg.chars:
                # Clip to the page box.
                # pdfminer reports text drawn OFF the page
                # (measured: a template credit line at y 549-565 on a 540pt-high deck)
                # which the renderer never shows —
                # an engine that returns it would make the caller pay to translate invisible text.
                if (ch["x1"] <= 0 or ch["x0"] >= W
                        or ch["bottom"] <= 0 or ch["top"] >= H):
                    continue
                brk = (prev is None
                       or ch.get("fontname") != prev.get("fontname")
                       or abs(float(ch.get("size", 0)) - float(prev.get("size", 0))) > 0.1
                       or _color_tuple(ch.get("non_stroking_color"))
                       != _color_tuple(prev.get("non_stroking_color"))
                       or abs(ch["top"] - prev["top"]) > 2.0        # new line
                       or ch["x0"] - prev["x1"] > float(prev.get("size", 12)))  # gap
                if brk:
                    close_run()
                run.append(ch)
                prev = ch
            close_run()
        return spans

    def text_lines(self) -> list[dict]:
        """Layout lines with styled spans: [{"bbox": (x0,y0,x1,y1), "spans": [Span…]}].

        Line GEOMETRY comes from pdfium's text engine (FPDFText_CountRects) —
        the same class of segmentation the consumers of this data were tuned against —
        not from baseline-bucket heuristics,
        which merged side-by-side deck columns into one mega-line
        and split wide-spaced parentheticals (both measured on real documents).
        Fragments the engine splits inside one visual row (style runs, word gaps)
        are re-joined when the gap is space-sized relative to the row height;
        column-sized gaps stay splits.
        Style comes from the char layer, assigned by centre point;
        leftover chars (rotated text and anything the engine missed)
        fall back to baseline grouping so nothing is dropped.
        """
        self.doc._flush()
        with _PDFIUM:
            pdf = pdfium.PdfDocument(self.doc._bytes)
            try:
                page = pdf[self.index]
                _, H = page.get_size()
                tp = page.get_textpage()
                n = _fp.FPDFText_CountRects(tp.raw, 0, -1)
                frags = []
                for i in range(n):
                    l, t, r, b = (ctypes.c_double() for _ in range(4))
                    _fp.FPDFText_GetRect(
                        tp.raw, i, *(ctypes.byref(v) for v in (l, t, r, b)))
                    frags.append([l.value, H - t.value, r.value, H - b.value])
                # Loose char boxes carry the FONT-METRIC vertical extent —
                # measured identical to the numbers
                # the layout fitter's heuristics were tuned against
                # (tight/ink tops sat up to 0.4em lower and skewed every placement built on them).
                loose = []
                for i in range(_fp.FPDFText_CountChars(tp.raw)):
                    rf = _fp.FS_RECTF()
                    if _fp.FPDFText_GetLooseCharBox(tp.raw, i, ctypes.byref(rf)):
                        loose.append((rf.left, H - rf.top, rf.right, H - rf.bottom))
                tp.close()
            finally:
                pdf.close()

        # fragment → row merge
        frags.sort(key=lambda f: (f[1] + f[3]) / 2)
        rows: list[list[float]] = []
        for f in frags:
            merged = False
            for row in rows:
                ov = min(row[3], f[3]) - max(row[1], f[1])
                h = min(row[3] - row[1], f[3] - f[1])
                if h > 0 and ov / h >= 0.5:
                    # interval gap, order-independent
                    # (the first cut used max(fx0−rowx1, rowx0−fx1),
                    # which fabricates a huge gap whenever the fragment sits LEFT of the row —
                    # measured 91pt on two fragments 7pt apart)
                    gap = max(f[0], row[0]) - min(f[2], row[2])
                    # tight on purpose: measured word-space fragments sit 7–8pt apart,
                    # while a SEPARATE text box on the same row
                    # ("01." beside its label — a passthrough unit that must keep its own size)
                    # starts 34pt away.
                    # Splitting there is what lets untranslated marker boxes keep their styling.
                    if gap <= max(1.2 * h, 9.0):
                        row[0] = min(row[0], f[0]); row[1] = min(row[1], f[1])
                        row[2] = max(row[2], f[2]); row[3] = max(row[3], f[3])
                        merged = True
                        break
            if not merged:
                rows.append(list(f))

        # Greedy first-match seeds row ISLANDS
        # (a row grown from the rightmost fragment and one grown from the middle
        # can end up 7pt apart yet never compared) —
        # coalesce rows to a fixpoint with the same rule.
        changed = True
        while changed:
            changed = False
            for i in range(len(rows)):
                for j in range(len(rows) - 1, i, -1):
                    a, b = rows[i], rows[j]
                    ov = min(a[3], b[3]) - max(a[1], b[1])
                    h = min(a[3] - a[1], b[3] - b[1])
                    if h <= 0 or ov / h < 0.5:
                        continue
                    if max(a[0], b[0]) - min(a[2], b[2]) <= max(1.2 * h, 9.0):
                        a[0] = min(a[0], b[0]); a[1] = min(a[1], b[1])
                        a[2] = max(a[2], b[2]); a[3] = max(a[3], b[3])
                        del rows[j]
                        changed = True

        # chars → rows (style layer)
        import pdfplumber
        with pdfplumber.open(io.BytesIO(self.doc._bytes)) as pdf_:
            pg = pdf_.pages[self.index]
            W, Hp = float(pg.width), float(pg.height)
            chars = [ch for ch in pg.chars
                     if not (ch["x1"] <= 0 or ch["x0"] >= W
                             or ch["bottom"] <= 0 or ch["top"] >= Hp)]
        buckets: list[list] = [[] for _ in rows]
        stray: list = []
        for ch in chars:
            cx, cy = (ch["x0"] + ch["x1"]) / 2, (ch["top"] + ch["bottom"]) / 2
            for i, row in enumerate(rows):
                if row[0] - 1 <= cx <= row[2] + 1 and row[1] - 1 <= cy <= row[3] + 1:
                    buckets[i].append(ch)
                    break
            else:
                stray.append(ch)

        # rows → font-metric vertical bounds, then spans inherit them
        row_loose: list[list] = [[] for _ in rows]
        for lb in loose:
            cx, cy = (lb[0] + lb[2]) / 2, (lb[1] + lb[3]) / 2
            for i, row in enumerate(rows):
                if row[0] - 1 <= cx <= row[2] + 1 and row[1] - 2 <= cy <= row[3] + 2:
                    row_loose[i].append(lb)
                    break
        lines = []
        for row, chs, lbs in zip(rows, buckets, row_loose):
            if not chs:
                continue
            if lbs:
                row = [row[0], min(b[1] for b in lbs), row[2], max(b[3] for b in lbs)]
            spans = _spans_of(chs)
            if lbs:
                fixed = []
                for sp in spans:
                    within = [b for b in lbs
                              if sp.bbox[0] - 1 <= (b[0] + b[2]) / 2 <= sp.bbox[2] + 1]
                    if within:
                        fixed.append(Span(sp.text, sp.font, sp.size, sp.color,
                                          (sp.bbox[0], min(b[1] for b in within),
                                           sp.bbox[2], max(b[3] for b in within))))
                    else:
                        fixed.append(sp)
                spans = fixed
            lines.append({"bbox": tuple(row), "spans": spans})
        # rotated/missed chars: baseline-bucket fallback, one line per run
        stray.sort(key=lambda c: (round(c["bottom"] / 2), c["x0"]))
        run: list = []
        for ch in stray:
            if run and (abs(ch["bottom"] - run[-1]["bottom"]) > 2.0
                        or ch["x0"] - run[-1]["x1"] > 2.5 * float(ch.get("size", 10))):
                lines.append(_stray_line(run)); run = []
            run.append(ch)
        if run:
            lines.append(_stray_line(run))
        lines.sort(key=lambda ln: (round(ln["bbox"][3] / 3), ln["bbox"][0]))
        return lines

    def strokes(self) -> list[dict]:
        """Stroked vector art (lines + outlined rects) — gridline fodder.
        Each: {kind, x0, y0, x1, y1, width, color}, top-left y-down."""
        self.doc._flush()   # readers see every queued write
        import pdfplumber
        out: list[dict] = []
        with pdfplumber.open(io.BytesIO(self.doc._bytes)) as pdf:
            pg = pdf.pages[self.index]
            for ln in pg.lines:
                out.append({"kind": "line", "x0": ln["x0"], "y0": ln["top"],
                            "x1": ln["x1"], "y1": ln["bottom"],
                            "width": ln.get("linewidth", 0),
                            "color": _color_tuple(ln.get("stroking_color"))})
            for rc in pg.rects:
                if rc.get("stroke"):
                    out.append({"kind": "rect", "x0": rc["x0"], "y0": rc["top"],
                                "x1": rc["x1"], "y1": rc["bottom"],
                                "width": rc.get("linewidth", 0),
                                "color": _color_tuple(rc.get("stroking_color"))})
        return out

    def fills(self) -> list[dict]:
        """Filled vector art (cell shading, highlight bars)."""
        self.doc._flush()   # readers see every queued write
        import pdfplumber
        out: list[dict] = []
        with pdfplumber.open(io.BytesIO(self.doc._bytes)) as pdf:
            pg = pdf.pages[self.index]
            for rc in list(pg.rects) + list(pg.curves):
                if rc.get("fill"):
                    out.append({"kind": rc.get("object_type", "rect"),
                                "x0": rc["x0"], "y0": rc["top"],
                                "x1": rc["x1"], "y1": rc["bottom"],
                                "color": _color_tuple(rc.get("non_stroking_color"))})
        return out

    def images(self) -> list[dict]:
        """Placed images: {name, x0, y0, x1, y1}, top-left y-down."""
        self.doc._flush()   # readers see every queued write
        import pdfplumber
        with pdfplumber.open(io.BytesIO(self.doc._bytes)) as pdf:
            pg = pdf.pages[self.index]
            return [{"name": im.get("name", ""), "x0": im["x0"], "y0": im["top"],
                     "x1": im["x1"], "y1": im["bottom"]} for im in pg.images]

    # ── annotations ──
    def links(self) -> list[dict]:
        """Link annotations: [{index, x0, y0, x1, y1, uri}], top-left y-down.
        `index` is the annotation's position in the page's /Annots array —
        the handle `set_link_rects` takes.
        Annotations are page furniture, not content:
        erasers and overlays leave them alone,
        so a link whose text is re-laid must be MOVED with `set_link_rects`
        or it keeps hotspotting (and, in viewers that tint link rects, painting) the old location.
        No flush: annotations live outside the content stream,
        so queued draws cannot affect them (and vice versa)."""
        _, H = self.size
        out: list[dict] = []
        with pikepdf.open(io.BytesIO(self.doc._bytes)) as pdf:
            annots = pdf.pages[self.index].get("/Annots")
            for i, a in enumerate(annots or []):
                if a.get("/Subtype") != pikepdf.Name("/Link"):
                    continue
                x0, y0, x1, y1 = (float(v) for v in a["/Rect"])
                act = a.get("/A")
                uri = (str(act["/URI"]) if act is not None
                       and act.get("/URI") is not None else None)
                out.append({"index": i, "uri": uri,
                            "x0": min(x0, x1), "y0": H - max(y0, y1),
                            "x1": max(x0, x1), "y1": H - min(y0, y1)})
        return out

    def set_link_rects(self, updates: list[tuple[int, tuple]]) -> int:
        """Move link annotations: [(index in /Annots, top-left-frame region)].
        One byte round-trip for the whole batch; returns how many moved."""
        if not updates:
            return 0
        _, H = self.size
        moved = 0
        with pikepdf.open(io.BytesIO(self.doc._bytes)) as pdf:
            annots = pdf.pages[self.index].get("/Annots")
            if annots is None:
                return 0
            for i, region in updates:
                if not 0 <= i < len(annots):
                    continue
                x0, y0, x1, y1 = region
                annots[i].Rect = pikepdf.Array(
                    [min(x0, x1), H - max(y0, y1), max(x0, x1), H - min(y0, y1)])
                moved += 1
            buf = io.BytesIO()
            pdf.save(buf)
        self.doc._bytes = buf.getvalue()
        return moved

    # ── erasers ──
    def erase_text(self, region: tuple) -> dict:
        """Remove the text touching `region` from the content stream.

        Show operators are rewritten at glyph precision: an erased glyph becomes a kern of the same
        advance, so surviving text does not move, and every other operator is re-emitted unchanged.
        Leaving the rest of the stream alone is the whole point of doing it this way.
        The object-level route (pdfium removal) has to call FPDFPage_GenerateContent afterwards, and
        that regeneration re-encodes colour through pdfium's own model: a page painted in CMYK,
        Separation/DeviceN or a shading pattern comes back solid black, from a single object removed.
        Print-ready documents are precisely the ones painted that way, and they are most of what gets
        translated, so that route is gone from this path — `erase_art` still uses it, on pages whose
        art is being removed anyway.
        The coverage limit is text inside a Form XObject, which the walker does not descend into.
        Object-level removal never reached it either (pdfium enumerates page objects only, which is
        why FPDFFormObj_* exists), so nothing regressed by dropping it — but it is now REPORTED in
        `unreachable` rather than silently left behind.
        """
        self.doc._flush()   # operations apply strictly in call order
        _, H = self.size
        regions = ([tuple(x) for x in region]
                   if region and isinstance(region[0], (tuple, list, Rect))
                   else [tuple(region)])
        flipped = [(x0, H - y1, x1, H - y0) for x0, y0, x1, y1 in regions]

        from . import _textstream
        new_bytes, stats = _textstream.erase_glyphs(self.doc._bytes, self.index, flipped)
        if stats["ops_rewritten"]:
            self.doc._bytes = new_bytes
        return {"removed": stats["glyphs"], "skipped": 0,
                "unreachable": stats["form_xobject_ops"], **stats}

    def erase_art(self, region: tuple) -> dict:
        """Remove path objects (rules, highlight bars) fully covered by `region`.

        The second erase engine — the pass the first migration estimate missed.
        Object-level removal aimed at FPDF_PAGEOBJ_PATH.
        A path object that extends beyond the region
        (e.g. one object drawing a whole table grid)
        is conservatively kept and counted in `skipped` —
        subpath splitting is future precision work.

        Removal makes pdfium regenerate the content stream, which re-encodes colour through its own
        model, so on a page painted in CMYK, Separation/DeviceN or a shading every fill on it turns
        black — the whole page lost to remove one rule. Those pages are refused instead, reported as
        `fragile_color`: an underline left standing is a blemish, a black page is the document gone.
        Erasing art through the stream walker the way `erase_text` does would lift the restriction.
        """
        self.doc._flush()
        if self._fragile_color():
            return {"removed": 0, "skipped": 0, "fragile_color": True}
        return self._erase(region, kinds=(_fp.FPDF_PAGEOBJ_PATH,))

    def _fragile_color(self) -> bool:
        """Cached: does this page paint in colour pdfium's regeneration would lose?

        Cached per page because callers erase region by region — the answer cannot change under them,
        since removing art and drawing RGB text never introduce a colour space that was not there.
        """
        if getattr(self, "_fragile_color_cache", None) is None:
            from . import _textstream
            self._fragile_color_cache = _textstream.uses_fragile_color(
                self.doc._bytes, self.index)
        return self._fragile_color_cache

    def _erase(self, region, kinds: tuple[int, ...]) -> dict:
        self.doc._flush()   # operations apply strictly in call order
        # One region or a list —
        # a batch shares one pdfium pass, which is both faster and safer
        # (a single GenerateContent regeneration).
        regions = ([tuple(r) for r in region]
                   if region and isinstance(region[0], (tuple, list, Rect))
                   else [tuple(region)])
        with _PDFIUM:
            return self._erase_locked(regions, kinds)

    def _erase_locked(self, regions, kinds: tuple[int, ...]) -> dict:
        pdf = pdfium.PdfDocument(self.doc._bytes)
        try:
            page = pdf[self.index]
            _, H = page.get_size()
            # regions → pdfium's bottom-up frame, once, at this boundary
            flipped = [(x0, H - y1, x1, H - y0) for x0, y0, x1, y1 in regions]
            raw = page.raw
            doomed, skipped = [], 0
            for i in range(_fp.FPDFPage_CountObjects(raw)):
                obj = _fp.FPDFPage_GetObject(raw, i)
                if _fp.FPDFPageObj_GetType(obj) not in kinds:
                    continue
                l, b, r, t = (ctypes.c_float() for _ in range(4))
                if not _fp.FPDFPageObj_GetBounds(obj, *(ctypes.byref(v) for v in (l, b, r, t))):
                    continue
                l, b, r, t = l.value, b.value, r.value, t.value
                inside = any(l >= x0 - _EPS and r <= x1 + _EPS
                             and b >= by0 - _EPS and t <= by1 + _EPS
                             for x0, by0, x1, by1 in flipped)
                overlaps = inside or any(
                    not (r < x0 or l > x1 or t < by0 or b > by1)
                    for x0, by0, x1, by1 in flipped)
                if inside:
                    doomed.append(obj)
                elif overlaps:
                    skipped += 1
            for obj in doomed:
                _fp.FPDFPage_RemoveObject(raw, obj)
                _fp.FPDFPageObj_Destroy(obj)     # removal hands ownership back
            if doomed:
                _fp.FPDFPage_GenerateContent(raw)
            buf = io.BytesIO()
            pdf.save(buf)
        finally:
            pdf.close()
        self.doc._bytes = buf.getvalue()
        return {"removed": len(doomed), "skipped": skipped}

    # ── writers (queued; flushed at save) ──
    def draw_text(self, text: str, *, at: tuple[float, float], font: Font,
                  size: float, color: tuple = (0, 0, 0),
                  fallbacks: list[Font] | tuple[Font, ...] = (),
                  angle: float = 0.0) -> None:
        """Draw `text` with its baseline starting at `at` (top-left y-down).

        `fallbacks`: fonts tried PER GLYPH when `font` has no coverage —
        CJK documents routinely mix a main face with marker glyphs it lacks
        (measured: GoNoto has no ○ ● ▣),
        and every CJK consumer would otherwise reimplement this same routing loop.
        A glyph no font covers still goes to the main font
        (its notdef box is at least visible evidence).
        """
        if angle:
            # rotated runs are queued whole —
            # per-glyph fallback advance would need the rotated frame;
            # vertical labels are short, main-font-only
            self._queue({"kind": "text", "text": text, "at": at, "font": font,
                         "size": size, "color": color, "angle": angle})
            return
        x, y = at
        for fnt, seg in _route_glyphs(text, font, tuple(fallbacks)):
            self._queue({"kind": "text", "text": seg, "at": (x, y), "font": fnt,
                         "size": size, "color": color, "angle": 0.0})
            x += fnt.width(seg, size)

    def draw_box(self, region: tuple, *, stroke: tuple | None = (0, 0, 0),
                 fill: tuple | None = None, width: float = 1.0) -> None:
        self._queue({"kind": "box", "region": region, "stroke": stroke,
                     "fill": fill, "width": width})

    def draw_rule(self, a: tuple[float, float], b: tuple[float, float], *,
                  width: float = 1.0, color: tuple = (0, 0, 0)) -> None:
        self._queue({"kind": "rule", "a": a, "b": b, "width": width,
                     "color": color})

    def place_image(self, image, region: tuple) -> None:
        """Place a PIL image (or path) into `region`."""
        self._queue({"kind": "image", "image": image, "region": region})

    def _queue(self, op: dict) -> None:
        self.doc._pending.setdefault(self.index, []).append(op)

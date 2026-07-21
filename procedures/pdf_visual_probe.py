#!/usr/bin/env python3
"""
pdf_visual_probe.py — decide whether a PDF has visual content that pdftotext
CANNOT capture (scanned pages OR embedded charts/diagrams/tables-as-images),
using ONLY poppler-utils CLIs already on the box (pdfinfo, pdffonts, pdfimages,
pdftotext). No pymupdf, no heavy deps, no GPU.

Why not a char-count threshold: a page can be 90% text with ONE embedded chart
that carries the real signal (tokenomics table as image, architecture diagram).
That page has plenty of text, so a char threshold sails right past the thing
that actually needs OCR. This probes per-page structure instead.

Classification per page:
  - text_only : has fonts + extracted text, no significant image area
  - scanned   : no fonts (or ~none) + a page-covering image  -> whole page needs OCR
  - mixed     : has fonts/text AND a large embedded image     -> that image needs OCR
  - empty     : no fonts, no images, no text (blank/vector-only edge case)

Output: JSON verdict with per-page findings + a top-level recommendation:
  ocr_needed: none | some_pages | whole_doc
  ocr_targets: list of {page, reason, image_ids?}

Usage:
  python3 procedures/pdf_visual_probe.py <file.pdf> [--json]
Exit 0 always (advisory).
"""
import json
import subprocess
import sys
from pathlib import Path

# A page-image whose area covers >= this fraction of the page is "significant".
# Tunable: 0.15 catches charts/tables-as-image without flagging tiny logos/icons.
SIGNIFICANT_IMAGE_AREA_FRAC = 0.15
# A single image covering >= this fraction ~= the page IS a scan.
FULLPAGE_IMAGE_AREA_FRAC = 0.80
# Below this many extracted chars on a page, its text layer is effectively empty.
THIN_TEXT_CHARS = 40
# ABSOLUTE pixel floor: an embedded raster this many px on its short side (and
# this much total px area) carries real, OCR-worthy signal REGARDLESS of how
# small it's scaled on the page. This is the fix for the dpi/display-size trap:
# a 600x300 chart set to width:400px looks like ~9% of an A4 page by the dpi
# area math and slips under SIGNIFICANT_IMAGE_AREA_FRAC, yet it's a genuine
# chart/table that pdftotext can't read. A real logo/icon/bullet is far smaller.
SIGNIFICANT_IMAGE_MIN_SHORT_PX = 200
SIGNIFICANT_IMAGE_MIN_AREA_PX = 200 * 200  # 40k px = a small but real figure


def _run(args):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return 1, "", str(e)


def _page_dims(pdf):
    """Return {page_no: (w_pts, h_pts)} from pdfinfo -f 1 -l N (per-page media box)."""
    # pdfinfo gives one media box unless we ask per page; use -l to force page count,
    # then read 'Page size'. For per-page we parse pdfinfo's per-page block if present.
    code, out, _ = _run(["pdfinfo", str(pdf)])
    pages, w, h = 0, None, None
    for line in out.splitlines():
        if line.startswith("Pages:"):
            try:
                pages = int(line.split(":", 1)[1].strip())
            except Exception:
                pass
        elif line.startswith("Page size:"):
            # e.g. "Page size:      595.32 x 841.92 pts (A4)"
            try:
                seg = line.split(":", 1)[1].strip().split("pts")[0]
                parts = seg.replace("x", " ").split()
                w, h = float(parts[0]), float(parts[1])
            except Exception:
                pass
    dims = {}
    if w and h:
        for p in range(1, pages + 1):
            dims[p] = (w, h)
    return pages, dims


def _fonts_by_page(pdf, pages):
    """Return set of pages that have at least one embedded/real font.
    pdffonts -l <N> gives a doc-level list; for per-page we probe page ranges.
    Cheap approach: run pdffonts per page range only if doc is small; else once."""
    have_fonts = set()
    # Doc-level: if the whole doc has zero fonts, it's fully scanned — fast path.
    code, out, _ = _run(["pdffonts", str(pdf)])
    lines = [l for l in out.splitlines() if l.strip()]
    doc_has_any_font = len(lines) > 2  # 2 header lines then font rows
    if not doc_has_any_font:
        return have_fonts, False  # no page has fonts
    # Per-page font presence (bounded: only probe if <= 60 pages to stay cheap).
    if pages <= 60:
        for p in range(1, pages + 1):
            c, o, _ = _run(["pdffonts", "-f", str(p), "-l", str(p), str(pdf)])
            rows = [l for l in o.splitlines() if l.strip()]
            if len(rows) > 2:
                have_fonts.add(p)
    else:
        # Large doc: assume fonts present per-page (doc-level had fonts); we still
        # rely on image-area + thin-text to flag mixed/scanned pages.
        have_fonts = set(range(1, pages + 1))
    return have_fonts, True


def _images_by_page(pdf):
    """Return {page_no: [ (img_id, w_px, h_px, x_dpi, y_dpi) ]} via pdfimages -list."""
    code, out, _ = _run(["pdfimages", "-list", str(pdf)])
    by_page = {}
    for line in out.splitlines():
        parts = line.split()
        # header rows / separators -> skip; data rows start with a page int
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        try:
            page = int(parts[0]); num = int(parts[1])
            w = int(parts[3]); h = int(parts[4])
            xdpi = parts[12] if len(parts) > 12 else "?"
            ydpi = parts[13] if len(parts) > 13 else "?"
        except Exception:
            continue
        by_page.setdefault(page, []).append({
            "img": num, "w_px": w, "h_px": h, "xdpi": xdpi, "ydpi": ydpi,
        })
    return by_page


def _text_chars_by_page(pdf, pages):
    """Return {page_no: char_count} from pdftotext per page."""
    counts = {}
    for p in range(1, pages + 1):
        c, o, _ = _run(["pdftotext", "-f", str(p), "-l", str(p), str(pdf), "-"])
        counts[p] = len(o.strip())
    return counts


def probe(pdf: Path):
    pages, dims = _page_dims(pdf)
    if pages == 0:
        return {"error": "could not read PDF / zero pages", "file": str(pdf)}
    have_fonts, doc_has_font = _fonts_by_page(pdf, pages)
    imgs = _images_by_page(pdf)
    texts = _text_chars_by_page(pdf, pages)

    page_findings = []
    ocr_targets = []
    for p in range(1, pages + 1):
        w_pts, h_pts = dims.get(p, (595.0, 842.0))
        # Approx page area in px at 72dpi baseline for a rough area fraction.
        # We compare embedded image PIXEL area against page area scaled to the
        # image's own DPI where known; fall back to 150dpi assumption.
        page_imgs = imgs.get(p, [])
        # Largest single-image area fraction on the page.
        max_frac = 0.0
        big_img_ids = []
        for im in page_imgs:
            dpi = 150.0
            try:
                dpi = float(im["xdpi"]) if im["xdpi"] not in ("?", "") else 150.0
            except Exception:
                dpi = 150.0
            page_w_px = (w_pts / 72.0) * dpi
            page_h_px = (h_pts / 72.0) * dpi
            page_area = max(page_w_px * page_h_px, 1.0)
            frac = (im["w_px"] * im["h_px"]) / page_area
            if frac > max_frac:
                max_frac = frac
            # Significant if EITHER it covers a big page fraction (dpi math) OR it
            # is an intrinsically substantial raster in absolute pixels (catches
            # scaled-down but data-bearing charts/tables the fraction test misses).
            short_px = min(im["w_px"], im["h_px"])
            area_px = im["w_px"] * im["h_px"]
            big_by_frac = frac >= SIGNIFICANT_IMAGE_AREA_FRAC
            big_by_px = (short_px >= SIGNIFICANT_IMAGE_MIN_SHORT_PX and
                         area_px >= SIGNIFICANT_IMAGE_MIN_AREA_PX)
            if big_by_frac or big_by_px:
                big_img_ids.append(im["img"])

        has_font = p in have_fonts
        text_chars = texts.get(p, 0)
        has_text = text_chars >= THIN_TEXT_CHARS

        if not has_font and max_frac >= FULLPAGE_IMAGE_AREA_FRAC:
            kind = "scanned"
            ocr_targets.append({"page": p, "reason": "no fonts + full-page image", "image_ids": big_img_ids or [i["img"] for i in page_imgs]})
        elif has_text and big_img_ids:
            kind = "mixed"
            ocr_targets.append({"page": p, "reason": f"text present but {len(big_img_ids)} significant image(s) uncaptured (max {max_frac:.0%} of page)", "image_ids": big_img_ids})
        elif not has_text and not page_imgs:
            kind = "empty"
        else:
            kind = "text_only"

        page_findings.append({
            "page": p, "kind": kind, "text_chars": text_chars,
            "has_font": has_font, "images": len(page_imgs),
            "max_image_area_frac": round(max_frac, 3),
        })

    if not ocr_targets:
        rec = "none"
    elif all(f["kind"] == "scanned" for f in page_findings if f["kind"] != "text_only" and f["kind"] != "empty") and \
         any(f["kind"] == "scanned" for f in page_findings):
        rec = "whole_doc"
    else:
        rec = "some_pages"

    return {
        "file": str(pdf),
        "pages": pages,
        "ocr_needed": rec,
        "ocr_targets": ocr_targets,
        "pages_detail": page_findings,
        "summary": {
            "text_only": sum(1 for f in page_findings if f["kind"] == "text_only"),
            "scanned": sum(1 for f in page_findings if f["kind"] == "scanned"),
            "mixed": sum(1 for f in page_findings if f["kind"] == "mixed"),
            "empty": sum(1 for f in page_findings if f["kind"] == "empty"),
        },
    }


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("usage: pdf_visual_probe.py <file.pdf> [--json]", file=sys.stderr)
        sys.exit(0)
    pdf = Path(args[0])
    if not pdf.exists():
        print(json.dumps({"error": "file not found", "file": str(pdf)}))
        sys.exit(0)
    result = probe(pdf)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

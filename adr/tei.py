"""TEI XML as GROBID writes it from a PDF, as semantic HTML for semantic.normalize.

GROBID reads a scholarly PDF into the abstract, the body as a flat run of divisions (each with its heading and its
number, "2.1", which is all that says how deep it sits), paragraphs with their citations and cross-references,
formulas as their text, figures and tables with their captions (a table with its cells), footnotes, and a reference
list of parsed entries. The pictures themselves stay in the PDF: a figure comes out as its caption.

A paper's own metadata comes from where it was found (Zotero's record is the reader's and better than what GROBID
makes of a title page); meta_of is for a PDF with nothing else.
"""
import re
from html import escape

from bs4 import BeautifulSoup, NavigableString, Tag

HEAD_NUM = re.compile(r"^(?:\d+\.)*\d+\.?$|^[A-Z]\.(?:\d+\.?)*$|^[IVX]+\.?$")
CAPTION_HEAD = re.compile(r"^(?:Figure|Fig\.?|Table|Tab\.)\s*[A-Z]?\d+(?:\.\d+)*[a-z]?\s*[:.]", re.I)


def _local(tag):
    return (tag.name or "").split(":")[-1] if isinstance(tag, Tag) else ""


def _find(el, name, **kw):
    return el.find(lambda t: _local(t) == name, **kw) if el is not None else None


def _find_all(el, name, **kw):
    return el.find_all(lambda t: _local(t) == name, **kw) if el is not None else []


def _text(el):
    return re.sub(r"\s+", " ", el.get_text(" ") if el is not None else "").strip()


def _xid(el):
    return next((v for k, v in el.attrs.items() if k.split(":")[-1] == "id"), "") if isinstance(el, Tag) else ""


def _id(el):
    x = _xid(el)
    return f' id="{escape(x)}"' if x else ""


def _person(p):
    sn = _text(_find(p, "surname"))
    fn = " ".join(_text(f) for f in _find_all(p, "forename"))
    return f"{sn}, {fn}" if sn and fn else sn or fn


def meta_of(soup):
    """What GROBID read from the title page: title, authors, DOI, date (no license: a PDF does not say one GROBID reads)."""
    head = _find(soup, "teiHeader")
    title = _text(_find(_find(head, "titleStmt"), "title"))
    analytic = _find(head, "analytic")
    authors = [a for a in (_person(_find(x, "persName")) for x in _find_all(analytic, "author") if _find(x, "persName")) if a]
    doi = next((_text(i) for i in _find_all(head, "idno") if (i.get("type") or "").upper() == "DOI"), "")
    d = _find(_find(head, "publicationStmt"), "date")
    date = (d.get("when") or "") if d is not None else ""
    return {"title": title, "authors": authors, "doi": doi, "date": date, "license": ""}


def to_html(xml):
    """(semantic HTML, meta) for a GROBID TEI document."""
    soup = BeautifulSoup(xml, "lxml-xml")
    meta = meta_of(soup)
    out = []
    abstract = _find(_find(soup, "profileDesc"), "abstract")
    if abstract is not None and _text(abstract):
        out.append("<h2>Abstract</h2>")
        for p in _find_all(abstract, "p"):
            out.append(f"<p>{_inline(p)}</p>")
    text = _find(soup, "text")
    body, back = _find(text, "body"), _find(text, "back")
    # figures and tables come at the end of the body: each goes after the paragraph that first points to it
    figs = {_xid(f): f for f in _find_all(body, "figure") if _xid(f)}
    ctx = {"figs": figs, "placed": set()}
    notes = [n for n in _find_all(text, "note") if n.get("place") == "foot"]
    for div in (body.find_all(recursive=False) if body is not None else []):
        if _local(div) == "div":
            out.extend(_div(div, ctx))
        elif _local(div) == "p":
            out.extend(_para(div, ctx))
    out.extend(_figure(f) for x, f in figs.items() if x not in ctx["placed"])
    out.extend(_figure(f) for f in _find_all(body, "figure") if not _xid(f))
    for div in (back.find_all(recursive=False) if back is not None else []):
        kind = div.get("type") or ""
        if kind == "references":
            out.append(_refs(div))
        elif kind in ("acknowledgement", "funding", "availability", "annex", "") or _local(div) == "div":
            title = {"acknowledgement": "Acknowledgments", "funding": "Funding", "availability": "Data availability"}.get(kind, "")
            inner = _find_all(div, "div", recursive=False) or [div]
            for i, sub in enumerate(inner):
                head = _find(sub, "head", recursive=False)
                if head is None and title and i == 0:
                    out.append(f"<h2>{escape(title)}</h2>")
                out.extend(_div(sub, ctx, top=head is not None))
    if notes:
        items = "".join(f'<li id="{escape(_xid(n))}"><p>{_inline(n)}</p></li>' for n in notes if _xid(n))
        if items:
            out.append(f'<section class="footnotes"><ol>{items}</ol></section>')
    return "\n".join(out), meta


def _depth(n):
    """A heading's level from its number: "3" is a section, "3.2" a subsection, "3.2.1" below it."""
    n = (n or "").strip().rstrip(".")
    return 2 + n.count(".") if re.fullmatch(r"\d+(?:\.\d+)*", n) else 2


def _div(div, ctx, top=True):
    out = []
    head = _find(div, "head", recursive=False)
    if head is not None and CAPTION_HEAD.match(_text(head)):   # a caption GROBID took for a heading: read as text
        out.append(f"<p>{_inline(head)}</p>")
        head = None
    if head is not None and top:
        n = (head.get("n") or "").strip()
        lvl = min(_depth(n), 6)
        num = f"{escape(n)} " if n and HEAD_NUM.match(n) else ""
        out.append(f"<h{lvl}{_id(div)}>{num}{_inline(head)}</h{lvl}>")
    for ch in div.find_all(recursive=False):
        name = _local(ch)
        if name == "p":
            out.extend(_para(ch, ctx))
        elif name == "formula":
            out.append(_formula(ch))
        elif name == "list":
            out.append(_list(ch))
        elif name == "figure":
            x = _xid(ch)
            if x not in ctx["placed"]:
                ctx["placed"].add(x)
                out.append(_figure(ch))
        elif name == "div":
            out.extend(_div(ch, ctx))
    return out


def _para(p, ctx):
    out = [f"<p>{_inline(p)}</p>"] if _text(p) else []
    for r in _find_all(p, "ref"):
        target = (r.get("target") or "").lstrip("#")
        if r.get("type") in ("figure", "table") and target in ctx["figs"] and target not in ctx["placed"]:
            ctx["placed"].add(target)
            out.append(_figure(ctx["figs"][target]))
    return out


def _formula(f):
    """A formula as GROBID reads it: its text (no markup to typeset), with its number."""
    label = _text(_find(f, "label"))
    body = "".join(_node(c) for c in f.children if _local(c) != "label").strip()
    return f"<p{_id(f)}>{body}{f' {escape(label)}' if label else ''}</p>" if body else ""


def _list(el):
    items = "".join(f"<li>{_inline(i)}</li>" for i in _find_all(el, "item", recursive=False))
    return f"<ul>{items}</ul>" if items else ""


def _figure(fig):
    """A figure, or a table (type="table") with its cells; the caption as "Figure 3: ...", as semantic.py reads it."""
    is_table = fig.get("type") == "table"
    label = _text(_find(fig, "label", recursive=False))
    head = _text(_find(fig, "head", recursive=False))
    desc = _find(fig, "figDesc", recursive=False)
    word = "Table" if is_table else "Figure"
    name = f"{word} {label}" if label else head
    text = _inline(desc).strip() if desc is not None else ""
    if name and re.match(re.escape(name), _text(desc), re.I):   # the caption names itself already
        name = ""
    cap = f"{escape(name)}: {text}" if name and text else escape(name) or text
    caption = f"<figcaption>{cap}</figcaption>" if cap else ""
    table = _find(fig, "table", recursive=False)
    rows = []
    for r in _find_all(table, "row"):
        tag = "th" if r.get("role") == "label" else "td"
        cells = "".join(f'<{tag}{_span(c)}>{_inline(c)}</{tag}>' for c in _find_all(r, "cell", recursive=False))
        rows.append(f"<tr>{cells}</tr>")
    grid = f"<table><tbody>{''.join(rows)}</tbody></table>" if rows else ""
    if not (caption or grid):
        return ""
    return f"<figure{_id(fig)}>{caption}{grid}</figure>"


def _span(c):
    return "".join(f' {k}="{escape(c[v])}"' for k, v in (("colspan", "cols"), ("rowspan", "rows")) if c.get(v))


def _node(n):
    if isinstance(n, NavigableString):
        return escape(str(n))
    if not isinstance(n, Tag):
        return ""
    name = _local(n)
    if name == "ref":
        target, kind = (n.get("target") or "").split()[0].lstrip("#") if n.get("target") else "", n.get("type", "")
        inner = _inline(n)
        if kind == "bibr":
            return f'<cite class="ltx_cite"><a href="#{escape(target)}">{inner}</a></cite>' if target else inner
        if kind == "foot":
            return f'<sup><a href="#{escape(target)}">{inner}</a></sup>' if target else f"<sup>{inner}</sup>"
        if kind == "url" or re.match(r"https?://", target):
            return f'<a href="{escape(target or n.get_text(""))}">{inner}</a>'
        return f'<a href="#{escape(target)}">{inner}</a>' if target else inner
    if name == "hi":
        rend = n.get("rend") or ""
        tag = "em" if "italic" in rend else "strong" if "bold" in rend else "sub" if "subscript" in rend else "sup" if "superscript" in rend else ""
        return f"<{tag}>{_inline(n)}</{tag}>" if tag else _inline(n)
    if name in ("label", "graphic", "coords"):
        return ""
    if name == "formula":   # inline
        return _inline(n)
    if name == "lb":
        return " "
    return _inline(n)


def _inline(el):
    return "".join(_node(c) for c in el.children).strip() if el is not None else ""


def _refs(div):
    items = []
    for b in _find_all(div, "biblStruct"):
        items.append(f'<li{_id(b)}>{_bibl(b)}</li>')
    return f"<h2>References</h2><ul>{''.join(items)}</ul>" if items else ""


def _bibl(b):
    """A parsed reference as it reads: authors (year) title. Source volume pages. Its raw text when GROBID kept it."""
    raw = next((n for n in _find_all(b, "note") if n.get("type") == "raw_reference"), None)
    if raw is not None and _text(raw):
        return escape(_text(raw))
    analytic, monogr = _find(b, "analytic"), _find(b, "monogr")
    who = analytic if analytic is not None and _find(analytic, "author") else monogr
    names = [_person(_find(a, "persName")) for a in _find_all(who, "author") if _find(a, "persName")]
    title = _text(_find(analytic, "title")) if analytic is not None else ""
    source = _text(_find(monogr, "title"))
    if not title:
        title, source = source, ""
    d = _find(_find(monogr, "imprint"), "date")
    year = ((d.get("when") or "")[:4] or _text(d)) if d is not None else ""
    bits = {s.get("unit"): _text(s) or "-".join(x for x in (s.get("from"), s.get("to")) if x)
            for s in _find_all(_find(monogr, "imprint"), "biblScope")}
    doi = next((_text(i) for i in _find_all(b, "idno") if (i.get("type") or "").upper() == "DOI"), "")
    parts = ["; ".join(n for n in names if n), f"({escape(year)})" if year else "", escape(title) + "." if title else "",
             f"<em>{escape(source)}</em>" if source else "", escape(bits.get("volume", "")), escape(bits.get("page", "")),
             f"doi:{escape(doi)}" if doi else ""]
    return escape(parts[0]) + (". " if parts[0] else "") + " ".join(p for p in parts[1:] if p)

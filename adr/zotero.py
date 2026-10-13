"""A Zotero library, browsed and read: Zotero Desktop's local API for the library, GROBID for an item's PDF.

  Zotero Desktop   http://localhost:23119/api/   the web API's routes without a key: users/0/ is the reader's own
                   library, groups/<id>/ a group's. Zotero 7 serves it once "Allow other applications on this
                   computer to communicate with Zotero" is on (Settings › Advanced). Read only: nothing here changes
                   the library.
  GROBID           http://localhost:8070            a scholarly PDF as TEI XML (tei.py): sections, paragraphs,
                   citations, captions, references. Run on this computer: docker run --rm -p 8070:8070 grobid/grobid:0.9.1-crf

An item becomes a paper by the best text it has: its arXiv paper when it has an arXiv id (arXiv's HTML), else its
PDF through GROBID, else its DOI through Europe PMC. A paper read from the PDF is stored as papers/zotero-<lib>-<key>/
(source.html, the TEI as source.tei.xml, meta.json) and the PDF stays where Zotero keeps it: the paper holds the
attachment's key and finds the file when the reader opens the Original view (pdf.py). links.json, beside the papers,
says which item became which paper, whatever its source, so the library shows what is open.

The library and the addresses are the reader's settings (~/.paperfold/settings.json, "zotero"): nothing about anyone's
library is written here.
"""
import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import local, store
from .fetch import UA

ZOTERO = "http://localhost:23119"
GROBID = "http://localhost:8070"
KEY = re.compile(r"[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}")   # Zotero's item keys
LIB = re.compile(r"users/0|groups/\d{1,10}")
PID = re.compile(r"zotero-(g\d{1,10}|u)-([a-z0-9]{8})")
ARXIV = re.compile(r"(?:arxiv\.org/(?:abs|pdf|html)/|arXiv:\s*|10\.48550/arXiv\.)(\d{4}\.\d{4,5})", re.I)
CITEKEY = re.compile(r"^\s*Citation Key:\s*(\S+)", re.M | re.I)
SKIP_TYPES = {"attachment", "note", "annotation"}
LINKS = "links.json"
_lock = threading.Lock()


class Off(RuntimeError):
    """Zotero is not running, or its local API is off: what the app says, by name."""


def settings():
    from .models import load_settings
    z = load_settings().get("zotero") or {}
    return {"library": z.get("library") or "", "base": (z.get("base") or ZOTERO).rstrip("/"),
            "grobid": (z.get("grobid") or GROBID).rstrip("/"), "data_dir": z.get("data_dir") or str(Path.home() / "Zotero")}


def save_settings(**kw):
    from .models import load_settings, save_settings as save
    s = load_settings()
    z = dict(s.get("zotero") or {})
    for k, v in kw.items():
        if v is None:
            continue
        if k == "library" and v and not LIB.fullmatch(v):
            raise ValueError("bad-library")
        z[k] = str(v).strip()
    s["zotero"] = z
    save(s)
    return settings()


# ── the local API ─────────────────────────────────────────────────────

def _get(path, params=None, base=None, raw=False, timeout=20):
    """One request to Zotero's local API; (body, headers). Off when Zotero does not answer or its API is off."""
    url = f"{base or settings()['base']}/api/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={**UA, "Zotero-API-Version": "3"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            return (body if raw else json.loads(body or b"null")), r.headers
    except urllib.error.HTTPError as e:
        if e.code == 403:   # "Local API is not enabled"
            raise Off("api-off")
        raise
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        raise Off("not-running")


def _all(path, params=None):
    """Every row of a listing: the API gives at most 100 a request."""
    rows, start = [], 0
    while True:
        page, headers = _get(path, {**(params or {}), "limit": 100, "start": start})
        rows += page or []
        total = int(headers.get("Total-Results") or len(rows))
        start += 100
        if not page or start >= total:
            return rows


def status():
    """Whether Zotero answers, and the libraries it holds (the reader's own, then each group)."""
    s = settings()
    try:
        groups, _ = _get("users/0/groups", {"limit": 100})
    except Off as e:
        return {"ok": False, "error": str(e), "library": s["library"], "libraries": [], "grobid": grobid_ok()}
    libs = [{"id": "users/0", "name": "My Library"}] + [
        {"id": f"groups/{g['id']}", "name": g["data"]["name"], "items": g.get("meta", {}).get("numItems")} for g in groups or []]
    return {"ok": True, "library": s["library"], "libraries": libs, "grobid": grobid_ok()}


def collections(lib):
    """The library's collections as a flat list, each with its parent ("" at the top), sorted by name."""
    _check(lib)
    rows = _all(f"{lib}/collections")
    out = [{"key": c["key"], "name": c["data"]["name"], "parent": c["data"].get("parentCollection") or "",
            "items": c.get("meta", {}).get("numItems", 0)} for c in rows]
    return sorted(out, key=lambda c: c["name"].lower())


def items(lib, collection="", q=""):
    """The items of a collection, or of the whole library (none chosen, or a search), each as the library shows it."""
    _check(lib)
    if collection and not KEY.fullmatch(collection):
        raise ValueError("bad-collection")
    params = {"q": q, "qmode": "titleCreatorYear"} if q else {}
    path = f"{lib}/collections/{collection}/items/top" if collection and not q else f"{lib}/items/top"
    rows = _all(path, params)
    opened = links()
    out = []
    for r in rows:
        it = item_of(r)
        if it["type"] in SKIP_TYPES:
            continue
        it["paper"] = opened.get(f"{lib}/{it['key']}", "")
        out.append(it)
    return out


def item_of(r):
    """An item of the API, as the library shows it."""
    d, m = r["data"], r.get("meta", {})
    att = (r.get("links") or {}).get("attachment") or {}
    authors = [c.get("lastName") or c.get("name") or "" for c in d.get("creators", []) if c.get("creatorType") in ("author", "editor", None)]
    extra = d.get("extra") or ""
    ck = CITEKEY.search(extra)
    return {"key": r["key"], "type": d.get("itemType", ""), "title": d.get("title") or "(untitled)",
            "authors": [a for a in authors if a], "year": (m.get("parsedDate") or d.get("date") or "")[:4],
            "doi": d.get("DOI") or "", "url": d.get("url") or "", "arxiv": arxiv_of(d),
            "citekey": d.get("citationKey") or (ck.group(1) if ck else ""), "collections": d.get("collections", []),
            "pdf": att.get("attachmentType") == "application/pdf"}


def arxiv_of(d):
    for v in (d.get("url"), d.get("extra"), d.get("archiveID"), d.get("DOI"), d.get("publicationTitle")):
        m = ARXIV.search(v or "")
        if m:
            return m.group(1)
    if (d.get("repository") or d.get("libraryCatalog") or "").lower() == "arxiv":
        m = re.search(r"\d{4}\.\d{4,5}", d.get("number") or d.get("archiveID") or "")
        if m:
            return m.group(0)
    return ""


def _check(lib):
    if not LIB.fullmatch(lib or ""):
        raise ValueError("bad-library")


# ── an item becomes a paper ───────────────────────────────────────────

def pid_of(lib, key):
    return f"zotero-{'u' if lib == 'users/0' else 'g' + lib.split('/')[1]}-{key.lower()}"


def lib_key(pid):
    m = PID.fullmatch(pid)
    if not m:
        raise ValueError(f"not a Zotero paper: {pid}")
    return ("users/0" if m.group(1) == "u" else f"groups/{m.group(1)[1:]}"), m.group(2).upper()


def ref_of(ref):
    """(library, item key) of "zotero:groups/123/ABCD2345", else (None, None)."""
    m = re.fullmatch(r"zotero:(users/0|groups/\d{1,10})/([23456789A-Z]{8})", (ref or "").strip())
    return (m.group(1), m.group(2)) if m else (None, None)


def resolve(lib, key):
    """The paper an item becomes: its arXiv paper when it has an arXiv id; else papers/zotero-…, read from its PDF
    (or, with none, from its DOI in Europe PMC) when it is generated. The choice is kept in links.json."""
    _check(lib)
    r, _ = _get(f"{lib}/items/{key}")
    it = item_of(r)
    if it["arxiv"]:
        pid = it["arxiv"]
    else:
        pid = pid_of(lib, key)
        if not (store.pdir(pid) / "meta.json").exists() and not it["pdf"] and it["doi"]:
            from . import epmc
            pid = epmc.by_doi(it["doi"])   # ValueError("no-fulltext") when Europe PMC holds none
        elif not it["pdf"] and not (store.pdir(pid) / "meta.json").exists():
            raise ValueError("no-pdf")
    _link(f"{lib}/{key}", pid, it)
    return pid


def links():
    f = store.DATA / "papers" / LINKS
    try:
        return {k: v["paper"] for k, v in json.loads(f.read_text()).items()}
    except (OSError, ValueError):
        return {}


def _link(where, pid, it):
    f = store.DATA / "papers" / LINKS
    with _lock:
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError):
            data = {}
        data[where] = {"paper": pid, "title": it["title"], "citekey": it["citekey"], "collections": it["collections"]}
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
        tmp.replace(f)


def pdf_attachment(lib, key):
    """The item's PDF: the attachment's key, file name, and how Zotero keeps it."""
    kids = _all(f"{lib}/items/{key}/children")
    pdfs = [k["data"] for k in kids if k["data"].get("contentType") == "application/pdf"]
    pdfs.sort(key=lambda a: a.get("linkMode") == "linked_url")   # a stored file before a link to the web
    if not pdfs:
        raise RuntimeError("no-pdf")
    a = pdfs[0]
    return {"key": a["key"], "filename": a.get("filename") or "", "mode": a.get("linkMode") or "", "path": a.get("path") or ""}


def pdf_file(z):
    """Where a paper's PDF is now, from what its meta.json keeps ("zotero"): Zotero says, when it is running; else the
    data folder's storage/<attachment>/; else, for a file Zotero links to, its own path. None when it is gone."""
    lib, att = z.get("library"), z.get("attachment")
    if not (lib and att):
        return None
    try:
        url, _ = _get(f"{lib}/items/{att}/file/view/url", raw=True, timeout=5)
        p = Path(urllib.request.url2pathname(urllib.parse.urlparse(url.decode().strip()).path))
        if p.is_file():
            return p
    except (Off, urllib.error.HTTPError, OSError, ValueError):
        pass
    for p in (Path(settings()["data_dir"]).expanduser() / "storage" / att / (z.get("filename") or ""),
              Path(z.get("path") or "") if z.get("mode") == "linked_file" else None):
        if p and p.name and p.is_file():
            return p
    return None


def fetch(pid, progress=lambda *a: None):
    """Store an item read from its PDF (once): GROBID's TEI, as semantic HTML, with Zotero's own record as the meta."""
    d = store.pdir(pid)
    if (d / "meta.json").exists() and (d / "source.html").exists():
        return pid
    lib, key = lib_key(pid)
    r, _ = _get(f"{lib}/items/{key}")
    it = item_of(r)
    att = pdf_attachment(lib, key)
    z = {"library": lib, "item": key, "attachment": att["key"], "filename": att["filename"], "mode": att["mode"],
         "path": att["path"], "citekey": it["citekey"], "collections": it["collections"]}
    f = pdf_file(z)
    if f is None:
        raise RuntimeError("no-pdf")
    data = f.read_bytes()
    progress("fetch", 0, 1)
    tei = grobid(data)
    from . import tei as tei_
    html, m = tei_.to_html(tei)
    z["sha1"] = hashlib.sha1(data).hexdigest()
    d_ = r["data"]
    meta = {"id": pid, "version": "", "title": it["title"] if it["title"] != "(untitled)" else (m["title"] or f.stem),
            "authors": _authors(d_) or m["authors"], "date": d_.get("date") or m["date"],
            "abs_url": d_.get("url") or (f"https://doi.org/{it['doi']}" if it["doi"] else ""), "html_url": "",
            "license": "", "doi": it["doi"] or m["doi"], "source": {"kind": "zotero", "name": it["citekey"] or att["filename"]},
            "label": it["citekey"] or "Zotero", "zotero": z}
    return local.store_doc(pid, html, meta, "source.tei.xml", tei, lambda folder: (lambda src: None))


def _authors(d):
    out = []
    for c in d.get("creators", []):
        if c.get("creatorType") not in ("author", None):
            continue
        out.append(f"{c['lastName']}, {c.get('firstName', '')}".rstrip(", ") if c.get("lastName") else c.get("name", ""))
    return [a for a in out if a]


# ── GROBID ────────────────────────────────────────────────────────────

def grobid_ok():
    try:
        with urllib.request.urlopen(f"{settings()['grobid']}/api/isalive", timeout=3) as r:
            return r.read().strip().lower() == b"true"
    except (urllib.error.URLError, OSError):
        return False


def grobid(pdf, timeout=300):
    """A PDF as GROBID's TEI. Consolidation is off: it would look each reference up on Crossref, and the reader's
    library is read on this computer only."""
    boundary = uuid.uuid4().hex
    fields = {"consolidateHeader": "0", "consolidateCitations": "0", "includeRawCitations": "1", "segmentSentences": "0"}
    body = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items())
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="input"; filename="paper.pdf"\r\n'
             f"Content-Type: application/pdf\r\n\r\n").encode() + pdf + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{settings()['grobid']}/api/processFulltextDocument", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Accept": "application/xml"})
    for wait in (2, 5, 10, 20, None):   # 503: all its workers are busy; asked again a little later
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code != 503 or wait is None:
                raise RuntimeError(f"grobid-failed: HTTP {e.code}")
        except (urllib.error.URLError, ConnectionError):
            raise RuntimeError("no-grobid")
        time.sleep(wait)

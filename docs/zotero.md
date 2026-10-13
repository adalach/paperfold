# Zotero

Browse your Zotero library inside PaperFold, its collections as Zotero shows them, and read any item at the five
levels. Everything happens on your computer: PaperFold talks to the Zotero app you already run, the way citation
plugins for Word and other apps do, and needs no Zotero account, key or web service.

## Set up

1. **Zotero 7, open, with its local connection on.** In Zotero: *Settings › Advanced › Allow other applications on
   this computer to communicate with Zotero*. This is the same switch other apps on your computer use to reach
   your library.
2. **GROBID, for items with only a PDF.** GROBID is an open-source tool that reads a scholarly PDF into its parts:
   title, abstract, sections and their headings, paragraphs, figure and table captions, references. It runs on your
   computer, in Docker:

   ```bash
   docker run --rm -p 8070:8070 grobid/grobid:0.9.1-crf
   ```

   The `-crf` image is 0.5 GB and reads well; `-full` (about 15 GB) adds deep-learning models that read references
   and affiliations better. Without GROBID, items on arXiv (and items whose full text Europe PMC holds) still open.
3. In PaperFold: **Zotero library**, on the start page or at the foot of the sidebar. Pick a library (your own or
   a group you belong to), then a collection, then **Read** on an item.

## How an item becomes a paper

The best text an item has:

1. An arXiv ID (in its URL, `extra` or archive ID): the paper is read from arXiv, as any arXiv link.
2. Otherwise its PDF in Zotero, read by GROBID.
3. No PDF: its DOI, when Europe PMC holds the full text.

A paper read from its PDF is stored like any other, in `papers/zotero-g<group id>-<item key>/` (or
`zotero-u-<item key>` for your own library): GROBID's output as it came (`source.tei.xml`), the text the reader is
made from (`source.html`), and Zotero's own record in `meta.json` (title, authors, date, DOI, Better BibTeX citekey,
collections). The PDF is not copied: the paper keeps the attachment's key, and **Original** opens the file where
Zotero keeps it, even with Zotero closed. `papers/links.json` notes which item became which paper, so the library
marks what you have opened. `papers/` is not part of the repository.

## What stays on your computer

- PaperFold reads Zotero through `http://localhost:23119`, which Zotero serves on this computer only. It only reads:
  nothing in your library is changed.
- PDFs go to GROBID at `http://localhost:8070`, on this computer. GROBID's consolidation, which would look each
  reference up on Crossref, is switched off.
- The only request that leaves your computer is the Europe PMC lookup for an item with no PDF and a DOI, as when you
  paste a DOI. Generating the levels uses the model you connected, as for any paper.
- The library you picked is remembered in `~/.paperfold/settings.json`. The addresses can be changed there too
  (`"zotero": {"base": ..., "grobid": ..., "data_dir": ...}`), for a Zotero or GROBID on another port or a Zotero
  data folder that is not `~/Zotero`.

## Limits

- Figures come out as their captions: GROBID leaves the pictures in the PDF.
- Formulas come out as their text, not typeset.
- GROBID misreads some layouts: a thesis's list of abbreviations can come out as headings, and letters set apart
  ("T H E L I B R A R Y") stay apart.
- An arXiv paper without an HTML version cannot be read yet, even when Zotero has its PDF.
- A PDF changed in Zotero after it was read keeps the paper made from the old one.

## For developers

`adr/zotero.py` holds the local API client, the choice of source, and the call to GROBID; `adr/tei.py` writes
GROBID's TEI as the semantic HTML every source goes through (`semantic.normalize`), as `jats.py` does for JATS. The
server's routes are `/api/zotero`, `/api/zotero/collections`, `/api/zotero/items` and `/api/zotero/settings`; an item
is generated as any paper, `POST /api/generate {paper: "zotero:groups/<id>/<item key>"}`.

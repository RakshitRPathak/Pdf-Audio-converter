import io

import pytest
from pypdf import PdfReader, PdfWriter

from conftest import LOREM, make_pdf
from pdf_extract import PdfError, clean_pages, extract


def test_extracts_and_reflows_hard_wrapped_lines():
    doc = extract(make_pdf([LOREM]))
    assert doc.text == "The quick brown fox jumps over the lazy dog. This sentence exists so the page has enough readable text to pass the scanned-document check easily."
    assert (doc.pages_total, doc.pages_used) == (1, 1)


def test_rejoins_words_hyphenated_across_lines_but_keeps_hyphens_inside_a_line():
    text = clean_pages(["The infor-\nmation is state-of-the-art and very impor-\ntant to all."])
    assert "information" in text and "important" in text and "state-of-the-art" in text
    # Known limitation: a real compound split exactly at a line end ("well-\nknown") is indistinguishable
    # from a hyphenated word and is joined. That is the standard trade-off; the result is still spoken naturally.
    assert clean_pages(["It is well-\nknown that"]) == "It is wellknown that."


def test_removes_running_headers_and_page_numbers():
    pdf = make_pdf([[f"Body sentence number {i} is deliberately longer than any running header would be, so it stays."] for i in range(5)],
                   header="ACME Annual Report 2026", footer_numbers=True)
    text = extract(pdf).text
    assert "ACME" not in text and "Page" not in text
    assert text.count("Body sentence number") == 5


def test_short_documents_keep_their_first_line():
    """Header removal must not fire on 1-2 page documents (could delete real content)."""
    text = extract(make_pdf([["Title line", *LOREM]])).text
    assert text.startswith("Title line")


def test_headings_get_a_pause_and_urls_and_citations_are_cleaned():
    text = clean_pages(["Introduction\n\nSee https://example.com/very/long/path?x=1 for details [12] and more [3, 4].\nThe end."])
    assert text.startswith("Introduction.\n\n")
    assert "https" not in text and "[12]" not in text and "[3, 4]" not in text and "link" in text


def test_normalises_ligatures_and_control_characters():
    assert clean_pages(["e\ufb03cient \ufb01le\x00 \x07ok"]) == "efficient file ok."


def test_page_range_and_validation():
    pdf = make_pdf([[f"Page {n} text is here and is long enough to be considered readable content."] for n in range(1, 6)])
    doc = extract(pdf, 2, 3)
    assert "Page 2" in doc.text and "Page 3" in doc.text and "Page 1 " not in doc.text and "Page 4" not in doc.text
    assert doc.pages_used == 2 and doc.pages_total == 5
    for first, last in [(4, 2), (1, 99), (0, 2), ("x", 2), (-1, 2)]:
        with pytest.raises(PdfError) as e:
            extract(pdf, first, last)
        assert e.value.code == "BAD_RANGE"


def test_not_a_pdf_and_corrupt_pdf():
    with pytest.raises(PdfError) as e:
        extract(b"PK\x03\x04 this is a zip")
    assert e.value.code == "NOT_A_PDF"
    with pytest.raises(PdfError) as e:
        extract(b"%PDF-1.7\n garbage garbage garbage")
    assert e.value.code == "CORRUPT_PDF"


def test_scanned_or_blank_pdf_gets_a_helpful_error():
    with pytest.raises(PdfError) as e:
        extract(make_pdf([[], [], []]))
    assert e.value.code == "NO_TEXT" and "OCR" in e.value.message and e.value.status == 422


def test_password_protected_pdf():
    w = PdfWriter()
    for page in PdfReader(io.BytesIO(make_pdf([LOREM]))).pages:
        w.add_page(page)
    w.encrypt("secret")
    buf = io.BytesIO()
    w.write(buf)
    with pytest.raises(PdfError) as e:
        extract(buf.getvalue())
    assert e.value.code == "ENCRYPTED"


def test_too_many_pages():
    pdf = make_pdf([LOREM] * 5)
    with pytest.raises(PdfError) as e:
        extract(pdf, max_pages=3)
    assert e.value.code == "TOO_MANY_PAGES" and e.value.status == 413

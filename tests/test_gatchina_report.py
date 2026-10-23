"""Offline checks for the exact 2025 visitor-table labels."""

import pytest

from src.etl.adapters.gatchina_report import SOURCE_URL, USER_AGENT, parse_pages, parse_pdf, fetch_pdf


PAGE_1 = "ОТЧЕТ\nГОСУДАРСТВЕННОГО МУЗЕЯ-ЗАПОВЕДНИКА «ГАТЧИНА»\nЗА 2025 год"
# Short reconstructed text fixture matching the inspected page, not a complete PDF.
PAGE_2 = (
    "2. Сведения о посетителях музея\n\n"
    "I. Общее количество посетителей музея -заповедника в 2025 году составило 728  000\n"
    "человек. Из них:\n"
    "Дворцовый парк – 317 655 посетителей по разовым билетам.\n"
    "По абонементам местных жителей Гатчинский парк посетили 202 282 раза.\n"
)


def test_parse_anchored_labels_and_verbatim_evidence():
    records = parse_pages([PAGE_1, PAGE_2], document_sha256="abc")
    assert [r["value"] for r in records] == [728000, 202282]
    assert [r["unit"] for r in records] == ["человек", "раза"]
    assert [r["page"] for r in records] == [2, 2]
    assert [r["metric"] for r in records] == [
        "reported_total", "park_local_resident_subscription_visits"
    ]
    assert all(r["period"] == 2025 and r["status"] == "observed" for r in records)
    assert all(r["source_url"] == SOURCE_URL and r["document_sha256"] == "abc" for r in records)
    assert "728  000\nчеловек." in records[0]["excerpt"]
    assert records[1]["excerpt"] == "По абонементам местных жителей Гатчинский парк посетили 202 282 раза."
    assert all("overlap" in r["interpretation_caveat"] for r in records)


@pytest.mark.parametrize("separator", ["\u00a0", "\u202f"])
def test_nonbreaking_number_separators(separator):
    page = PAGE_2.replace("728  000", f"728{separator}000").replace("202 282", f"202{separator}282")
    assert [r["value"] for r in parse_pages([PAGE_1, page])] == [728000, 202282]


@pytest.mark.parametrize("pages", [
    [],
    [PAGE_2],
    [PAGE_1.replace("ГАТЧИНА", "ДРУГОЙ МУЗЕЙ"), PAGE_2],
    [PAGE_1, "Посетители: 728 000 человек; парк: 202 282 раза"],
    [PAGE_1, PAGE_2.replace("202 282 раза", "202 282 человек")],
    [PAGE_1, PAGE_2.replace("728  000", "728 000").replace("2025 году", "2024 году")],
    [PAGE_1, PAGE_2 + "\n" + PAGE_2],
    [PAGE_1, PAGE_2 + "\nПо абонементам местных жителей Гатчинский парк посетили 5 раза."],
])
def test_reject_missing_changed_or_ambiguous_schema(pages):
    with pytest.raises(ValueError):
        parse_pages(pages)


def test_pdf_signature_and_size_rejected_without_network():
    with pytest.raises(ValueError, match="invalid or oversized"):
        parse_pdf(b"not a PDF")
    with pytest.raises(ValueError, match="invalid or oversized"):
        parse_pdf(b"%PDF-" + b"x" * 10_000_000)


def test_fetch_rejects_non_pdf_response(monkeypatch):
    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size):
            yield b"not a pdf"

    def fake_get(url, **kwargs):
        assert url == SOURCE_URL
        assert kwargs == {"timeout": (10, 30), "stream": True, "verify": True,
                          "headers": {"User-Agent": USER_AGENT}}
        return Response()

    monkeypatch.setattr("src.etl.adapters.gatchina_report.requests.get", fake_get)
    with pytest.raises(ValueError, match="not a PDF"):
        fetch_pdf()

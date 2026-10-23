"""Two reported visitation measures from the Gatchina museum's 2025 report.

These are not unique tourists. The reported total and subscription park visits
have unknown overlap; do not add them or subtract them from other categories.
"""

import argparse
import hashlib
import io
import json
import re

import requests
from pypdf import PdfReader


SOURCE_URL = "https://gatchinapalace.ru/upload/medialibrary/936/otvk9ta8motx44bue1km8esd54k5cht1.pdf"
MAX_BYTES = 10_000_000
ADAPTER_VERSION = "gatchina-2025-v1"
USER_AGENT = "LeningradTourismFramework/0.1 (+https://github.com/KTL-deep/leningrad_tourism_model)"

_SECTION = re.compile(r"(?m)^\s*2\.\s*Сведения\s+о\s+посетителях\s+музея\s*$")
_TOTAL = re.compile(
    r"I\.\s*Общее\s+количество\s+посетителей\s+музея\s*-\s*заповедника"
    r"\s+в\s+2025\s+году\s+составило\s+(?P<number>\d[\d \u00a0\u202f]*)\s+"
    r"(?P<unit>человек)\s*\."
)
_SUBSCRIPTIONS = re.compile(
    r"По\s+абонементам\s+местных\s+жителей\s+Гатчинский\s+парк"
    r"\s+посетили\s+(?P<number>\d[\d \u00a0\u202f]*)\s+(?P<unit>раза)\s*\."
)


def parse_pages(page_texts, *, document_sha256=None):
    """Parse extracted PDF page strings; fail closed on missing/duplicate labels."""
    pages = list(page_texts)
    if not pages or not any(re.search(r"ЗА\s+2025\s+год", p) for p in pages):
        raise ValueError("2025 Gatchina report heading missing")
    if not any("ГАТЧИНА" in p.upper() for p in pages[:2]):
        raise ValueError("Gatchina publisher identity missing")

    sections = [(index, m) for index, text in enumerate(pages, 1)
                for m in _SECTION.finditer(text)]
    if len(sections) != 1:
        raise ValueError("expected exactly one visitor section")
    page_number, section = sections[0]
    text = pages[page_number - 1][section.end():]
    records = []
    for metric, pattern, caveat in (
        ("reported_total", _TOTAL,
         "Reported museum visitation, not unique tourists; overlap with park subscription visits unknown."),
        ("park_local_resident_subscription_visits", _SUBSCRIPTIONS,
         "Local-resident park subscription visits (times), not unique people or tourists; overlap with reported total unknown."),
    ):
        matches = list(pattern.finditer(text))
        if len(matches) != 1:
            raise ValueError(f"expected exactly one {metric} in visitor section")
        match = matches[0]
        records.append({
            "metric": metric,
            "value": int(re.sub(r"\s", "", match.group("number"))),
            "unit": match.group("unit"),
            "source_url": SOURCE_URL,
            "document_sha256": document_sha256,
            "page": page_number,
            "excerpt": match.group(0),
            "status": "observed",
            "period": 2025,
            "adapter_version": ADAPTER_VERSION,
            "interpretation_caveat": caveat,
        })
    return records


def parse_pdf(pdf_bytes):
    """Extract page text and attach the SHA-256 of the original PDF bytes."""
    if len(pdf_bytes) > MAX_BYTES or not pdf_bytes.startswith(b"%PDF-"):
        raise ValueError("invalid or oversized PDF")
    reader = PdfReader(io.BytesIO(pdf_bytes))
    if reader.is_encrypted:
        raise ValueError("encrypted PDF")
    return parse_pages(
        [page.extract_text() or "" for page in reader.pages],
        document_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )


def fetch_pdf():
    """Fetch the official document once, over verified TLS with a bounded body."""
    with requests.get(SOURCE_URL, timeout=(10, 30), stream=True, verify=True,
                      headers={"User-Agent": USER_AGENT}) as response:
        response.raise_for_status()
        if int(response.headers.get("Content-Length", "0")) > MAX_BYTES:
            raise ValueError("oversized PDF")
        data = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise ValueError("oversized PDF")
    if not data.startswith(b"%PDF-"):
        raise ValueError("response is not a PDF")
    return bytes(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    print(json.dumps(parse_pdf(fetch_pdf()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

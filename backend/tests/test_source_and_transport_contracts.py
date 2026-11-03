import asyncio
import ssl

from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from docx import Document

from app.services import embedding, parser, vlm
from app.core.config import provider_tls_context
from app.services.grounded_validation import validate_grounded_answer
from app.services.pdf_provenance import source_metadata


def test_docx_once_and_heading_offsets(tmp_path, monkeypatch):
    doc = Document()
    doc.add_paragraph("Before.")
    doc.add_heading("Heading A", level=1)
    p = doc.add_paragraph()
    p.add_run("Distinct ")
    p.add_run("body.")
    path = tmp_path / "source.docx"
    doc.save(path)
    monkeypatch.setattr(parser.settings, "IMAGE_DIR", str(tmp_path))
    monkeypatch.setattr(parser, "HAS_LANGCHAIN", False)
    split = parser.StructuredSplitter.split_by_headings
    observed = {}

    def capture(self, text, headings, make_id):
        observed["text"] = text
        observed["headings"] = [(h.text, text[h.start_pos:h.end_pos]) for h in headings]
        return split(self, text, headings, make_id)

    monkeypatch.setattr(parser.StructuredSplitter, "split_by_headings", capture)
    chunks = parser.FileParser("test").parse(str(path), chunk_mode="structure")
    for phrase in ("Before.", "Heading A", "Distinct body."):
        assert observed["text"].count(phrase) == 1
    assert observed["text"].index("Before.") < observed["text"].index("Heading A") < observed["text"].index("Distinct body.")
    assert observed["headings"] == [("Heading A", "Heading A")]
    assert sum(c.content.count("Distinct body.") for c in chunks) == 1


def test_child_is_not_pdf_supplement():
    answer = "收入为 190395.32 元。[1]"
    child = {"chunk_id": "c", "parent_chunk_id": "parent", "content": "收入为 190395.32 元。"}
    assert validate_grounded_answer(answer, [child], finish_reason="stop").status == "VERIFIED"
    supplementary = {**child, "source_type": "source_native_table_expansion"}
    assert validate_grounded_answer(answer, [supplementary], finish_reason="stop").status == "UNKNOWN"


def test_verified_ca_on_remote_image_call(tmp_path, monkeypatch):
    image = tmp_path / "synthetic.png"
    image.write_bytes(b"synthetic")
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.post.return_value = httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]},
                                              request=httpx.Request("POST", "https://example.invalid"))
    create = Mock(return_value=client)
    monkeypatch.setattr(vlm.httpx, "AsyncClient", create)
    asyncio.run(vlm.VLMService().describe_image(str(image)))
    verification = create.call_args.kwargs.get("verify", True)
    assert verification is True or isinstance(verification, ssl.SSLContext) and verification.check_hostname


def test_embedding_ca_override_and_sensitive_log(monkeypatch, capsys):
    import certifi
    import requests

    monkeypatch.setattr(embedding.settings, "PROVIDER_CA_BUNDLE", certifi.where())
    context = provider_tls_context()
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    client = AsyncMock()
    client.__aenter__.return_value = client
    request = httpx.Request("POST", "https://example.invalid")
    client.post.return_value = httpx.Response(200, json={"embedding": [1.0]}, request=request)
    create = Mock(return_value=client)
    monkeypatch.setattr(embedding.httpx, "AsyncClient", create)
    service = embedding.EmbeddingService(base_url="https://example.invalid", model="local-test")
    assert asyncio.run(service.embed_query("private-marker")) == [1.0]
    assert create.call_args.kwargs["verify"].check_hostname

    post = Mock(return_value=httpx.Response(200, json={"private-marker": "private-marker"}, request=request))
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(Exception):
        asyncio.run(service.embed_documents(["private-marker"], model_id="doubao-test"))
    assert post.call_args.kwargs["verify"] == certifi.where()
    assert "private-marker" not in capsys.readouterr().out


def test_spreadsheet_and_csv_source_locators(tmp_path, monkeypatch):
    from openpyxl import Workbook

    monkeypatch.setattr(parser.settings, "IMAGE_DIR", str(tmp_path / "images"))
    monkeypatch.setattr(parser.settings, "PDF_AUDIT_DIR", str(tmp_path / "audit"))
    p = parser.FileParser("temporary")
    path = tmp_path / "table.xlsx"
    wb = Workbook()
    sheet = wb.active
    sheet.title = "Quarter"
    sheet.append(["label", "value"])
    for i in range(22):
        sheet.append([f"item {i}", i])
    wb.save(path)
    wb.close()
    csv = tmp_path / "quoted.csv"
    csv.write_text('label,value\n"spans\ntwo lines",1\nlast,2\n', encoding="utf8")
    for source, chunks, expected in [
        (path, p._parse_excel(str(path)), {"sheet": "Quarter", "row_start": 22, "row_end": 23,
                                           "column_start": 1, "column_end": 2}),
        (csv, p._parse_csv(str(csv)), {"record_start": 1, "record_end": 2,
                                        "column_start": 1, "column_end": 2}),
    ]:
        c = chunks[-1]
        loc = source_metadata(source, c.content, c.page_number, c.content_type)
        assert loc["source_locators"] == [expected]
        assert all(loc[key] == value for key, value in expected.items())
        assert source_metadata(source, c.content + " edited", c.page_number, c.content_type) == {}

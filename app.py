from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google.oauth2 import service_account
from googleapiclient.discovery import build
from pypdf import PdfReader


BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="PDF Parser Demo")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def extract_pdf_data(file_bytes: bytes, filename: str) -> dict[str, Any]:
    try:
        reader = PdfReader(BytesIO(file_bytes))
    except Exception as exc:  # pragma: no cover - depends on malformed PDFs
        raise ValueError("PDF 文件无法解析，请确认文件未损坏。") from exc

    page_texts: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        page_texts.append(text.strip())

    full_text = "\n".join(text for text in page_texts if text).strip()
    full_text = full_text.replace('\x00', '')
    if not full_text:
        full_text = "未提取到可读文本，可能是扫描版 PDF，需要接入 OCR。"

    emails = sorted(set(re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", full_text)))
    phones = sorted(
        set(
            re.findall(
                r"(?:\+?\d{1,3}[-\s]?)?(?:\d{3,4}[-\s]?\d{3,4}[-\s]?\d{3,4}|\d{11})",
                full_text,
            )
        )
    )
    dates = sorted(
        set(
            re.findall(
                r"(?:20\d{2}[-/.年](?:0?[1-9]|1[0-2])[-/.月](?:0?[1-9]|[12]\d|3[01])日?)",
                full_text,
            )
        )
    )
    money_values = sorted(
        set(re.findall(r"(?:¥|￥|USD\s?|CNY\s?)?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?", full_text))
    )

    invoice_number = _search_first(
        full_text,
        [
            r"(?:Invoice\s*No\.?|发票号|票据号)[:：]?\s*([A-Za-z0-9-]+)",
            r"(?:编号|单号)[:：]?\s*([A-Za-z0-9-]{6,})",
        ],
    )
    title = (reader.metadata.title if reader.metadata else None) or _guess_title(page_texts)

    client_contact = None
    client_name = None

    docusign_patterns = [
        r"Docusign Envelope ID:[^\n]+\n(?:[^\n]*\n){0,5}\s*(\d{10,11}|(?:\d{3}[-\s]?\d{3}[-\s]?\d{4}))\s*\n\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?|[\u4e00-\u9fa5]+)\s*$",
        r"Docusign Envelope ID:[^\n]+\n\s*(\d{3}[-\s]?\d{3}[-\s]?\d{4})\s*\n\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*$",
        r"Docusign Envelope ID:[^\n]+\n\s*(\d{10})\s*\n.*?\n\s*([\u4e00-\u9fa5]+)\s*$",
        r"Docusign Envelope ID:[^\n]+\n\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s*\n\s*(\d{10,11}|(?:\d{3}[-\s]?\d{3}[-\s]?\d{4}))\s*$",
        r"Docusign Envelope ID:[^\n]+\n\s*([\u4e00-\u9fa5]+)\s*\n\s*(\d{10,11}|(?:\d{3}[-\s]?\d{3}[-\s]?\d{4}))\s*$",
    ]

    for pattern in docusign_patterns:
        docusign_match = re.search(pattern, full_text, re.MULTILINE)
        if docusign_match:
            # Determine which group is phone vs name based on pattern type
            if pattern == docusign_patterns[3] or pattern == docusign_patterns[4]:
                # name first, phone second -> swap
                client_name = docusign_match.group(1)
                client_contact = docusign_match.group(2)
            else:
                client_contact = docusign_match.group(1)
                client_name = docusign_match.group(2)
            break

    if not client_contact:
        client_contact = _search_first(
            full_text,
            [
                r"(?:委托人联系方式|联系电话|联系方式)[:：]?\s*([^\n\r]+)",
            ],
        )

    if not client_contact:
        phone_matches = re.findall(r"\(?\d{3}\)?[-\s]?\d{3}[-\s]?\d{4}", full_text)
        valid_phones = [p for p in phone_matches if not p.startswith('0') and not p.startswith('510') and len(p.replace('-', '').replace(' ', '').replace('(', '').replace(')', '')) == 10]
        if valid_phones:
            client_contact = valid_phones[-1].strip('()')

    if not client_name:
        client_name = _search_first(
            full_text,
            [
                r"(?:委托人|客户姓名|申请人|姓名)[:：]?\s*([^\n\r]+)",
                r"(?:Client|Name)[:：]?\s*([^\n\r]+)",
            ],
        )

    plan_name = _search_first(
        full_text,
        [
            r"研究生启航申请计划",
            r"研究生菁英申请计划",
            r"SVIP冲藤计划",
            r"本科VIP申请计划",
            r"研究生\s*启航\s*申请计划",
            r"研究生\s*菁英\s*申请计划",
            r"研究生\s*SVIP\s*冲藤\s*申请计划",
            r"SVIP\s*冲藤计划",
            r"本科\s*VIP\s*申请计划",
            r"选择的是[^\n]*?((?:[^\s\n]+(?:\s+[^\s\n]+){0,3})?(?:计划|申请计划))",
        ],
    )
    if plan_name:
        plan_name = ''.join(plan_name.split())

    base_fee = _search_first(
        full_text,
        [
            r"基础服务费为\s*([0-9,]+)",
            r"基础费用\s*([0-9,]+)",
            r"基础服务费\s*([0-9,]+)",
            r"总费用为\s*([0-9,]+)",
            r"服务总费用为\s*([0-9,]+)",
        ],
    )
    if base_fee:
        base_fee = base_fee.replace(',', '')

    down_payment = _search_first(
        full_text,
        [
            r"服务费定金\s*([0-9,]+)",
            r"第一笔服务费[^\d]*?([0-9,]+)\s*(?:美元|元|円)",
            r"首付[^\d]*?([0-9,]+)",
            r"定金\s*([0-9,]+)\s*美元",
        ],
    )
    if down_payment:
        down_payment = down_payment.replace(',', '')

    return {
        "filename": filename,
        "page_count": len(reader.pages),
        "title": title,
        "invoice_number": invoice_number,
        "emails": emails,
        "phones": phones,
        "dates": dates,
        "money_values": money_values[:20],
        "text_preview": full_text[:4000],
        "client_name": client_name,
        "client_contact": client_contact,
        "plan_name": plan_name,
        "base_fee": base_fee,
        "down_payment": down_payment,
    }


def _search_first(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            if len(match.groups()) >= 1 and match.group(1) is not None:
                return match.group(1).strip()
            else:
                return match.group(0).strip()
    return None


def _guess_title(page_texts: list[str]) -> str | None:
    for page_text in page_texts:
        for line in page_text.splitlines():
            cleaned = line.strip()
            if 4 <= len(cleaned) <= 60:
                return cleaned
    return None


SHEET_ID = "135QulOdaQZHcAGQHia4Bq-buJnloHsX5iATPuJYUuf8"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _get_sheets_service():
    creds = service_account.Credentials.from_service_account_file(
        BASE_DIR / "credentials.json", scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds)


def append_to_google_sheet(data: dict[str, Any], sheet_id: str | None = None, row_num: int | None = None) -> int | None:
    target_sheet_id = sheet_id or SHEET_ID
    service = _get_sheets_service()

    if row_num is None:
        result = service.spreadsheets().values().get(
            spreadsheetId=target_sheet_id, range="2026F!B:B"
        ).execute()
        values = result.get("values") or []
        row_num = len(values) + 1
        if row_num <= 3:
            row_num = 4

    student_id = row_num

    values = [[
        student_id,
        data.get("client_name") or "",
        "",
        data.get("plan_name") or "",
        None, None, None,
        None,
        int(data["base_fee"]) if data.get("base_fee") else None,
        None, None, None,
        int(data["down_payment"]) if data.get("down_payment") else None,
        None, None, None, None,
        f"=I{row_num}-M{row_num}",
    ]]

    body = {"values": values}
    service.spreadsheets().values().update(
        spreadsheetId=target_sheet_id,
        range=f"2026F!A{row_num}:R{row_num}",
        valueInputOption="USER_ENTERED",
        body=body
    ).execute()
    return row_num


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "result": None,
            "error": None,
        },
    )


@app.post("/upload", response_class=HTMLResponse)
async def upload_pdf(request: Request, file: UploadFile = File(...)) -> HTMLResponse:
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名。")
    if not file.filename.lower().endswith(".pdf"):
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "result": None,
                "error": "只支持上传 PDF 文件。",
            },
            status_code=400,
        )

    file_bytes = await file.read()
    if not file_bytes:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "result": None,
                "error": "上传文件为空。",
            },
            status_code=400,
        )

    try:
        result = extract_pdf_data(file_bytes, file.filename)
    except ValueError as exc:
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "result": None,
                "error": str(exc),
            },
            status_code=400,
        )

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "result": result,
            "error": None,
        },
    )


@app.post("/write-to-excel", response_class=HTMLResponse)
async def write_to_excel(request: Request) -> HTMLResponse:
    try:
        body = await request.json()
        result = body.get("result")
        if not result:
            return templates.TemplateResponse(
                "index.html",
                {"request": request, "result": None, "error": "缺少数据。"},
                status_code=400,
            )
        row_num = append_to_google_sheet(result, sheet_id=result.get("sheet_id"))
        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "result": result,
                "error": None,
                "written": True,
                "row_num": row_num,
            },
        )
    except Exception as exc:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "result": None, "error": str(exc)},
            status_code=500,
        )
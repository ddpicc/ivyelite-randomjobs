from __future__ import annotations

import json
import os
import re
import unicodedata
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib import error, request as urllib_request

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
        page_texts.append(_normalize_text(text))

    clean_lines = _build_clean_lines(page_texts)
    full_text = "\n".join(clean_lines).strip()
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
    title = (reader.metadata.title if reader.metadata else None) or _guess_title(clean_lines)
    client_name = _extract_client_name(full_text, clean_lines, filename)

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

    base_fee = _extract_base_fee(clean_lines, full_text)
    down_payment = _extract_down_payment(clean_lines, full_text)

    result = {
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
        "plan_name": plan_name,
        "base_fee": base_fee,
        "down_payment": down_payment,
    }
    refined = _maybe_refine_with_ai(full_text, filename, result)
    if refined:
        result.update({key: value for key, value in refined.items() if value})
    return result


def _search_first(text: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            if len(match.groups()) >= 1 and match.group(1) is not None:
                return match.group(1).strip()
            else:
                return match.group(0).strip()
    return None


def _normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\x00", "")
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _build_clean_lines(page_texts: list[str]) -> list[str]:
    lines: list[str] = []
    for page_text in page_texts:
        for raw_line in page_text.splitlines():
            cleaned = raw_line.strip(" \t\n\r\f\v\u3000")
            if not cleaned:
                continue
            cleaned = re.sub(r"\s+", " ", cleaned)
            lines.append(cleaned)
    return lines


def _guess_title(lines: list[str]) -> str | None:
    for cleaned in lines:
        if 4 <= len(cleaned) <= 60:
            return cleaned
    return None


def _extract_client_name(full_text: str, lines: list[str], filename: str) -> str | None:
    candidates: list[tuple[int, str]] = []

    regex_candidates = [
        _search_first(
            full_text,
            [
                r"客户姓名[:：]\s*([^\n\r]{1,40})",
                r"申请人[:：]\s*([^\n\r]{1,40})",
                r"姓名[:：]\s*([^\n\r]{1,40})",
                r"委托人[:：]?\s*([A-Za-z][A-Za-z .'-]{1,40}|[\u4e00-\u9fff·]{2,12})",
            ],
        )
    ]
    for candidate in regex_candidates:
        score = _score_name_candidate(candidate)
        if score > 0:
            candidates.append((score + 15, candidate.strip()))

    for index, line in enumerate(lines):
        if "Docusign Envelope ID" not in line:
            continue
        block = lines[index + 1: index + 6]
        for candidate in block:
            score = _score_name_candidate(candidate)
            if score <= 0:
                continue
            block_bonus = 50
            if any(_looks_like_phone_or_id(item) for item in block if item != candidate):
                block_bonus += 10
            candidates.append((score + block_bonus, candidate))

    for index, line in enumerate(lines):
        score = _score_name_candidate(line)
        if score <= 0:
            continue
        if len(line) > 20:
            continue
        bonus = 0
        window = lines[max(0, index - 2): min(len(lines), index + 3)]
        joined_window = " ".join(window)
        if any("Docusign Envelope ID" in item for item in window):
            bonus += 25
        if "委托人" in joined_window:
            bonus += 10
        if index > 0 and "Docusign Envelope ID" in lines[index - 1]:
            bonus += 30
        if index > 1 and "Docusign Envelope ID" in lines[index - 2]:
            bonus += 20
        candidates.append((score + bonus, line))

    filename_candidate = _extract_name_from_filename(filename)
    filename_score = _score_name_candidate(filename_candidate)
    if filename_score > 0:
        candidates.append((filename_score + 5, filename_candidate.strip()))

    if not candidates:
        return None

    best_score, best_name = max(candidates, key=lambda item: (item[0], len(item[1])))
    if best_score < 40:
        return None
    if filename_candidate and _should_prefer_filename_name(best_name, filename_candidate):
        return filename_candidate.strip()
    return best_name


def _extract_name_from_filename(filename: str) -> str | None:
    stem = Path(filename).stem
    stem = re.sub(r"^Copy of\s*", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"^请使用\s*Docusign\s*签署[:：]\s*", "", stem, flags=re.IGNORECASE)
    stem = stem.replace("_-_", "-")
    parts = [part.strip() for part in re.split(r"[-—]+", stem) if part.strip()]
    if not parts:
        return None
    candidate = parts[-1]
    candidate = re.sub(r"\bpdf\b$", "", candidate, flags=re.IGNORECASE).strip()
    candidate = candidate.replace("_", " ")
    return candidate or None


def _score_name_candidate(candidate: str | None) -> int:
    if not candidate:
        return 0
    raw = candidate.strip()
    text = raw.strip(" :：,，")
    if len(text) < 2 or len(text) > 40:
        return 0
    if re.search(r"\d", text):
        return 0
    if sum(ch.isalpha() for ch in text) == 0 and not re.search(r"[\u4e00-\u9fff]", text):
        return 0

    invalid_fragments = [
        "委托人", "受托人", "通讯地址", "联系方式", "护照号码", "合同", "申请计划", "服务费", "地址",
        "项目", "美国", "中国", "签字", "日期", "第一条", "第二条", "第三条", "第四条", "第五条",
        "第六条", "第七条", "第八条", "第九条", "当事人", "留学", "面试", "录取", "Bank", "Name:",
        "法律", "效力", "文件", "协议", "条款", "关文件",
        "常青藤精英教育", "Ivy Elite", "Yifei Sang",
    ]
    if any(fragment in text for fragment in invalid_fragments):
        return 0
    if re.search(r"[,:：;；。()（）/]", raw):
        return 0

    if re.fullmatch(r"[\u4e00-\u9fff·]{2,8}", text):
        return 85
    if re.fullmatch(r"[A-Z][a-z]+(?: [A-Z][a-z]+){1,3}", text):
        return 80
    if re.fullmatch(r"[A-Z][a-z]+", text):
        return 55
    if re.fullmatch(r"[A-Za-z]+(?: [A-Za-z]+){1,3}", text):
        return 45
    return 0


def _extract_base_fee(lines: list[str], full_text: str) -> str | None:
    for line in lines:
        for pattern in [
            r"基础服务费(?:为)?\s*([0-9,]+)\s*美元",
            r"基础费用\s*([0-9,]+)\s*美元",
            r"服务总费用为\s*([0-9,]+)\s*美元",
            r"总费用为\s*([0-9,]+)\s*美元",
        ]:
            match = re.search(pattern, line, flags=re.IGNORECASE)
            if match:
                return match.group(1).replace(",", "")

    match = re.search(r"总费用为\s*([0-9,]+)\s*美元", full_text, flags=re.IGNORECASE)
    if match:
        return match.group(1).replace(",", "")
    return None


def _extract_down_payment(lines: list[str], full_text: str) -> str | None:
    for line in lines:
        for pattern in [
            r"服务费定金\s*([0-9,]+)\s*美元",
            r"支付第一笔(?:服务费)?(?:定金)?\s*([0-9,]+)\s*美元",
            r"第一笔[^\d]{0,20}([0-9,]+)\s*美元",
            r"定金\s*([0-9,]+)\s*美元",
            r"首付[^\d]*([0-9,]+)\s*美元",
        ]:
            match = re.search(pattern, line, flags=re.IGNORECASE)
            if match:
                return match.group(1).replace(",", "")

    match = re.search(r"定金\s*([0-9,]+)\s*美元", full_text, flags=re.IGNORECASE)
    if match:
        return match.group(1).replace(",", "")
    return None


def _looks_like_phone_or_id(text: str) -> bool:
    cleaned = text.strip()
    return bool(
        re.fullmatch(r"(?:\+?\d[\d -]{7,}|\d{8,}|[A-Z]{1,3}\d{6,10})", cleaned)
    )


def _should_prefer_filename_name(extracted_name: str, filename_name: str) -> bool:
    extracted = extracted_name.strip()
    filename_clean = filename_name.strip()
    if not extracted or not filename_clean:
        return False
    if extracted == filename_clean:
        return False
    if re.fullmatch(r"[A-Z][a-z]+", extracted) and re.fullmatch(
        r"[A-Z][a-z]+(?: [A-Z][a-z]+){1,3}", filename_clean
    ):
        filename_parts = filename_clean.split()
        return extracted in filename_parts
    return False


def _maybe_refine_with_ai(full_text: str, filename: str, result: dict[str, Any]) -> dict[str, str] | None:
    if os.environ.get("PDF_EXTRACT_ENABLE_AI_REFINE", "").lower() not in {"1", "true", "yes"}:
        return None
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    needs_refine = (
        not result.get("client_name")
        or not result.get("plan_name")
        or not result.get("base_fee")
        or not result.get("down_payment")
    )
    if not needs_refine:
        return None

    model = os.environ.get("PDF_EXTRACT_REFINE_MODEL")
    if not model:
        return None

    prompt = {
        "filename": filename,
        "current_result": {
            "client_name": result.get("client_name"),
            "plan_name": result.get("plan_name"),
            "base_fee": result.get("base_fee"),
            "down_payment": result.get("down_payment"),
        },
        "task": "从合同文本中修正字段。只返回JSON，字段仅包含 client_name, plan_name, base_fee, down_payment。拿不准就返回空字符串，不要猜。",
        "text": full_text[:12000],
    }
    body = json.dumps(
        {
            "model": model,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": "你是严谨的合同信息提取器，只能从原文中提取，不得脑补。",
                },
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ],
        }
    ).encode("utf-8")
    req = urllib_request.Request(
        os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(req, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (error.URLError, TimeoutError, json.JSONDecodeError):
        return None

    content = payload.get("choices", [{}])[0].get("message", {}).get("content")
    if not content:
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return None
    return {
        "client_name": data.get("client_name", "").strip(),
        "plan_name": data.get("plan_name", "").strip(),
        "base_fee": re.sub(r"[^\d]", "", data.get("base_fee", "")),
        "down_payment": re.sub(r"[^\d]", "", data.get("down_payment", "")),
    }


SHEET_ID = "135QulOdaQZHcAGQHia4Bq-buJnloHsX5iATPuJYUuf8"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _get_sheets_service():
    creds_info = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if creds_info:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(creds_info), scopes=SCOPES
        )
    else:
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

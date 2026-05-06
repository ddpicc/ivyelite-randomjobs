# PDF Parser Demo

一个最小可运行的 `FastAPI + PDF 上传解析` 示例项目。

## 功能

- 网页上传 PDF
- 服务端解析 PDF 文本
- 提取基础字段
  - 标题
  - 发票号候选
  - 邮箱
  - 电话
  - 日期
  - 金额
- 页面展示解析结果

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 启动

```bash
uvicorn app:app --reload
```

浏览器打开：

```text
http://127.0.0.1:8000
```

## 说明

- 当前使用 `pypdf` 提取文本。
- 如果 PDF 是扫描件，通常提取不到文本，需要接入 OCR，例如 `paddleocr`、`tesseract`。
- 现在的字段抽取是通用规则，适合演示和基础原型。
- 如果你已经有明确字段，比如“合同编号、甲方、乙方、金额、签署日期”，建议把 `extract_pdf_data()` 改成定向解析逻辑。

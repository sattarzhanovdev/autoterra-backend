"""Выгрузка списка клиентов в Excel, Word, PDF и CSV.

Один источник колонок — ``CLIENT_COLUMNS``: во всех четырёх форматах состав и
порядок полей совпадают, так что выгрузки можно сверять между собой.

Кириллица в PDF требует шрифта с нужными глифами: встроенные в reportlab
Helvetica и Vera её не содержат, поэтому рядом лежит DejaVuSans.
"""

import csv
import io
import logging
import os
from datetime import datetime

from django.http import HttpResponse
from django.utils import timezone

logger = logging.getLogger(__name__)

FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "fonts")
PDF_FONT = "DejaVuSans"
PDF_FONT_BOLD = "DejaVuSans-Bold"

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _tier(client):
    return client.partner_status or "—"


def _money(value):
    return f"{value or 0:,.0f}".replace(",", " ")


# (заголовок, как достать значение, ширина колонки в Excel)
CLIENT_COLUMNS = [
    ("Компания", lambda c: c.company_name, 32),
    ("ИНН", lambda c: c.inn, 14),
    ("Ранг", _tier, 12),
    ("Оборот, ₽", lambda c: _money(c.total_purchases), 14),
    ("Контакт", lambda c: c.contact_name, 22),
    ("Телефон", lambda c: c.phone, 16),
    ("Email", lambda c: getattr(c.user, "email", "") or "", 24),
    ("Город", lambda c: c.city, 18),
    ("Регион", lambda c: c.region.name if c.region_id else "", 18),
    ("Дистрибьютор", lambda c: c.distributor.name if c.distributor_id else "", 22),
    ("Менеджер", lambda c: c.manager.get_full_name() or c.manager.username if c.manager_id else "", 20),
    ("Статус", lambda c: c.get_status_display(), 14),
    ("Тип бизнеса", lambda c: c.get_category_display(), 28),
    ("Регистрация", lambda c: c.created_at.strftime("%d.%m.%Y") if c.created_at else "", 14),
]

# Для PDF колонок слишком много — берём то, что нужно на печати.
PDF_COLUMN_KEYS = ["Компания", "ИНН", "Ранг", "Оборот, ₽", "Контакт", "Телефон", "Город", "Статус"]


def _rows(clients):
    """Матрица значений: строка на клиента, порядок как в CLIENT_COLUMNS."""
    return [[getter(client) for _, getter, _ in CLIENT_COLUMNS] for client in clients]


def _filename(extension):
    return f"autoterra-clients-{timezone.localtime():%Y-%m-%d}.{extension}"


def _attachment(content, mime, extension):
    response = HttpResponse(content, content_type=mime)
    response["Content-Disposition"] = f'attachment; filename="{_filename(extension)}"'
    return response


# ── Excel ─────────────────────────────────────────────────────────────────────

def clients_to_xlsx(clients) -> HttpResponse:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Клиенты"

    headers = [title for title, _, _ in CLIENT_COLUMNS]
    sheet.append(headers)

    header_fill = PatternFill("solid", fgColor="1F1F1F")
    for index, title in enumerate(headers, start=1):
        cell = sheet.cell(row=1, column=index)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center")
        sheet.column_dimensions[get_column_letter(index)].width = CLIENT_COLUMNS[index - 1][2]

    for row in _rows(clients):
        sheet.append(row)

    # Шапка остаётся на экране при прокрутке, по колонкам — фильтры.
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(sheet.max_row, 1)}"

    return _attachment(_workbook_bytes(workbook), XLSX_MIME, "xlsx")


def _workbook_bytes(workbook):
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# ── CSV ───────────────────────────────────────────────────────────────────────

def clients_to_csv(clients) -> HttpResponse:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow([title for title, _, _ in CLIENT_COLUMNS])
    writer.writerows(_rows(clients))

    # BOM — иначе Excel открывает кириллицу кракозябрами.
    content = "﻿" + buffer.getvalue()
    return _attachment(content.encode("utf-8"), "text/csv; charset=utf-8", "csv")


# ── Word ──────────────────────────────────────────────────────────────────────

def clients_to_docx(clients) -> HttpResponse:
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    document = Document()

    # Колонок много — разворачиваем страницу горизонтально.
    section = document.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = section.page_height, section.page_width

    heading = document.add_heading("Список клиентов AutoTerra", level=1)
    heading.alignment = WD_ALIGN_PARAGRAPH.LEFT

    subtitle = document.add_paragraph(
        f"Выгружено {timezone.localtime():%d.%m.%Y %H:%M} · всего: {len(clients)}"
    )
    subtitle.runs[0].font.size = Pt(9)

    headers = [title for title, _, _ in CLIENT_COLUMNS]
    table = document.add_table(rows=1, cols=len(headers))
    table.style = "Light Grid Accent 1"

    for index, title in enumerate(headers):
        cell = table.rows[0].cells[index]
        cell.text = title
        for paragraph in cell.paragraphs:
            for run in paragraph.runs:
                run.font.bold = True
                run.font.size = Pt(8)

    for row in _rows(clients):
        cells = table.add_row().cells
        for index, value in enumerate(row):
            cells[index].text = str(value)
            for paragraph in cells[index].paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(8)

    buffer = io.BytesIO()
    document.save(buffer)
    return _attachment(buffer.getvalue(), DOCX_MIME, "docx")


# ── PDF ───────────────────────────────────────────────────────────────────────

def _register_pdf_fonts():
    """Подключает DejaVu: без него кириллица в PDF станет чёрными квадратами."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if PDF_FONT in pdfmetrics.getRegisteredFontNames():
        return True
    try:
        pdfmetrics.registerFont(TTFont(PDF_FONT, os.path.join(FONT_DIR, "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont(PDF_FONT_BOLD, os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")))
        return True
    except Exception:
        logger.exception("Не удалось подключить шрифт для PDF из %s", FONT_DIR)
        return False


def clients_to_pdf(clients) -> HttpResponse:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

    has_cyrillic_font = _register_pdf_fonts()
    body_font = PDF_FONT if has_cyrillic_font else "Helvetica"
    bold_font = PDF_FONT_BOLD if has_cyrillic_font else "Helvetica-Bold"

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=12 * mm, rightMargin=12 * mm,
        topMargin=12 * mm, bottomMargin=12 * mm,
        title="Список клиентов AutoTerra",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "TitleRu", parent=styles["Title"], fontName=bold_font, fontSize=16, alignment=TA_LEFT,
    )
    meta_style = ParagraphStyle(
        "MetaRu", parent=styles["Normal"], fontName=body_font, fontSize=8, textColor=colors.grey,
    )
    cell_style = ParagraphStyle(
        "CellRu", parent=styles["Normal"], fontName=body_font, fontSize=7, leading=9,
    )
    head_style = ParagraphStyle(
        "HeadRu", parent=cell_style, fontName=bold_font, textColor=colors.white,
    )

    indexes = [
        next(i for i, (title, _, _) in enumerate(CLIENT_COLUMNS) if title == key)
        for key in PDF_COLUMN_KEYS
    ]

    data = [[Paragraph(key, head_style) for key in PDF_COLUMN_KEYS]]
    for row in _rows(clients):
        data.append([Paragraph(str(row[i]), cell_style) for i in indexes])

    table = Table(data, repeatRows=1, colWidths=_pdf_col_widths(document))
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F1F1F")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#CCCCCC")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F6F6")]),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))

    story = [
        Paragraph("Список клиентов AutoTerra", title_style),
        Paragraph(
            f"Выгружено {timezone.localtime():%d.%m.%Y %H:%M} &nbsp;·&nbsp; всего: {len(clients)}",
            meta_style,
        ),
        Table([[""]], colWidths=[1], rowHeights=[6]),  # отступ перед таблицей
        table,
    ]
    document.build(story)
    return _attachment(buffer.getvalue(), "application/pdf", "pdf")


def _pdf_col_widths(document):
    """Ширины колонок пропорционально их содержимому."""
    weights = [4.0, 2.0, 1.4, 1.8, 2.6, 2.0, 2.0, 1.6]
    available = document.width
    total = sum(weights)
    return [available * weight / total for weight in weights]


EXPORTERS = {
    "xlsx": clients_to_xlsx,
    "docx": clients_to_docx,
    "pdf": clients_to_pdf,
    "csv": clients_to_csv,
}


def export_clients(clients, fmt: str) -> HttpResponse:
    """Единая точка входа: ``export_clients(queryset, "pdf")``."""
    exporter = EXPORTERS.get(fmt)
    if exporter is None:
        raise ValueError(f"Неизвестный формат выгрузки: {fmt}")
    return exporter(list(clients))

"""Render DESIGN.md as a two-page brief with a vector flowchart and native tables.

Optional dependency: requirements-docs.txt. Markdown remains the content source;
the Mermaid block is replaced with the print-friendly lifecycle drawing below.
"""

from html import escape
from pathlib import Path
import re

import reportlab
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Flowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)


ROOT = Path(__file__).resolve().parents[1]
WIDTH = A4[0] - 32 * mm
NAVY, TEAL = colors.HexColor("#123341"), colors.HexColor("#087e83")
INK, MUTED = colors.HexColor("#243d49"), colors.HexColor("#5c717d")
RULE, PALE = colors.HexColor("#d9e4e8"), colors.HexColor("#f1f6f8")
MINT, WHITE = colors.HexColor("#e9f6f3"), colors.white


def register_fonts():
    # Segoe UI on Windows; Vera ships with ReportLab for portable regeneration.
    system = Path("C:/Windows/Fonts")
    bundled = Path(reportlab.__file__).parent / "fonts"
    regular, bold = system / "segoeui.ttf", system / "segoeuib.ttf"
    if not regular.exists() or not bold.exists():
        regular, bold = bundled / "Vera.ttf", bundled / "VeraBd.ttf"
    pdfmetrics.registerFont(TTFont("DesignSans", str(regular)))
    pdfmetrics.registerFont(TTFont("DesignSans-Bold", str(bold)))
    pdfmetrics.registerFontFamily("DesignSans", normal="DesignSans", bold="DesignSans-Bold",
                                  italic="DesignSans", boldItalic="DesignSans-Bold")


def inline(value):
    value = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", escape(value))
    return re.sub(r"`(.*?)`", r'<font name="Courier">\1</font>', value)


class Hero(Flowable):
    def __init__(self, page=1):
        super().__init__()
        self.width, self.height, self.page = WIDTH, 72, page

    def draw(self):
        c = self.canv
        c.setFillColor(NAVY)
        c.roundRect(0, 0, self.width, self.height, 7, fill=1, stroke=0)
        c.setFillColor(colors.HexColor("#a9dcda"))
        c.setFont("DesignSans-Bold", 7.6)
        c.drawString(16, self.height - 16, "FRAMSIKT  /  PRODUCT ENGINEER TAKE-HOME")
        c.setFillColor(WHITE)
        c.setFont("DesignSans-Bold", 23 if self.page == 1 else 21)
        c.drawString(16, self.height - 42, "Snack Counter Automation" if self.page == 1
                     else "Money, recovery & scope")
        c.setFillColor(colors.HexColor("#c7dade"))
        c.setFont("DesignSans", 8.3)
        c.drawString(16, 10, "Design brief  |  Snack Counter Automation" if self.page == 1
                     else "Explicit rules. Conservative recovery. Deliberate limits.")


class Section(Flowable):
    def __init__(self, title):
        super().__init__()
        self.width, self.height, self.title = WIDTH, 26, title
        self.keepWithNext = True

    def draw(self):
        c = self.canv
        c.setFillColor(TEAL)
        c.roundRect(0, 6, 3, 12, 1, fill=1, stroke=0)
        c.setFont("DesignSans-Bold", 11)
        c.setFillColor(NAVY)
        c.drawString(10, 8, self.title)
        end = 21 + pdfmetrics.stringWidth(self.title, "DesignSans-Bold", 11)
        c.setStrokeColor(RULE)
        c.setLineWidth(0.6)
        c.line(end, 11, self.width, 11)


class OfficeCards(Flowable):
    def __init__(self, rows):
        super().__init__()
        self.width, self.height, self.rows = WIDTH, 65, rows

    def draw(self):
        c = self.canv
        gap, width = 12, (self.width-12)/2
        for index, (office, volunteers, budget, typical) in enumerate(self.rows):
            x = index*(width+gap)
            c.setFillColor(PALE)
            c.roundRect(x, 2, width, 60, 5, fill=1, stroke=0)
            c.setFont("DesignSans-Bold", 9.2)
            c.setFillColor(NAVY)
            c.drawString(x+12, 47, office.upper())
            c.setFont("DesignSans", 8)
            c.setFillColor(MUTED)
            c.drawRightString(x+width-12, 47, f"{volunteers} volunteers")
            c.setFont("DesignSans-Bold", 19)
            c.setFillColor(TEAL)
            c.drawString(x+12, 25, budget)
            c.setFont("DesignSans", 8)
            c.setFillColor(MUTED)
            c.drawString(x+12, 11, f"Monthly cap  |  Typical purchase {typical}")


class Lifecycle(Flowable):
    """Read left-to-right, then return along the lower row; all marks are vectors."""

    def __init__(self):
        super().__init__()
        self.width, self.height = WIDTH, 121

    def draw(self):
        c = self.canv
        gap, box_h = 13, 42
        box_w = (self.width-3*gap)/4
        xs = [i*(box_w+gap) for i in range(4)]
        top, bottom = 73, 9

        def arrow(x1, y1, x2, y2):
            c.setStrokeColor(TEAL)
            c.setFillColor(TEAL)
            c.setLineWidth(1)
            c.line(x1, y1, x2, y2)
            if x2 > x1:
                points = [(x2, y2), (x2-3, y2+2.2), (x2-3, y2-2.2)]
            elif x2 < x1:
                points = [(x2, y2), (x2+3, y2+2.2), (x2+3, y2-2.2)]
            else:
                points = [(x2, y2), (x2-2.2, y2+3), (x2+2.2, y2+3)]
            path = c.beginPath()
            path.moveTo(*points[0])
            for point in points[1:]:
                path.lineTo(*point)
            path.close()
            c.drawPath(path, fill=1, stroke=0)

        def node(index, y, title, caption, paid=False):
            x = xs[index]
            c.setFillColor(MINT if paid else PALE)
            c.roundRect(x, y, box_w, box_h, 5, fill=1, stroke=0)
            c.setFillColor(TEAL if paid else NAVY)
            c.setFont("DesignSans-Bold", 9.4)
            c.drawCentredString(x+box_w/2, y+25, title)
            c.setFont("DesignSans", 7.8)
            c.setFillColor(MUTED)
            c.drawCentredString(x+box_w/2, y+11, caption)

        for i in range(3):
            arrow(xs[i]+box_w+2, top+box_h/2, xs[i+1]-2, top+box_h/2)
        arrow(xs[3]+box_w/2, top-2, xs[3]+box_w/2, bottom+box_h+2)
        for i in (3, 2):
            arrow(xs[i]-2, bottom+box_h/2, xs[i-1]+box_w+2, bottom+box_h/2)
        for i, title, caption in [
            (0, "Empty / OPEN", "Deduplicate reports"),
            (1, "OFFERED", "Reserve cap · 4h"),
            (2, "COMMITTED", "One buyer · 24h target"),
            (3, "PURCHASED", "Stock + receipt"),
        ]:
            node(i, top, title, caption)
        node(3, bottom, "HR SUBMITTED", "Claim ID saved")
        node(2, bottom, "HR APPROVED", "Still awaiting payment")
        node(1, bottom, "REIMBURSED", "Payment confirmed", paid=True)
        c.setFont("DesignSans", 7.8)
        c.setFillColor(MUTED)
        for i, line in enumerate(("After stocking, the next", "empty report can start.", "HR continues separately.")):
            c.drawString(2, bottom+28-i*11, line)


class BudgetGate(Flowable):
    def __init__(self):
        super().__init__()
        self.width, self.height = WIDTH, 58

    def draw(self):
        c = self.canv
        c.setFillColor(MINT)
        c.roundRect(0, 3, self.width, 53, 5, fill=1, stroke=0)
        c.setFillColor(TEAL)
        c.setFont("DesignSans-Bold", 7.5)
        c.drawString(12, 42, "ATOMIC BUDGET CHECK  /  BEFORE A BUYER IS OFFERED")
        c.setFillColor(NAVY)
        c.setFont("DesignSans-Bold", 11)
        c.drawString(12, 26, "Purchased + reserved + requested cap ≤ office monthly budget")
        c.setFont("DesignSans", 8.1)
        c.setFillColor(MUTED)
        c.drawString(12, 12, "Actual purchase ≤ cap  ·  Integer minor units  ·  NOK and INR checked separately")


def make_table(header, rows, styles):
    if "Monthly budget" in header:
        return OfficeCards(rows)
    first = 0.27 if header[0] == "Part" else 0.28
    data = [[Paragraph(inline(value), styles["table_header"]) for value in header]]
    for row in rows:
        data.append([Paragraph(inline(value), styles["table_label"] if i == 0 else styles["table"])
                     for i, value in enumerate(row)])
    table = Table(data, colWidths=[WIDTH*first, WIDTH*(1-first)], repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [PALE, WHITE]),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, RULE),
    ]))
    return table


def build():
    register_fonts()
    body = ParagraphStyle("Body", fontName="DesignSans", fontSize=9.25,
                          leading=12.2, textColor=INK, spaceAfter=4.5)
    styles = {
        "body": body,
        "bullet": ParagraphStyle("Bullet", parent=body, leftIndent=10, bulletIndent=0,
                                  bulletFontName="DesignSans", bulletFontSize=8),
        "lead": ParagraphStyle("Lead", parent=body, fontSize=10.5, leading=14,
                                spaceBefore=7, textColor=TEAL),
        "table": ParagraphStyle("Table", parent=body, fontSize=8.5, leading=11.2, spaceAfter=0),
        "table_label": ParagraphStyle("TableLabel", parent=body, fontName="DesignSans-Bold",
                                      fontSize=8.5, leading=11.2, spaceAfter=0),
        "table_header": ParagraphStyle("TableHeader", parent=body, fontName="DesignSans-Bold",
                                       fontSize=8.4, leading=11.2, textColor=WHITE, spaceAfter=0),
    }
    lines = (ROOT / "DESIGN.md").read_text(encoding="utf-8").splitlines()
    story, index = [], 0
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line or line == "Framsikt | Design brief":
            continue
        if line.startswith("# "):
            story.append(Hero())
        elif line.startswith("## "):
            title = line[3:]
            if title == "Money and reporting":
                story.extend([PageBreak(), Hero(page=2), Spacer(1, 4)])
            story.append(Section(title))
        elif line.startswith("```mermaid"):
            while index < len(lines) and not lines[index].startswith("```"):
                index += 1
            index += 1
            story.append(Lifecycle())
        elif line.startswith("|"):
            grid = [line]
            while index < len(lines) and lines[index].strip().startswith("|"):
                grid.append(lines[index].strip())
                index += 1
            cells = [[v.strip() for v in row.strip("|").split("|")] for row in grid]
            story.extend([make_table(cells[0], cells[2:], styles), Spacer(1, 3)])
        elif line.startswith("- **Atomic budget gate:**"):
            story.append(BudgetGate())
        elif line.startswith("- "):
            story.append(Paragraph(inline(line[2:]), styles["bullet"], bulletText="•"))
        elif line.startswith("**One accountable"):
            story.append(Paragraph(inline(line), styles["lead"]))
        else:
            story.append(Paragraph(inline(line), styles["body"]))

    doc = SimpleDocTemplate(str(ROOT / "DESIGN.pdf"), pagesize=A4,
                            rightMargin=16*mm, leftMargin=16*mm,
                            topMargin=13*mm, bottomMargin=15*mm,
                            title="Snack Counter Automation - Design", author="")

    def footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(RULE)
        canvas.setLineWidth(0.6)
        canvas.line(16*mm, 12*mm, A4[0]-16*mm, 12*mm)
        canvas.setFont("DesignSans", 7.5)
        canvas.setFillColor(MUTED)
        canvas.drawString(16*mm, 7.5*mm, "Framsikt  /  Snack Counter Automation")
        canvas.drawRightString(A4[0]-16*mm, 7.5*mm, f"{document.page} / 2")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    print(ROOT / "DESIGN.pdf")


if __name__ == "__main__":
    build()

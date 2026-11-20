"""Export sourced Russian terminology to searchable PDFs and Qdrant payloads."""
import argparse
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from xml.sax.saxutils import escape

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


SOURCE_DIR = Path(__file__).resolve().parents[1] / "manager-demo" / "web"
PDF_NAMES = {
    "striking": "01-udarnye-discipliny.pdf",
    "grappling": "02-borba-i-mma.pdf",
    "traditional": "03-traditsionnye-discipliny.pdf",
    "shared": "04-obshchie-i-spornye-terminy.pdf",
}
INK = "#142639"
BLUE = "#24547B"
PALE = "#F2F6FA"
LINE = "#D9E3EC"


def load(name: str) -> dict:
    return json.loads((SOURCE_DIR / name).read_text(encoding="utf-8"))


def record(key: str, scope: str, title: str, text: str, aliases: list[str],
           urls: list[str], kind: str, reviewed_at: str) -> dict:
    point_id = str(uuid5(NAMESPACE_URL, f"https://hard-pro.ru/rag/combat-terminology/{key}"))
    return {"id": point_id, "payload": {
        "id": key, "scope": scope, "title": title, "text": text, "aliases": aliases,
        "source": urls[0], "source_urls": urls, "kind": kind, "lang": "ru", "reviewed_at": reviewed_at,
    }}


def corpus(data: dict, existing: dict):
    sources = data["sources"]
    reviewed_at = data["reviewed_at"]
    for group in data["groups"]:
        group_urls = [sources[key]["url"] for key in group["sources"]]
        names = ", ".join(group["names"])
        yield record(f"discipline-{group['id']}", group["id"], group["title"],
                     f"Обозначения — {names}. {group['scope']}", group["names"], group_urls, "discipline", reviewed_at)
        for term in group["terms"]:
            urls = [sources[key]["url"] for key in term.get("sources", group["sources"])]
            aliases = ", ".join(term["aliases"])
            yield record(f"term-{term['id']}", group["id"], term["term"],
                         f"{group['title']}. Варианты запроса — {aliases}. {term['note']}",
                         term["aliases"], urls, "term", reviewed_at)
    for item in data["ambiguities"]:
        urls = [sources[key]["url"] for key in item["sources"]]
        choices = " ".join(f"{case['term']} — {', '.join(case['cues'])}." for case in item["cases"])
        yield record(f"context-{item['id']}", "ambiguity", ", ".join(item["aliases"]),
                     f"{item['question']} {choices}", item["aliases"], urls, "ambiguity", reviewed_at)
    for rule in existing["rules"]:
        slug = uuid5(NAMESPACE_URL, rule["name"]).hex[:16]
        yield record(f"rule-{slug}", "competition-rules", rule["name"],
                     f"{rule['summary']} Источник — {rule['document']}.",
                     rule["aliases"], [rule["url"]], "competition_rule", reviewed_at)


def doc_styles() -> dict[str, ParagraphStyle]:
    base = dict(textColor=HexColor(INK), fontName="Text", fontSize=10, leading=14)
    return {
        "title": ParagraphStyle("title", **(base | {"fontName": "Bold", "fontSize": 28, "leading": 33, "spaceAfter": 12})),
        "h1": ParagraphStyle("h1", **(base | {"fontName": "Bold", "fontSize": 21, "leading": 25, "spaceAfter": 10})),
        "h2": ParagraphStyle("h2", **(base | {"fontName": "Bold", "fontSize": 11, "leading": 14, "spaceBefore": 12, "spaceAfter": 4})),
        "body": ParagraphStyle("body", **base),
        "small": ParagraphStyle("small", **(base | {"fontSize": 8.5, "leading": 12, "textColor": HexColor(BLUE)})),
        "cell": ParagraphStyle("cell", **(base | {"fontSize": 9.25, "leading": 12.6})),
        "head": ParagraphStyle("head", **(base | {"fontName": "Bold", "fontSize": 9.25, "textColor": HexColor("#FFFFFF")})),
    }


def printable(text: str) -> str:
    return escape(text.translate(str.maketrans("‐‑–—", "----")))


def source_link(source: dict, index: int) -> str:
    url = escape(source["url"], {'"': "&quot;"})
    return f'<link href="{url}" color="{BLUE}">[{index}] {printable(source["title"])}</link>'


def group_page(group: dict, sources: dict, styles: dict) -> list:
    refs = list(dict.fromkeys(group["sources"] + [
        key for term in group["terms"] for key in term.get("sources", [])
    ]))
    indexes = {key: i for i, key in enumerate(refs, 1)}
    body = [
        Paragraph(printable(group["title"]), styles["h1"]),
        Paragraph(printable(", ".join(group["names"])), styles["small"]),
        Spacer(1, 9),
        Paragraph(printable(group["scope"]), styles["body"]),
        Paragraph("Источники", styles["h2"]),
    ]
    body.extend(Paragraph(source_link(sources[key], indexes[key]), styles["small"]) for key in refs)
    body.append(Spacer(1, 10))
    rows = [[Paragraph(f'{printable(group["title"])}<br/>Термин и варианты запроса', styles["head"]),
             Paragraph("Значение и подбор", styles["head"])]]
    for term in group["terms"]:
        names = printable(", ".join(term["aliases"]))
        term_refs = term.get("sources", group["sources"])
        citations = " ".join(f"[{indexes[key]}]" for key in term_refs)
        note = printable(term["note"])
        if term.get("attested"):
            note += " Зафиксировано в источниках: " + printable(", ".join(term["attested"])) + "."
        rows.append([
            Paragraph(f'<font name="Bold">{printable(term["term"])}</font><br/>{names}', styles["cell"]),
            Paragraph(f'{note} <font color="{BLUE}">{citations}</font>', styles["cell"]),
        ])
    table = Table(rows, colWidths=(201, A4[0] - 94 - 201), repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HexColor(BLUE)),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [HexColor("#FFFFFF"), HexColor(PALE)]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.32, HexColor(LINE)),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    body.append(table)
    return body


def build_pdf(path: Path, family: str, data: dict) -> None:
    styles = doc_styles()
    title = data["families"][family]
    groups = [group for group in data["groups"] if group["family"] == family]
    if family == "shared":
        groups.append({
            "title": "Когда нужно уточнение",
            "names": ["Блинчики", "ФКР", "ги", "щитки"],
            "scope": "По этим словам подбор зависит от контекста. Здесь приведены варианты значения и вопрос для уточнения.",
            "sources": list(dict.fromkeys(key for item in data["ambiguities"] for key in item["sources"])),
            "terms": [{"term": ", ".join(item["aliases"]), "aliases": [case["term"] for case in item["cases"]],
                       "note": item["question"], "sources": item["sources"]} for item in data["ambiguities"]],
        })
    story = [
        Spacer(1, 49),
        Paragraph("СЛОВАРЬ ЭКИПИРОВКИ HARD-PRO", styles["small"]),
        Spacer(1, 12),
        Paragraph(printable(title), styles["title"]),
        Paragraph("Сокращения, разговорные названия и варианты написания для поиска по каталогу.", styles["body"]),
        Spacer(1, 15),
        Paragraph("Отметка «Зафиксировано в источниках» относится к употреблению конкретного написания.",
                  styles["body"]),
        Spacer(1, 8),
        Paragraph("Для допуска экипировки проверяйте категорию спортсмена, маркировку модели и положение конкретного турнира.",
                  styles["body"]),
        Spacer(1, 24),
        Paragraph("Разделы", styles["h1"]),
    ]
    for group in groups:
        story.extend([Paragraph(printable(group["title"]), styles["body"]), Spacer(1, 4)])
    for group in groups:
        story.extend([PageBreak(), *group_page(group, data["sources"], styles)])

    def draw_frame(canvas, _doc):
        canvas.setFillColor(HexColor(BLUE))
        canvas.rect(0, A4[1] - 9, A4[0], 9, stroke=0, fill=1)
        canvas.setFont("Text", 8.5)
        canvas.drawString(47, 28, f"HARD-PRO   /   {data['reviewed_at']}")
        canvas.drawRightString(A4[0] - 47, 28, str(canvas.getPageNumber()))

    doc = SimpleDocTemplate(str(path), pagesize=A4, title=title, author="HARD-PRO",
                            leftMargin=41, rightMargin=41, topMargin=45, bottomMargin=51)
    doc.build(story, onFirstPage=draw_frame, onLaterPages=draw_frame)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--regular-font", type=Path, required=True)
    parser.add_argument("--bold-font", type=Path, required=True)
    args = parser.parse_args()
    pdfmetrics.registerFont(TTFont("Text", str(args.regular_font)))
    pdfmetrics.registerFont(TTFont("Bold", str(args.bold_font)))
    data = load("combat-terminology.json")
    existing = load("manager-knowledge.json")
    pdf_dir = args.output / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    records = list(corpus(data, existing))
    if len({item["id"] for item in records}) != len(records):
        raise ValueError("Повторяющиеся идентификаторы фрагментов")
    (args.output / "hardpro-terminology-qdrant.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
        encoding="utf-8", newline="\n",
    )
    for family, filename in PDF_NAMES.items():
        build_pdf(pdf_dir / filename, family, data)
    print(f"{len(records)} фрагментов, {len(PDF_NAMES)} PDF")


if __name__ == "__main__":
    main()

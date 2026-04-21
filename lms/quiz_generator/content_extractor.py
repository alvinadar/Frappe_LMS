import re
import os
import json
import frappe
from difflib import SequenceMatcher


def extract_course_content(course_name: str) -> list:
    lessons = []
    chapters = frappe.get_all('Course Chapter',
                              filters={'course': course_name},
                              fields=['name', 'title'], order_by='creation asc')
    for chapter in chapters:
        lesson_refs = frappe.get_all('Lesson Reference',
                                     filters={'parent': chapter['name'], 'parenttype': 'Course Chapter'},
                                     fields=['lesson'], order_by='idx asc')
        for ref in lesson_refs:
            lesson = frappe.get_doc('Course Lesson', ref['lesson'])
            content = _extract_lesson_text(lesson)
            lessons.append({
                'name': lesson.name,
                'title': lesson.title or lesson.name,
                'chapter_title': chapter['title'] or chapter['name'],
                'course_name': course_name, # Added for vector metadata
                'content': content,
            })
    _match_pdfs_to_lessons(lessons)
    return lessons

def _extract_lesson_text(lesson) -> str:
    raw_parts = []

    if lesson.get("title"):
        raw_parts.insert(0, lesson.title)

    if lesson.get("content"):
        text, pdf_urls = _parse_editorjs(lesson.content)
        if text:
            raw_parts.append(text)
        for url in pdf_urls:
            pdf_text = _read_pdf_file(url)
            if pdf_text:
                raw_parts.append(pdf_text)

    if lesson.get("body"):
        raw_parts.append(lesson.body)

    attached_pdfs = frappe.get_all(
        "File",
        filters={
            "attached_to_doctype": "Course Lesson",
            "attached_to_name": lesson.name,
            "file_url": ["like", "%.pdf"],
        },
        fields=["file_url"],
    )
    for f in attached_pdfs:
        pdf_text = _read_pdf_file(f["file_url"])
        if pdf_text:
            raw_parts.append(pdf_text)

    combined = "\n\n".join(raw_parts)
    return _clean_text(combined)


def _match_pdfs_to_lessons(lessons: list):
    """
    Match unlinked PDFs to lessons by filename similarity.
    Each PDF is assigned to only ONE lesson (highest score wins).
    Each lesson gets only ONE PDF (highest score wins).
    """
    all_pdfs = frappe.get_all(
        "File",
        filters=[
            ["file_url", "like", "%.pdf"],
            ["attached_to_doctype", "is", "not set"],
        ],
        fields=["file_url", "file_name"],
    )

    if not all_pdfs:
        return

    # Deduplicate PDFs by file_url
    seen_urls = set()
    unique_pdfs = []
    for pdf in all_pdfs:
        if pdf["file_url"] not in seen_urls:
            seen_urls.add(pdf["file_url"])
            unique_pdfs.append(pdf)

    # Build score matrix
    scores = []
    for pdf in unique_pdfs:
        pdf_name = pdf["file_name"].replace(".pdf", "").replace("_", " ").lower()
        for lesson in lessons:
            score = SequenceMatcher(None, pdf_name, lesson["title"].lower()).ratio()
            if score >= 0.45:
                scores.append((score, pdf, lesson))

    # Sort by score descending — best matches first
    scores.sort(key=lambda x: x[0], reverse=True)

    used_pdfs = set()
    used_lessons = set()

    for score, pdf, lesson in scores:
        # Skip if this PDF or lesson already matched
        if pdf["file_url"] in used_pdfs:
            continue
        if lesson["name"] in used_lessons:
            continue

        pdf_text = _read_pdf_file(pdf["file_url"])
        if pdf_text:
            frappe.logger().info(
                f"[GeminiQuiz] Matched PDF '{pdf['file_name']}' "
                f"to lesson '{lesson['title']}' (score: {score:.2f})"
            )
            lesson["content"] = (_clean_text(pdf_text)).strip()
            used_pdfs.add(pdf["file_url"])
            used_lessons.add(lesson["name"])


def _parse_editorjs(content: str):
    texts = []
    pdf_urls = []

    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content, []

    for block in data.get("blocks", []):
        block_type = block.get("type", "")
        block_data = block.get("data", {})

        if block_type in ["paragraph", "header"]:
            text = block_data.get("text", "").strip()
            if text:
                texts.append(text)
        elif block_type == "markdown":
            text = block_data.get("text", "").strip()
            if text:
                texts.append(text)
        elif block_type == "list":
            for item in block_data.get("items", []):
                if isinstance(item, str) and item.strip():
                    texts.append(item.strip())
        elif block_type == "upload":
            file_url = block_data.get("file_url", "")
            file_type = block_data.get("file_type", "")
            if file_type == "PDF" or file_url.lower().endswith(".pdf"):
                pdf_urls.append(file_url)

    return "\n".join(texts), pdf_urls


def _read_pdf_file(file_url: str) -> str:
    try:
        from PyPDF2 import PdfReader

        site_path = frappe.get_site_path()

        if file_url.startswith("/private/files/"):
            file_path = os.path.join(site_path, "private", "files", os.path.basename(file_url))
        else:
            file_path = os.path.join(site_path, "public", "files", os.path.basename(file_url))

        if not os.path.exists(file_path):
            frappe.logger().warning(f"[GeminiQuiz] PDF not found: {file_path}")
            return ""

        reader = PdfReader(file_path)
        pages_text = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages_text.append(text.strip())

        return "\n".join(pages_text)

    except Exception as e:
        frappe.logger().warning(f"[GeminiQuiz] Failed to read PDF {file_url}: {e}")
        return ""


def _clean_text(text: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    entities = {
        "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&quot;": '"', "&#39;": "'", "&nbsp;": " ",
    }
    for entity, char in entities.items():
        text = text.replace(entity, char)
    text = re.sub(r"[#*_`~\[\]|]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def test_pdf_matching():
    lessons = [
        {"name": "ann-basics", "title": "Artificial Neural Network Basics", "content": ""},
        {"name": "activation", "title": "Activation Function", "content": ""},
        {"name": "loss-functions", "title": "Loss Functions Basic-ANN", "content": ""},
        {"name": "sql-intro", "title": "Pythons SQL Introduction", "content": ""},
    ]
    _match_pdfs_to_lessons(lessons)
    for l in lessons:
        print(f"Lesson: {l['title']} | Content: {len(l['content'])} chars")
        print(f"Preview: {l['content'][:150]}")
        print("---")

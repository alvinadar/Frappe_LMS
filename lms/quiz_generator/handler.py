import frappe
import time


def handle_course_update(doc, method):
    if doc.status not in ["Published", "Approved"]:
        return
    if doc.get("ai_quiz_generated"):
        return
    frappe.enqueue(
        "lms.quiz_generator.handler.generate_quiz_job",
        course_name=doc.name,
        queue="long",
        timeout=600,
        job_name=f"gemini_quiz_{doc.name}",
        enqueue_after_commit=True,
    )


def generate_quiz_job(course_name: str):
    from lms.quiz_generator.content_extractor import extract_course_content
    from lms.quiz_generator.vector_store import process_and_store_document
    from lms.quiz_generator.gemini_client import generate_questions_with_rag
    from lms.quiz_generator.quiz_builder import create_quiz_for_lesson
    
    frappe.logger().info(f'[GeminiQuiz] Starting Vector Ingestion for: {course_name}')
    try:
        lessons = extract_course_content(course_name)
        if not lessons:
            return
        
        for lesson in lessons:
            if not lesson.get('content') or len(lesson['content'].strip()) < 100:
                continue
                
            existing = frappe.db.get_value('Course Lesson', lesson['name'], 'quiz_id')
            if existing and existing.startswith('ai-'):
                continue
                
            # 1. Build Metadata for ChromaDB
            metadata = {
                "course_name": lesson.get('course_name', course_name),
                "chapter_title": lesson.get('chapter_title', ''),
                "lesson_title": lesson['title']
            }
            
            # 2. Chunk and Store in Vector Database
            is_stored = process_and_store_document(
                lesson_name=lesson['name'], 
                text_content=lesson['content'], 
                metadata=metadata
            )
            
            # 3. Trigger the RAG Quiz Generation
            if is_stored:
                frappe.logger().info(f'[GeminiQuiz] Vectors stored for {lesson["name"]}. Executing RAG.')
                
                questions = generate_questions_with_rag(
                    lesson_title=lesson['title'], 
                    num_questions=5
                )
                
                if questions:
                    quiz_name = create_quiz_for_lesson(
                        course_name=course_name,
                        lesson_name=lesson['name'],
                        lesson_title=lesson['title'],
                        chapter_title=lesson.get('chapter_title', ''),
                        questions=questions,
                    )
                    frappe.logger().info(f'[GeminiQuiz] Successfully created LMS Quiz: {quiz_name}')
                
    except Exception:
        frappe.log_error(frappe.get_traceback(), f'[GeminiQuiz] Vectorization/RAG Failed: {course_name}')


def handle_new_lesson(doc, method):
    """Fires when a new lesson is created. Generates quiz immediately."""
    if doc.get("quiz_id"):
        return

    # Get course from doc directly or look it up via chapter
    course_name = doc.get("course")

    if not course_name:
        # Try to find course via Chapter Reference
        chapter_ref = frappe.db.get_value(
            "Lesson Reference",
            {"lesson": doc.name},
            "parent"
        )
        if chapter_ref:
            course_name = frappe.db.get_value("Course Chapter", chapter_ref, "course")

    if not course_name:
        frappe.logger().info(f"[GeminiQuiz] Could not find course for lesson '{doc.name}'. Skipping.")
        return

    frappe.logger().info(f"[GeminiQuiz] New lesson '{doc.name}' detected. Enqueueing quiz generation.")
    frappe.enqueue(
        "lms.quiz_generator.handler.regenerate_quiz_for_lesson",
        lesson_name=doc.name,
        course_name=course_name,
        queue="long",
        timeout=300,
        enqueue_after_commit=True,
    )


def handle_lesson_update(doc, method):
    """Fires when lesson content is updated. Regenerates quiz."""
    if not doc.get("course"):
        return
    # Only regenerate if body or content changed
    frappe.enqueue(
        "lms.quiz_generator.handler.regenerate_quiz_for_lesson",
        lesson_name=doc.name,
        course_name=doc.course,
        queue="long",
        timeout=300,
        enqueue_after_commit=True,
    )


def handle_file_upload(doc, method):
    """Fires when a PDF is uploaded. Only matches and generates for unquizzed lessons."""
    if not doc.file_url or not doc.file_url.lower().endswith(".pdf"):
        return

    frappe.logger().info(f"[GeminiQuiz] PDF uploaded: {doc.file_name}")

    # Find the best matching lesson that doesn't have a quiz yet
    from difflib import SequenceMatcher

    lessons = frappe.get_all(
        "Course Lesson",
        fields=["name", "title", "course", "quiz_id"],
    )

    pdf_name = doc.file_name.replace(".pdf", "").replace("_", " ").lower()
    best_score = 0
    best_lesson = None

    for lesson in lessons:
        # Skip lessons that already have an AI quiz
        if lesson.get("quiz_id") and lesson["quiz_id"].startswith("ai-"):
            continue
        score = SequenceMatcher(None, pdf_name, lesson["title"].lower()).ratio()
        if score > best_score:
            best_score = score
            best_lesson = lesson

    if best_score >= 0.45 and best_lesson:
        frappe.logger().info(
            f"[GeminiQuiz] PDF '{doc.file_name}' matched to lesson "
            f"'{best_lesson['title']}' (score: {best_score:.2f}). Generating quiz."
        )
        frappe.enqueue(
            "lms.quiz_generator.handler.regenerate_quiz_for_lesson",
            lesson_name=best_lesson["name"],
            course_name=best_lesson["course"],
            queue="long",
            timeout=300,
            enqueue_after_commit=True,
        )
    else:
        frappe.logger().info(
            f"[GeminiQuiz] No unquizzed lesson matched PDF '{doc.file_name}' "
            f"(best score: {best_score:.2f}). Skipping."
        )


def regenerate_quiz_for_lesson(lesson_name: str, course_name: str):
    from lms.quiz_generator.content_extractor import extract_course_content
    from lms.quiz_generator.vector_store import process_and_store_document
    from lms.quiz_generator.gemini_client import generate_questions_with_rag
    from lms.quiz_generator.quiz_builder import create_quiz_for_lesson

    lock_key = f'gemini_quiz_lock_{lesson_name}'
    if frappe.cache.get_value(lock_key):
        return
    frappe.cache.set_value(lock_key, 1, expires_in_sec=600)

    try:
        lessons = extract_course_content(course_name)
        lesson = next((l for l in lessons if l['name'] == lesson_name), None)
        
        if not lesson or len(lesson['content'].strip()) < 100:
            return
            
        existing_id = frappe.db.get_value('Course Lesson', lesson_name, 'quiz_id')
        if existing_id and existing_id.startswith('ai-'):
            frappe.delete_doc('LMS Quiz', existing_id, ignore_permissions=True)
            frappe.db.commit()

        # 1. Build Metadata for ChromaDB
        metadata = {
            "course_name": course_name,
            "chapter_title": lesson.get('chapter_title', ''),
            "lesson_title": lesson['title']
        }

        # 2. Chunk and Store in Vector Database
        is_stored = process_and_store_document(
            lesson_name=lesson_name, 
            text_content=lesson['content'], 
            metadata=metadata
        )

        # 3. Trigger the RAG Quiz Generation
        if is_stored:
             frappe.logger().info(f'[GeminiQuiz] Vectors stored for {lesson_name}. Executing RAG.')
             
             questions = generate_questions_with_rag(
                 lesson_title=lesson['title'], 
                 num_questions=5
             )
             
             if questions:
                 quiz_name = create_quiz_for_lesson(
                     course_name=course_name,
                     lesson_name=lesson_name,
                     lesson_title=lesson['title'],
                     chapter_title=lesson.get('chapter_title', ''),
                     questions=questions,
                 )
                 frappe.logger().info(f'[GeminiQuiz] Successfully created LMS Quiz: {quiz_name}')

    except Exception:
        frappe.log_error(frappe.get_traceback(), f'[GeminiQuiz] Vectorization/RAG Failed: {lesson_name}')


def retry_failed_generations():
    """Daily scheduler: retry lessons with no quiz."""
    lessons = frappe.get_all(
        "Course Lesson",
        fields=["name", "course"],
        filters={"quiz_id": ["is", "not set"]},
    )
    for lesson in lessons:
        if not lesson.get("course"):
            continue
        try:
            regenerate_quiz_for_lesson(lesson["name"], lesson["course"])
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"[GeminiQuiz] Failed for {lesson['name']}")


def handle_lesson_reference_insert(doc, method):
    """
    Fires when a lesson is linked to a chapter.
    At this point we can reliably find the course.
    """
    try:
        # Get the chapter this lesson was added to
        chapter = frappe.get_doc("Course Chapter", doc.parent)
        course_name = chapter.course

        if not course_name:
            return

        lesson_name = doc.lesson

        # Skip if lesson already has a quiz
        existing_quiz = frappe.db.get_value("Course Lesson", lesson_name, "quiz_id")
        if existing_quiz and existing_quiz.startswith("ai-"):
            return

        frappe.logger().info(
            f"[GeminiQuiz] Lesson '{lesson_name}' linked to chapter '{doc.parent}'. "
            f"Enqueueing quiz generation."
        )

        frappe.enqueue(
            "lms.quiz_generator.handler.regenerate_quiz_for_lesson",
            lesson_name=lesson_name,
            course_name=course_name,
            queue="long",
            timeout=300,
            enqueue_after_commit=True,
        )

    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "[GeminiQuiz] handle_lesson_reference_insert failed"
        )


def process_pending_lessons():
    """
    Runs every 30 minutes.
    Finds all lessons without quizzes and generates them.
    Clears stuck jobs from queue first.
    """
    import redis
    from rq import Queue as RQueue

    # Clear stuck jobs
    try:
        r = redis.from_url("redis://localhost:11000")
        q = RQueue("Users-alvinash-frappe-bench:long", connection=r)
        q.empty()
        frappe.logger().info("[GeminiQuiz] Queue cleared by scheduler.")
    except Exception:
        pass

    # Find all lessons without quizzes
    lessons = frappe.get_all(
        "Course Lesson",
        fields=["name", "title", "course"],
        filters={"quiz_id": ["is", "not set"]},
    )

    if not lessons:
        frappe.logger().info("[GeminiQuiz] No pending lessons found.")
        return

    frappe.logger().info(f"[GeminiQuiz] Found {len(lessons)} lessons without quizzes.")

    for lesson in lessons:
        if not lesson.get("course"):
            continue
        try:
            regenerate_quiz_for_lesson(lesson["name"], lesson["course"])
        except Exception:
            frappe.log_error(frappe.get_traceback(), f"[GeminiQuiz] Failed for {lesson['name']}")
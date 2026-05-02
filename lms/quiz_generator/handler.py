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
        # Race condition: lesson created before chapter linkage complete.
        # Enqueue a delayed retry; by then handle_lesson_reference_insert
        # will have set up the chapter, OR the cron will catch it.
        frappe.logger().info(
            f"[GeminiQuiz] Course unknown for lesson '{doc.name}' at insert time. "
            f"Cron will pick it up."
        )
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


@frappe.whitelist()
def regenerate_quiz_for_lesson(lesson_name: str, course_name: str):
    if not frappe.has_permission("Course Lesson", "write"):
        frappe.throw("You need write access to Course Lesson.")

    from lms.quiz_generator.content_extractor import extract_course_content, _extract_lesson_text
    from lms.quiz_generator.vector_store import process_and_store_document
    from lms.quiz_generator.gemini_client import generate_questions_with_rag
    from lms.quiz_generator.quiz_builder import create_quiz_for_lesson

    MIN_CONTENT_CHARS = 50

    frappe.log_error(
        f"[REGEN START] lesson={lesson_name} course={course_name}",
        "[GeminiQuiz REGEN]"
    )

    lock_key = f'gemini_quiz_lock_{lesson_name}'
    if frappe.cache.get_value(lock_key):
        frappe.log_error(
            f"[REGEN SKIP] lock active for {lesson_name}",
            "[GeminiQuiz REGEN]"
        )
        return {"status": "skipped", "reason": "lock_active"}
    frappe.cache.set_value(lock_key, 1, expires_in_sec=600)

    try:
        # Try the chapter-walk path first
        lessons = extract_course_content(course_name)
        lesson = next((l for l in lessons if l['name'] == lesson_name), None)

        # Fallback: if chapter walk missed it, read the Course Lesson doc directly
        if not lesson:
            frappe.log_error(
                f"[REGEN FALLBACK] {lesson_name} not in extract_course_content results. Reading directly.",
                "[GeminiQuiz REGEN]"
            )
            try:
                lesson_doc = frappe.get_doc("Course Lesson", lesson_name)
                content = _extract_lesson_text(lesson_doc)
                lesson = {
                    "name": lesson_doc.name,
                    "title": lesson_doc.title or lesson_doc.name,
                    "chapter_title": "",
                    "course_name": course_name,
                    "content": content,
                }
            except Exception as e:
                frappe.log_error(
                    f"[REGEN FAIL] cannot load lesson {lesson_name}: {e}",
                    "[GeminiQuiz REGEN]"
                )
                return {"status": "failed", "reason": "lesson_not_found"}

        content_len = len(lesson['content'].strip())
        frappe.log_error(
            f"[REGEN CONTENT] {lesson_name}: {content_len} chars extracted",
            "[GeminiQuiz REGEN]"
        )

        if content_len < MIN_CONTENT_CHARS:
            return {"status": "skipped", "reason": "content_too_short", "chars": content_len}

        metadata = {
            "course_name": course_name,
            "chapter_title": lesson.get('chapter_title', ''),
            "lesson_title": lesson['title']
        }

        is_stored = process_and_store_document(
            lesson_name=lesson_name,
            text_content=lesson['content'],
            metadata=metadata
        )

        if not is_stored:
            frappe.log_error(
                f"[REGEN FAIL] vector storage failed for {lesson_name}",
                "[GeminiQuiz REGEN]"
            )
            return {"status": "failed", "reason": "vector_storage_failed"}

        frappe.log_error(
            f"[REGEN RAG] calling Gemini for {lesson_name}",
            "[GeminiQuiz REGEN]"
        )

        questions = generate_questions_with_rag(
            lesson_title=lesson['title'],
            num_questions=5
        )

        if not questions:
            frappe.log_error(
                f"[REGEN FAIL] Gemini returned no valid questions for {lesson_name}",
                "[GeminiQuiz REGEN]"
            )
            return {"status": "failed", "reason": "no_questions_generated"}

        quiz_name = create_quiz_for_lesson(
            course_name=course_name,
            lesson_name=lesson_name,
            lesson_title=lesson['title'],
            chapter_title=lesson.get('chapter_title', ''),
            questions=questions,
        )

        if not quiz_name:
            return {"status": "failed", "reason": "quiz_builder_returned_none"}

        frappe.log_error(
            f"[REGEN SUCCESS] {lesson_name} -> {quiz_name}",
            "[GeminiQuiz REGEN]"
        )
        return {"status": "success", "quiz_name": quiz_name, "questions_count": len(questions)}

    except Exception:
        frappe.log_error(frappe.get_traceback(), f'[GeminiQuiz REGEN] Exception: {lesson_name}')
        return {"status": "exception"}
    finally:
        frappe.cache.delete_value(lock_key)


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
    """Cron entry: generate quizzes for lessons that don't have one yet.

    Quota-aware:
      - Honors gemini_rate_limited_until circuit breaker
      - Processes at most BATCH_LIMIT lessons per run
      - Skips lessons that have failed MAX_LESSON_ATTEMPTS times
    """
    BATCH_LIMIT = 5
    MAX_LESSON_ATTEMPTS = 5  # raised from 3 - cron should be persistent

    # Circuit breaker: if Gemini was 429'd recently, skip this entire run.
    if frappe.cache.get_value("gemini_rate_limited_until"):
        frappe.logger().info(
            "[GeminiQuiz] Skipping run: Gemini circuit breaker active."
        )
        return

    lessons = frappe.get_all(
        "Course Lesson",
        fields=["name", "title", "course"],
        filters={"quiz_id": ["is", "not set"]},
    )
    if not lessons:
        return

    processed = 0
    for lesson in lessons:
        if processed >= BATCH_LIMIT:
            frappe.logger().info(
                f"[GeminiQuiz] Reached batch limit ({BATCH_LIMIT}); next cron picks up the rest."
            )
            break

        if not lesson.get("course"):
            continue

        # Per-lesson retry counter in cache (24h TTL, resets daily)
        attempts_key = f"gemini_attempts:{lesson['name']}"
        attempts = int(frappe.cache.get_value(attempts_key) or 0)
        if attempts >= MAX_LESSON_ATTEMPTS:
            frappe.logger().info(
                f"[GeminiQuiz] Skipping {lesson['name']}: {attempts} attempts already."
            )
            continue

        try:
            result = regenerate_quiz_for_lesson(lesson["name"], lesson["course"])
            processed += 1
            # Reset this lesson's counter
            frappe.cache.set_value(attempts_key, 0, expires_in_sec=86400)
            # If the regen actually produced a quiz, also reset all OTHER lessons'
            # counters - quota proved good, so don't keep skipping siblings.
            if isinstance(result, dict) and result.get("status") == "success":
                for other in lessons:
                    other_key = f"gemini_attempts:{other['name']}"
                    if other['name'] != lesson['name']:
                        frappe.cache.set_value(other_key, 0, expires_in_sec=86400)

            # If a circuit breaker got set DURING this lesson, stop the loop.
            if frappe.cache.get_value("gemini_rate_limited_until"):
                frappe.logger().info(
                    "[GeminiQuiz] Circuit breaker tripped mid-run; stopping."
                )
                break

        except Exception:
            frappe.cache.set_value(
                attempts_key, attempts + 1, expires_in_sec=86400
            )
            frappe.log_error(
                frappe.get_traceback(),
                f"[GeminiQuiz] Failed for {lesson['name']} (attempt {attempts + 1})",
            )



@frappe.whitelist()
def backfill_quiz_blocks():
    """One-time retroactive patch: for every lesson that has quiz_id set but no
    corresponding {"type":"quiz"} block in its content JSON, add the block.

    Safe to run multiple times (idempotent). Does NOT call Gemini.
    Intended to be invoked once via /api/method after deploying the
    embed-quiz-as-content-block fix.

    Returns a summary dict so the admin can see what changed.
    """
    import json

    if not frappe.has_permission("Course Lesson", "write"):
        frappe.throw("You need write access to Course Lesson to run this.")

    lessons = frappe.get_all(
        "Course Lesson",
        filters={"quiz_id": ["is", "set"]},
        fields=["name", "title", "quiz_id", "content"],
    )

    patched = []
    skipped_already_linked = []
    skipped_no_quiz = []
    errors = []

    for row in lessons:
        try:
            quiz_name = row.get("quiz_id")
            if not quiz_name:
                skipped_no_quiz.append(row["name"])
                continue

            raw_content = row.get("content") or ""
            if raw_content.strip():
                try:
                    content_data = json.loads(raw_content)
                except Exception:
                    content_data = {"blocks": []}
            else:
                content_data = {"blocks": []}

            if not isinstance(content_data, dict):
                content_data = {"blocks": []}
            if "blocks" not in content_data or not isinstance(content_data["blocks"], list):
                content_data["blocks"] = []

            already_linked = any(
                (b or {}).get("type") == "quiz"
                and ((b or {}).get("data") or {}).get("quiz") == quiz_name
                for b in content_data["blocks"]
            )

            if already_linked:
                skipped_already_linked.append(row["name"])
                continue

            content_data["blocks"].append({
                "id": f"quiz-{quiz_name[:20]}",
                "type": "quiz",
                "data": {"quiz": quiz_name},
            })

            frappe.db.set_value(
                "Course Lesson",
                row["name"],
                "content",
                json.dumps(content_data),
                update_modified=False,
            )
            patched.append({"lesson": row["name"], "quiz": quiz_name})
        except Exception as e:
            errors.append({"lesson": row.get("name"), "error": str(e)})
            frappe.log_error(
                frappe.get_traceback(),
                f"[GeminiQuiz Backfill] Failed on lesson '{row.get('name')}'",
            )

    frappe.db.commit()

    return {
        "total_lessons_with_quiz_id": len(lessons),
        "patched": patched,
        "patched_count": len(patched),
        "already_linked_count": len(skipped_already_linked),
        "no_quiz_count": len(skipped_no_quiz),
        "errors": errors,
    }

@frappe.whitelist()
def clean_orphan_quiz_refs():
    """One-time cleanup: for every lesson, remove quiz_id and content-block
    quiz references that point to non-existent LMS Quiz records.

    Bypasses doc validation by writing directly with frappe.db.set_value.
    Use after a quiz wipeout to unstick lessons that won't save due to
    'Invalid Quiz ID' validation.
    """
    import json

    if not frappe.has_permission("Course Lesson", "write"):
        frappe.throw("You need write access to Course Lesson.")

    lessons = frappe.get_all(
        "Course Lesson",
        fields=["name", "quiz_id", "content"],
    )

    cleaned = []
    skipped = []
    errors = []

    for row in lessons:
        try:
            modified = False
            quiz_id = row.get("quiz_id")

            # 1. Clear quiz_id if quiz no longer exists
            if quiz_id and not frappe.db.exists("LMS Quiz", quiz_id):
                frappe.db.set_value(
                    "Course Lesson", row["name"], "quiz_id", "",
                    update_modified=False,
                )
                modified = True

            # 2. Strip orphan quiz blocks from content JSON
            raw_content = row.get("content") or ""
            if raw_content.strip():
                try:
                    content_data = json.loads(raw_content)
                except Exception:
                    content_data = None

                if isinstance(content_data, dict) and isinstance(content_data.get("blocks"), list):
                    original_count = len(content_data["blocks"])
                    cleaned_blocks = []
                    for block in content_data["blocks"]:
                        if (block or {}).get("type") == "quiz":
                            ref = ((block or {}).get("data") or {}).get("quiz")
                            if ref and not frappe.db.exists("LMS Quiz", ref):
                                continue  # drop orphan
                        cleaned_blocks.append(block)
                    if len(cleaned_blocks) != original_count:
                        content_data["blocks"] = cleaned_blocks
                        frappe.db.set_value(
                            "Course Lesson", row["name"], "content",
                            json.dumps(content_data),
                            update_modified=False,
                        )
                        modified = True

            if modified:
                cleaned.append(row["name"])
            else:
                skipped.append(row["name"])

        except Exception as e:
            errors.append({"lesson": row.get("name"), "error": str(e)})
            frappe.log_error(
                frappe.get_traceback(),
                f"[GeminiQuiz Cleanup] Failed on lesson '{row.get('name')}'",
            )

    frappe.db.commit()

    return {
        "total": len(lessons),
        "cleaned": cleaned,
        "cleaned_count": len(cleaned),
        "skipped_count": len(skipped),
        "errors": errors,
    }

@frappe.whitelist()
def diagnose_lesson(lesson_name: str, course_name: str):
    """Returns everything regenerate_quiz_for_lesson would see, without calling Gemini."""
    from lms.quiz_generator.content_extractor import extract_course_content, _extract_lesson_text

    if not frappe.has_permission("Course Lesson", "write"):
        frappe.throw("You need write access to Course Lesson.")

    result = {
        "lesson_name": lesson_name,
        "course_name": course_name,
    }

    # Direct doc check
    try:
        lesson_doc = frappe.get_doc("Course Lesson", lesson_name)
        result["doc_exists"] = True
        result["doc_title"] = lesson_doc.title
        result["doc_quiz_id"] = lesson_doc.quiz_id
        result["doc_body_len"] = len(lesson_doc.body or "")
        result["doc_content_len"] = len(lesson_doc.content or "")
        try:
            extracted = _extract_lesson_text(lesson_doc)
            result["direct_extracted_len"] = len(extracted)
            result["direct_extracted_preview"] = extracted[:300]
        except Exception as e:
            result["direct_extracted_error"] = str(e)
    except Exception as e:
        result["doc_exists"] = False
        result["doc_error"] = str(e)

    # Chapter-walk path
    try:
        lessons = extract_course_content(course_name)
        result["course_walk_lesson_count"] = len(lessons)
        result["course_walk_lesson_names"] = [l['name'] for l in lessons]
        match = next((l for l in lessons if l['name'] == lesson_name), None)
        if match:
            result["course_walk_found"] = True
            result["course_walk_content_len"] = len(match['content'])
            result["course_walk_content_preview"] = match['content'][:300]
        else:
            result["course_walk_found"] = False
    except Exception as e:
        result["course_walk_error"] = str(e)

    # Cache state
    result["cache_lock_active"] = bool(frappe.cache.get_value(f'gemini_quiz_lock_{lesson_name}'))
    result["cache_circuit_breaker"] = bool(frappe.cache.get_value("gemini_rate_limited_until"))
    result["cache_attempts"] = frappe.cache.get_value(f"gemini_attempts:{lesson_name}")

    return result

@frappe.whitelist()
def test_gemini():
    """Direct, single-shot Gemini test. Returns whatever happens as JSON.
    Bypasses RAG, parser, retries, content extraction.
    """
    if not frappe.has_permission("Course Lesson", "write"):
        frappe.throw("Permission denied.")

    result = {}

    try:
        from lms.quiz_generator.gemini_client import _get_api_key
        api_key = _get_api_key()
        result["api_key_present"] = bool(api_key)
        result["api_key_suffix"] = api_key[-4:] if api_key else None
    except Exception as e:
        result["api_key_error"] = f"{type(e).__name__}: {e}"
        return result

    try:
        from langchain_google_genai import ChatGoogleGenerativeAI

        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=api_key,
            temperature=0.4,
            max_output_tokens=200,
        )
        response = llm.invoke("Say the word 'hello' and nothing else.")
        result["status"] = "success"
        result["response_type"] = type(response).__name__
        result["response_content"] = str(response.content)[:500]
        return result

    except Exception as e:
        result["status"] = "exception"
        result["exception_type"] = type(e).__name__
        result["exception_message"] = str(e)[:1500]
        import traceback
        result["traceback"] = traceback.format_exc()[:2000]
        return result
    
@frappe.whitelist()
def test_rag_prompt():
    """Run the full RAG prompt with a tiny test context. Returns raw output."""
    if not frappe.has_permission("Course Lesson", "write"):
        frappe.throw("Permission denied.")

    result = {}

    try:
        from lms.quiz_generator.gemini_client import _get_api_key
        from langchain_google_genai import ChatGoogleGenerativeAI
        from langchain_core.prompts import PromptTemplate

        api_key = _get_api_key()

        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            google_api_key=api_key,
            temperature=0.4,
            max_output_tokens=2048,
        )

        template = """You are an expert instructional designer.
    Based ONLY on the provided curriculum context, generate exactly {num_questions} multiple-choice questions.

    RULES:
    1. Each question must have exactly 4 answer options.
    2. Exactly 1 option must be correct.
    3. Provide a brief explanation for the correct answer.
    4. Return ONLY valid JSON — no markdown, no code fences.

    JSON FORMAT:
    [
        {{
            "question": "Question text?",
            "options": [
                {{"text": "Option A", "is_correct": true, "explanation": "Why"}},
                {{"text": "Option B", "is_correct": false, "explanation": ""}},
                {{"text": "Option C", "is_correct": false, "explanation": ""}},
                {{"text": "Option D", "is_correct": false, "explanation": ""}}
            ]
        }}
    ]

    CURRICULUM CONTEXT:
    {context}

    Return ONLY the JSON array."""

        prompt = PromptTemplate(
            input_variables=["num_questions", "context"],
            template=template
        )
        chain = prompt | llm

        sample_context = ("Machine learning is a subset of artificial intelligence "
                          "that allows systems to learn from data. Common types are "
                          "supervised, unsupervised, and reinforcement learning.")

        response = chain.invoke({
            "num_questions": 2,
            "context": sample_context,
        })

        raw = str(response.content)
        result["status"] = "success"
        result["raw_output"] = raw[:3000]
        result["raw_length"] = len(raw)
        result["starts_with"] = raw[:30]
        result["ends_with"] = raw[-30:] if len(raw) > 30 else raw
        return result

    except Exception as e:
        import traceback
        result["status"] = "exception"
        result["exception_type"] = type(e).__name__
        result["exception_message"] = str(e)[:1500]
        result["traceback"] = traceback.format_exc()[:2500]
        return result
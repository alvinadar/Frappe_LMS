import frappe


def create_quiz_for_lesson(course_name, lesson_name, lesson_title, chapter_title, questions):
    quiz_title = _build_quiz_title(lesson_title, chapter_title)

    # SAFETY: Build the new quiz fully BEFORE deleting any existing one,
    # so a generation failure cannot leave the lesson pointing to nothing.
    quiz = frappe.new_doc("LMS Quiz")
    quiz.title = quiz_title
    quiz.passing_percentage = 60
    quiz.max_attempts = 3
    for q_data in questions:
        lms_question = _create_lms_question(q_data)
        if lms_question:
            quiz.append("questions", {
                "question": lms_question.name,
                "question_detail": q_data["question"],
                "type": "Choices",
                "marks": 1,
            })

    if not quiz.questions:
        frappe.log_error(
            f"[GeminiQuiz] Aborting regen for {lesson_name}: zero valid questions built."
        )
        return None

    # Only NOW remove the old quiz, immediately before inserting the replacement.
    existing_quiz = frappe.db.exists("LMS Quiz", {"title": quiz_title})
    if existing_quiz:
        frappe.delete_doc("LMS Quiz", existing_quiz, ignore_permissions=True)
        frappe.db.commit()

    quiz.insert(ignore_permissions=True, ignore_if_duplicate=True)
    frappe.db.commit()
    _link_quiz_to_lesson(lesson_name, quiz.name)
    return quiz.name


def _create_lms_question(q_data):
    """
    Creates an LMS Question document with option_1..4 and is_correct_1..4 fields.
    """
    try:
        options = q_data["options"][:4]

        question = frappe.new_doc("LMS Question")
        question.question = q_data["question"]
        question.type = "Choices"

        for i, opt in enumerate(options, 1):
            question.set(f"option_{i}", opt["text"])
            question.set(f"is_correct_{i}", 1 if opt["is_correct"] else 0)
            question.set(f"explanation_{i}", opt.get("explanation", ""))

        question.insert(ignore_permissions=True)
        return question

    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"[GeminiQuiz] Failed to create LMS Question: {q_data.get('question', '')[:80]}",
        )
        return None


def _link_quiz_to_lesson(lesson_name, quiz_name):
    """Link a quiz to a lesson.

    Writes to both:
      1. lesson.quiz_id (legacy field, harmless fallback)
      2. lesson.content JSON blocks (the frontend actually reads this for rich lessons)
    """
    import json
    try:
        lesson = frappe.get_doc("Course Lesson", lesson_name)

        # Legacy field — keep for backwards compatibility
        lesson.quiz_id = quiz_name

        # Parse content JSON (shape: {"blocks": [...], "time": ..., "version": ...})
        if lesson.content:
            try:
                content_data = json.loads(lesson.content)
            except Exception:
                content_data = {"blocks": []}
        else:
            content_data = {"blocks": []}

        if not isinstance(content_data, dict):
            content_data = {"blocks": []}
        if "blocks" not in content_data or not isinstance(content_data["blocks"], list):
            content_data["blocks"] = []

        # Idempotency: skip if a quiz block for this quiz already exists
        already_linked = any(
            (b or {}).get("type") == "quiz"
            and ((b or {}).get("data") or {}).get("quiz") == quiz_name
            for b in content_data["blocks"]
        )

        if not already_linked:
            content_data["blocks"].append({
                "id": f"quiz-{quiz_name[:20]}",
                "type": "quiz",
                "data": {"quiz": quiz_name},
            })

        lesson.content = json.dumps(content_data)
        lesson.save(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            f"[GeminiQuiz] Could not link quiz '{quiz_name}' to lesson '{lesson_name}'",
        )


def _build_quiz_title(lesson_title, chapter_title):
    prefix = "[AI]"
    if chapter_title:
        return f"{prefix} {chapter_title} — {lesson_title} Quiz"
    return f"{prefix} {lesson_title} Quiz"
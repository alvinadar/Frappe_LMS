import frappe


def create_quiz_for_lesson(course_name, lesson_name, lesson_title, chapter_title, questions):
    quiz_title = _build_quiz_title(lesson_title, chapter_title)

    # FIX: Find and delete any existing quiz with this exact title to prevent UniqueValidationError
    existing_quiz = frappe.db.exists("LMS Quiz", {"title": quiz_title})
    if existing_quiz:
        frappe.delete_doc("LMS Quiz", existing_quiz, ignore_permissions=True)
        frappe.db.commit()

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
    try:
        lesson = frappe.get_doc("Course Lesson", lesson_name)
        lesson.quiz_id = quiz_name
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
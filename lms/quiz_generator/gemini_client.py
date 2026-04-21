import json
import time
import frappe
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate
from lms.quiz_generator.vector_store import search_similar_chunks
from lms.quiz_generator.rate_limiter import wait_for_rate_limit

MAX_RETRIES = 3  # As per project requirements

def _get_api_key():
    import time as t
    keys = []
    for i in ['', '_2', '_3']:
        key = frappe.conf.get(f'gemini_api_key{i}')
        if key:
            keys.append(key)
    if not keys:
        return None
    return keys[int(t.time() / 60) % len(keys)]

def generate_questions_with_rag(lesson_title, num_questions=5):
    """Retrieves context from ChromaDB and generates questions using LangChain & Gemini."""
    api_key = _get_api_key()
    if not api_key:
        frappe.log_error("No Gemini API key found in site config.")
        return None

    # 1. Retrieval: Fetch top 7 most relevant chunks from ChromaDB
    frappe.logger().info(f"[RAG] Retrieving vector context for: {lesson_title}")
    results = search_similar_chunks(
        query_text=lesson_title, 
        n_results=7, 
        filter_metadata={"lesson_title": lesson_title}
    )
    
    if not results or not results['documents'] or not results['documents'][0]:
        frappe.logger().warning(f"[RAG] No vector context found for {lesson_title}. Did it ingest properly?")
        return None

    # Combine retrieved chunks into a single context string
    context_text = "\n\n---\n\n".join(results['documents'][0])

    # 2. Setup LangChain LLM
    llm = ChatGoogleGenerativeAI(
        model="gemini-2.0-flash",
        google_api_key=api_key,
        temperature=0.4,
        max_output_tokens=2048,
    )

    # 3. Define the Prompt Template
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

    # Combine prompt and LLM into a chain
    chain = prompt | llm

    # 4. Generate with Retry Logic
    for attempt in range(1, MAX_RETRIES + 1):
        wait_for_rate_limit()
        try:
            response = chain.invoke({
                "num_questions": num_questions,
                "context": context_text
            })
            
            questions = _parse_response(response.content)
            if questions:
                return questions
                
        except Exception as e:
            is_rate_limit = any(x in str(e).lower() for x in ['429', 'quota', 'exhausted'])
            if is_rate_limit:
                 frappe.logger().warning(f"[RAG] Rate limit hit. Waiting 60s...")
                 time.sleep(60)
            else:
                 frappe.logger().warning(f"[RAG] Generation attempt {attempt} failed: {e}")
                 time.sleep(5)
            
    frappe.log_error(f'[RAG] Failed to generate valid questions for {lesson_title} after {MAX_RETRIES} attempts.')
    return None

def _parse_response(raw_text):
    text = raw_text.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[-1]
    if text.endswith('```'):
        text = text.rsplit('```', 1)[0]
    try:
        data = json.loads(text.strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list):
        return None
    validated = [_validate_question(item) for item in data]
    validated = [q for q in validated if q]
    return validated if validated else None

def _validate_question(item):
    if not isinstance(item, dict): return None
    q_text = str(item.get('question', '')).strip()
    if not q_text: return None
    options = item.get('options', [])
    if not isinstance(options, list) or len(options) < 2: return None
    validated_opts = []
    correct_count = 0
    for opt in options:
        if not isinstance(opt, dict): continue
        text = str(opt.get('text', '')).strip()
        is_correct = bool(opt.get('is_correct', False))
        explanation = str(opt.get('explanation', '')).strip()
        if not text: continue
        if is_correct: correct_count += 1
        validated_opts.append({'text': text, 'is_correct': is_correct, 'explanation': explanation})
    if len(validated_opts) < 2 or correct_count != 1: return None
    return {'question': q_text, 'options': validated_opts[:4]}
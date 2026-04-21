import frappe
from mcp.server.fastmcp import FastMCP
from lms.quiz_generator.vector_store import search_similar_chunks

# Initialize the FastMCP Server
mcp = FastMCP("Frappe LMS Curriculum Server")

@mcp.resource("curriculum://{course_name}/{lesson_name}")
def get_lesson_content(course_name: str, lesson_name: str) -> str:
    """Exposes raw lesson content as a standardized MCP Resource."""
    # Ensure Frappe is initialized if this script runs standalone via STDIO
    if not frappe.db:
        frappe.init(site="lms.localhost")
        frappe.connect()
        
    try:
        lesson = frappe.get_doc("Course Lesson", lesson_name)
        return lesson.content or lesson.body or "No content found for this lesson."
    except Exception as e:
        frappe.log_error(str(e), "[MCP Server] Resource Fetch Error")
        return f"Error fetching lesson: {e}"

@mcp.tool()
def semantic_search_curriculum(query: str, n_results: int = 5) -> str:
    """MCP Tool to search the ChromaDB vector database for relevant curriculum context."""
    if not frappe.db:
        frappe.init(site="lms.localhost")
        frappe.connect()
        
    try:
        results = search_similar_chunks(query_text=query, n_results=n_results)
        if not results or not results['documents'] or not results['documents'][0]:
            return "No matching curriculum content found."
        
        # Combine the chunks into a readable string for the LLM
        return "\n\n---\n\n".join(results['documents'][0])
    except Exception as e:
        frappe.log_error(str(e), "[MCP Server] Tool Execution Error")
        return f"Search failed: {e}"

@mcp.prompt()
def quiz_generation_prompt(lesson_title: str, num_questions: int = 5) -> str:
    """Reusable MCP Prompt template for structured quiz generation."""
    return f'''You are an expert instructional designer.
    Based ONLY on the provided curriculum context, generate exactly {num_questions} multiple-choice questions for the lesson '{lesson_title}'.
    
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
    '''

if __name__ == "__main__":
    # Run the server using STDIO transport as specified in the architecture design
    mcp.run(transport='stdio')
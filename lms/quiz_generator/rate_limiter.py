import frappe
import time


REQUESTS_PER_MINUTE = 8  # Conservative limit (Tier 1 allows 60, we use 10 to be safe)
CACHE_KEY = "gemini_quiz_last_request_time"


def wait_for_rate_limit():
    """
    Ensures at least 6 seconds between each Gemini API call.
    (10 requests/min = 1 request every 6 seconds)
    Stores last request time in Frappe cache.
    """
    min_interval = 60.0 / REQUESTS_PER_MINUTE  # 6 seconds between calls

    try:
        last_request = frappe.cache.get_value(CACHE_KEY)

        if last_request:
            elapsed = time.time() - float(last_request)
            if elapsed < min_interval:
                wait_time = min_interval - elapsed
                frappe.logger().info(
                    f"[GeminiQuiz] Rate limiter: waiting {wait_time:.1f}s before next API call."
                )
                time.sleep(wait_time)

    except Exception:
        pass  # If cache fails, proceed anyway

    # Record this request time
    try:
        frappe.cache.set_value(CACHE_KEY, time.time(), expires_in_sec=120)
    except Exception:
        pass

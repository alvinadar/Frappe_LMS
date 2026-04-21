"""Landing page at "/" — redirects logged-in users to their role-appropriate view,
shows welcome page to anonymous visitors."""
import frappe


def get_context(context):
	"""Frappe calls this before rendering home.html.

	If the user is logged in, we set a redirect. Otherwise we fall through
	to render the welcome page.
	"""
	if frappe.session.user and frappe.session.user != "Guest":
		roles = set(frappe.get_roles(frappe.session.user))

		# Admin-like roles → Frappe desk
		if roles & {"System Manager", "Administrator"}:
			frappe.local.flags.redirect_location = "/app"
			raise frappe.Redirect

		# Tutors / course creators → tutor dashboard
		if roles & {"Course Creator", "Moderator"}:
			frappe.local.flags.redirect_location = "/lms/dashboard"
			raise frappe.Redirect

		# Students (or any logged-in user) → courses list
		frappe.local.flags.redirect_location = "/lms/courses"
		raise frappe.Redirect

	# Anonymous visitor — render welcome page
	context.no_cache = 1
	context.site_name = "Data Autopilot Learning"
	context.tagline = "AI-powered learning, built for curious minds."
	return context

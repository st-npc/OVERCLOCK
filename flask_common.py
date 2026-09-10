"""Flask app-setup boilerplate shared by app_main.py and app_helper.py: the
security-headers after_request hook and the generic 404/500 handlers were
byte-for-byte duplicated between the two files (each with its own CSP
string), so a fix to one was easy to forget in the other. Not used by
relay_server.py on purpose — that file is meant to be copy-pasted alone to
a separate host (see its module docstring), so it keeps its own inline copy
instead of depending on this module.
"""
from flask import jsonify

from security import apply_security_headers


def install_security_headers(app, csp: str):
    """Register an after_request hook that applies the standard hardening
    headers plus the given CSP to every response from `app`."""

    @app.after_request
    def _add_security_headers(resp):
        return apply_security_headers(resp, csp=csp)

    return _add_security_headers


def install_error_handlers(app):
    """Register the generic JSON 404/500 handlers used by both services,
    so neither ever falls back to Flask's default HTML error page."""

    @app.errorhandler(404)
    def not_found(_err):
        return jsonify({"error": "not found"}), 404

    @app.errorhandler(500)
    def server_error(_err):
        return jsonify({"error": "internal server error"}), 500

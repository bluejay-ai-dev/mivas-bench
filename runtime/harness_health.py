"""HTTP /health on the harness pod after the tool server moves to its own Deployment.

ALB still probes the CHIRP target's :8000. This process answers that probe; it is
not the industry tool server.
"""

from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class _Health(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return


def main() -> None:
    port = int(os.environ.get("TOOL_SERVER_PORT", "8000"))
    HTTPServer(("0.0.0.0", port), _Health).serve_forever()


if __name__ == "__main__":
    main()

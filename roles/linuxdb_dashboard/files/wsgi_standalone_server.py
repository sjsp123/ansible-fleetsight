#!/usr/bin/env python3
"""
Managed by Ansible (roles/linuxdb_dashboard) - do not edit by hand on target
hosts (edit roles/linuxdb_dashboard/files/wsgi_standalone_server.py in the
repo instead).

Serves app.py using only Python's standard library
(wsgiref.simple_server) - no Flask, no httpd, no third-party packages,
matching the same "no EPEL, no extra packages" approach as
roles/log_search_ui's standalone variant. Handles each request in its
own thread since app.py is a purely read-only, stateless file reader -
no shared mutable state, no database connection to worry about sharing
across threads.

Configuration comes from environment variables (set in the systemd
unit, see templates/linuxdb-dashboard.service.j2) rather than command
line arguments, so this file itself never needs templating - only the
unit file does.
"""

import base64
import os
import socketserver
import ssl
import sys
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

APP_DIR = os.environ.get("LINUXDB_DASHBOARD_APP_DIR", "/opt/linuxdb_dashboard")
HOST = os.environ.get("LINUXDB_DASHBOARD_HOST", "0.0.0.0")
PORT = int(os.environ.get("LINUXDB_DASHBOARD_PORT", "8082"))

sys.path.insert(0, APP_DIR)
import config  # noqa: E402 - TLS_ENABLED / TLS_CERT_PATH / TLS_KEY_PATH
from app import application  # noqa: E402


class ThreadingWSGIServer(socketserver.ThreadingMixIn, WSGIServer):
    """Handles each request in its own thread instead of one at a time.

    Safe here because app.py never writes anything and holds no shared
    mutable module-level state - every request only opens files under
    LINUXDB_REPORTS_DATA_DIR/HOSTINFO_CSV_PATH for reading. daemon_threads
    = True so in-flight request threads don't block process
    shutdown/restart (e.g. on a systemd restart).
    """

    daemon_threads = True


class JournaldFriendlyHandler(WSGIRequestHandler):
    """wsgiref's default log_message() writes bare lines to stderr with
    no extra structure. systemd captures stderr into the journal either
    way, but this keeps the per-request line a bit more readable in
    `journalctl -u linuxdb-dashboard` (client IP, timestamp, request line).

    Also fills in the traditional Common-Log-Format "remote user" field
    (normally always "-" in wsgiref) with the Basic-Auth username sent
    on this request, if any - so who authenticated is visible directly
    in `journalctl -u linuxdb-dashboard` without needing anything else
    enabled. This is a display label only, decoded from the same
    Authorization header app.py's own auth check already validated (or
    rejected) - never itself a source of truth for identity, and shown
    even when auth ultimately failed (a wrong password still logs the
    username that was tried, alongside the 401 status on that line).
    """

    def get_environ(self):
        # Stashed on self so log_message() - called after the request
        # has already been handled, see wsgiref.simple_server.ServerHandler.close()
        # - can still read the Authorization header that produced this response.
        self._environ = super().get_environ()
        return self._environ

    def _auth_username(self):
        header = getattr(self, "_environ", {}).get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Basic "):
            return "-"
        try:
            decoded = base64.b64decode(header[len("Basic "):]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return "-"
        user, _, _password = decoded.partition(":")
        return user or "-"

    def log_message(self, format_str, *args):  # noqa: A002 - matches base signature
        sys.stderr.write(
            "%s - %s [%s] %s\n"
            % (self.client_address[0], self._auth_username(), self.log_date_time_string(), format_str % args)
        )
        sys.stderr.flush()


def main():
    with make_server(
        HOST, PORT, application, server_class=ThreadingWSGIServer, handler_class=JournaldFriendlyHandler
    ) as httpd:
        scheme = "http"
        if getattr(config, "TLS_ENABLED", False):
            # Wrap the already-bound listening socket in TLS - same port,
            # no firewalld change, no reverse proxy. httpd.socket is the
            # raw listening socket wsgiref.simple_server created above;
            # replacing it here means every connection accept()'d from
            # this point on goes through the TLS handshake first.
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=config.TLS_CERT_PATH, keyfile=config.TLS_KEY_PATH)
            httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
            scheme = "https"
        sys.stderr.write(
            "linuxdb_dashboard server listening on %s://%s:%s (multi-threaded)\n" % (scheme, HOST, PORT)
        )
        sys.stderr.flush()
        httpd.serve_forever()


if __name__ == "__main__":
    main()

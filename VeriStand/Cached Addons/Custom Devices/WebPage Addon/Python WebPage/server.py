#!/usr/bin/env python3
"""
VeriStand Variable Dashboard Server
=====================================
A lightweight HTTP server for NI Linux RT that serves a live dashboard
showing control application variables, plus a gRPC server that accepts
a Shutdown command to cleanly stop everything.

Architecture:
  - Your control app writes variable values to /tmp/vs_variables.json
  - HTTP server reads that file and serves it at GET /data
  - The dashboard HTML page polls /data every second
  - A gRPC server listens for a Shutdown RPC to stop both servers

Usage:
  python3 server.py                              # HTTP :8080, gRPC :50051
  python3 server.py --port 9000 --grpc-port 50052
  python3 server.py --demo                       # simulated data for testing

Shutdown (from another terminal or application):
  python3 shutdown_client.py                     # sends Shutdown RPC
"""

import json
import os
import sys
import time
import signal
import argparse
import threading
import socket
from concurrent import futures
from http.server import HTTPServer, SimpleHTTPRequestHandler

import grpc
import dashboard_pb2
import dashboard_pb2_grpc

# --- Configuration -----------------------------------------------------------

if os.name == "nt":
    VARIABLES_FILE = "C:\\Users\\Public\\Documents\\National Instruments\\NI VeriStand 2026\\Custom Devices\\WebPage Addon\\Python Webpage\\vs_variables.json"
else:
    VARIABLES_FILE = "/tmp/vs_variables.json"
DASHBOARD_DIR = os.path.dirname(os.path.abspath(__file__))

# --- HTTP Request Handler ----------------------------------------------------

class DashboardHandler(SimpleHTTPRequestHandler):
    """Serves the dashboard HTML and a /data JSON endpoint."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DASHBOARD_DIR, **kwargs)

    def do_GET(self):
        if self.path == "/data":
            self._serve_data()
        elif self.path == "/" or self.path == "/index.html":
            self.path = "/index.html"
            super().do_GET()
        else:
            super().do_GET()

    def _serve_data(self):
        """Read the shared JSON file and return its contents."""
        try:
            with open(VARIABLES_FILE, "r") as f:
                data = json.load(f)
            data["_server_time"] = time.time()
            payload = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except FileNotFoundError:
            self._send_error(503, f"Waiting for control app — {VARIABLES_FILE} not found.")
        except json.JSONDecodeError:
            self._send_error(500, "Variables file contains invalid JSON.")
        except Exception as e:
            self._send_error(500, str(e))

    def _send_error(self, code, message):
        payload = json.dumps({"error": message}).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        if "/data" not in args[0]:
            super().log_message(fmt, *args)


# --- gRPC Shutdown Service ---------------------------------------------------

class DashboardControlServicer(dashboard_pb2_grpc.DashboardControlServicer):
    """gRPC servicer that handles the Shutdown RPC."""

    def __init__(self, http_server: HTTPServer, grpc_server: grpc.Server):
        self._http_server = http_server
        self._grpc_server = grpc_server

    def Shutdown(self, request, context):
        """
        Cleanly stop both the HTTP and gRPC servers.
        Returns error_code 0 on success, non-zero on failure.
        """
        print("\n[gRPC] Shutdown request received.")
        try:
            # Schedule the actual shutdown on a background thread so we
            # can still return the response to the caller first.
            threading.Thread(target=self._do_shutdown, daemon=True).start()
            return dashboard_pb2.ShutdownResponse(
                error_code=0,
                error_message=""
            )
        except Exception as e:
            return dashboard_pb2.ShutdownResponse(
                error_code=1,
                error_message=f"Shutdown failed: {e}"
            )

    def _do_shutdown(self):
        """Perform the actual shutdown sequence after a brief delay."""
        # Small delay to let the gRPC response go out on the wire.
        time.sleep(0.5)

        print("[gRPC] Stopping HTTP server...")
        self._http_server.shutdown()

        print("[gRPC] Stopping gRPC server...")
        # grace=2 gives in-flight RPCs 2 seconds to finish.
        self._grpc_server.stop(grace=2)

        print("[gRPC] All servers stopped. Exiting.")


# --- Main --------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="VeriStand Dashboard Server")
    parser.add_argument("--port", type=int, default=8382,
                        help="HTTP port (default 8382)")
    parser.add_argument("--grpc-port", type=int, default=53251,
                        help="gRPC port (default 53251)")
    parser.add_argument("--ip", type=str, default="192.168.68.70",
                        help="ip (default 192.168.68.70)")
    parser.add_argument("--demo", action="store_true",
                        help="Write simulated data for testing")
    args = parser.parse_args()

    # ── Start HTTP server ────────────────────────────────────────────

    http_server = HTTPServer((args.ip, args.port), DashboardHandler)
    http_thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    http_thread.start()
    print(f"[HTTP] Dashboard running at http://{args.ip}:{args.port}")
    print(f"[HTTP] Reading variables from {VARIABLES_FILE}")

    # ── Start gRPC server ────────────────────────────────────────────
    grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
    servicer = DashboardControlServicer(http_server, grpc_server)
    dashboard_pb2_grpc.add_DashboardControlServicer_to_server(servicer, grpc_server)
    grpc_server.add_insecure_port(f"{args.ip}:{args.grpc_port}")
    grpc_server.start()
    print(f"[gRPC] Shutdown listener on port {args.grpc_port}")

    # ── Block until gRPC server terminates (via Shutdown RPC or Ctrl-C) ──
    try:
        grpc_server.wait_for_termination()
    except KeyboardInterrupt:
        print("\n[SIGINT] Shutting down...")
        http_server.shutdown()
        grpc_server.stop(grace=2)

    print("Server exited.")

if __name__ == "__main__":
    main()

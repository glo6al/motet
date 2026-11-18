"""HTTP listeners for offline tests, independent of reverse DNS."""
from http.server import ThreadingHTTPServer
from socketserver import TCPServer


class LoopbackHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # The validated fixture address is numeric. HTTPServer otherwise
        # does a synchronous reverse lookup just to name the listener.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

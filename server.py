import email.utils
import html
import http.client
import os
import socket
import socketserver
import sys
import time
import contextlib
from functools import partial

from http import HTTPStatus
import keyword
import app
import http.server
__version__ = "0.6"


INDEX_MODULE = app
# Default error message template

DEFAULT_ERROR_MESSAGE = """{
    "error_code": %(code)d,
    "error_msg": %(message)s,
    "details": %(code)s - %(explain)s
}"""

DEFAULT_ERROR_CONTENT_TYPE = "application/json"

class HTTPServer(socketserver.TCPServer):

    allow_reuse_address = 1    # Seems to make sense in testing environment

    def server_bind(self):
        """Override server_bind to store the server name."""
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = socket.getfqdn(host)
        self.server_port = port


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


class BaseHTTPRequestHandler(socketserver.StreamRequestHandler):

    # The Python system version, truncated to its first component.
    sys_version = "Python/" + sys.version.split()[0]

    # The server software version.  You may want to override this.
    # The format is multiple whitespace-separated strings,
    # where each string is of the form name[/version].
    server_version = "BaseHTTP/" + __version__

    error_message_format = DEFAULT_ERROR_MESSAGE
    error_content_type = DEFAULT_ERROR_CONTENT_TYPE

    # The default request version.  This only affects responses up until
    # the point where the request line is parsed, so it mainly decides what
    # the client gets back when sending a malformed request line.
    # Most web servers default to HTTP 0.9, i.e. don't send a status line.
    default_request_version = "HTTP/0.9"

    def parse_request(self):
        """Parse a request (internal).

        The request should be stored in self.raw_requestline; the results
        are in self.command, self.path, self.request_version and
        self.headers.

        Return True for success, False for failure; on failure, any relevant
        error response has already been sent back.

        """
        self.command = None  # set in case of error on the first line
        self.request_version = version = self.default_request_version
        self.close_connection = True
        requestline = str(self.raw_requestline, 'iso-8859-1')
        requestline = requestline.rstrip('\r\n')
        self.requestline = requestline
        words = requestline.split()
        if len(words) == 0:
            return False

        if len(words) >= 3:  # Enough to determine protocol version
            version = words[-1]
            try:
                if not version.startswith('HTTP/'):
                    raise ValueError
                base_version_number = version.split('/', 1)[1]
                version_number = base_version_number.split(".")
                # RFC 2145 section 3.1 says there can be only one "." and
                #   - major and minor numbers MUST be treated as
                #      separate integers;
                #   - HTTP/2.4 is a lower version than HTTP/2.13, which in
                #      turn is lower than HTTP/12.3;
                #   - Leading zeros MUST be ignored by recipients.
                if len(version_number) != 2:
                    raise ValueError
                version_number = int(version_number[0]), int(version_number[1])
            except (ValueError, IndexError):
                self.send_error(
                    HTTPStatus.BAD_REQUEST,
                    "Bad request version (%r)" % version)
                return False
            if version_number >= (1, 1) and self.protocol_version >= "HTTP/1.1":
                self.close_connection = False
            if version_number >= (2, 0):
                self.send_error(
                    HTTPStatus.HTTP_VERSION_NOT_SUPPORTED,
                    "Invalid HTTP version (%s)" % base_version_number)
                return False
            self.request_version = version

        if not 2 <= len(words) <= 3:
            self.send_error(
                HTTPStatus.BAD_REQUEST,
                "Bad request syntax (%r)" % requestline)
            return False
        command, path = words[:2]
        if len(words) == 2:
            self.close_connection = True
            if command != 'GET':
                self.send_error(
                    HTTPStatus.BAD_REQUEST,
                    "Bad HTTP/0.9 request type (%r)" % command)
                return False
            
        
        self.command, self.path = command, path

        # Examine the headers and look for a Connection directive.
        try:
            self.headers = http.client.parse_headers(self.rfile,
                                                     _class=http.client.HTTPMessage)
        except http.client.LineTooLong as err:
            self.send_error(
                HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE,
                "Line too long",
                str(err))
            return False
        except http.client.HTTPException as err:
            self.send_error(
                HTTPStatus.REQUEST_HEADER_FIELDS_TOO_LARGE,
                "Too many headers",
                str(err)
            )
            return False

        conntype = self.headers.get('Connection', "")
        if conntype.lower() == 'close':
            self.close_connection = True
        elif (conntype.lower() == 'keep-alive' and
              self.protocol_version >= "HTTP/1.1"):
            self.close_connection = False
        # Examine the headers and look for an Expect directive
        expect = self.headers.get('Expect', "")
        if (expect.lower() == "100-continue" and
                self.protocol_version >= "HTTP/1.1" and
                self.request_version >= "HTTP/1.1"):
            if not self.handle_expect_100():
                return False
        return True

    def handle_expect_100(self):
        """Decide what to do with an "Expect: 100-continue" header.

        If the client is expecting a 100 Continue response, we must
        respond with either a 100 Continue or a final response before
        waiting for the request body. The default is to always respond
        with a 100 Continue. You can behave differently (for example,
        reject unauthorized requests) by overriding this method.

        This method should either return True (possibly after sending
        a 100 Continue response) or send an error response and return
        False.

        """
        self.send_response_only(HTTPStatus.CONTINUE)
        self.end_headers()
        return True

    def handle_one_request(self):
        """Handle a single HTTP request.

        You normally don't need to override this method; see the class
        __doc__ string for information on how to handle specific HTTP
        commands such as GET and POST.

        """
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ''
                self.request_version = ''
                self.command = ''
                self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                # An error code has been sent, just exit
                return
            mname = 'do_' + self.command
            if not hasattr(self, mname):
                self.send_error(
                    HTTPStatus.NOT_IMPLEMENTED,
                    "Unsupported method (%r)" % self.command)
                return
            method = getattr(self, mname)
            method()
            self.wfile.flush() #actually send the response if not already done.
        except socket.timeout as e:
            #a read or a write timed out.  Discard this connection
            self.log_error("Request timed out: %r", e)
            self.close_connection = True
            return

    def handle(self):
        """Handle multiple requests if necessary."""
        self.close_connection = True

        self.handle_one_request()
        while not self.close_connection:
            self.handle_one_request()

    def send_error(self, code, message=None, explain=None):
        """Send and log an error reply.

        Arguments are
        * code:    an HTTP error code
                   3 digits
        * message: a simple optional 1 line reason phrase.
                   *( HTAB / SP / VCHAR / %x80-FF )
                   defaults to short entry matching the response code
        * explain: a detailed message defaults to the long entry
                   matching the response code.

        This sends an error response (so it must be called before any
        output has been generated), logs the error, and finally sends
        a piece of HTML explaining the error to the user.

        """

        try:
            shortmsg, longmsg = self.responses[code]
        except KeyError:
            shortmsg, longmsg = '???', '???'
        if message is None:
            message = shortmsg
        if explain is None:
            explain = longmsg
        self.log_error("code %d, message %s", code, message)
        self.send_response(code, message)
        self.send_header('Connection', 'close')

        # Message body is omitted for cases described in:
        #  - RFC7230: 3.3. 1xx, 204(No Content), 304(Not Modified)
        #  - RFC7231: 6.3.6. 205(Reset Content)
        body = None
        if (code >= 200 and
            code not in (HTTPStatus.NO_CONTENT,
                         HTTPStatus.RESET_CONTENT,
                         HTTPStatus.NOT_MODIFIED)):
            # HTML encode to prevent Cross Site Scripting attacks
            # (see bug #1100201)
            content = (self.error_message_format % {
                'code': code,
                'message': html.escape(message, quote=False),
                'explain': html.escape(explain, quote=False)
            })
            body = content.encode('UTF-8', 'replace')
            self.send_header("Content-Type", self.error_content_type)
            self.send_header('Content-Length', str(len(body)))
        self.end_headers()

        if self.command != 'HEAD' and body:
            self.wfile.write(body)

    def send_response(self, code, message=None):
        """Add the response header to the headers buffer and log the
        response code.

        Also send two standard headers with the server software
        version and the current date.

        """
        self.log_request(code)
        self.send_response_only(code, message)
        self.send_header('Server', self.version_string())
        self.send_header('Date', self.date_time_string())

    def send_response_only(self, code, message=None):
        """Send the response header only."""
        if self.request_version != 'HTTP/0.9':
            if message is None:
                if code in self.responses:
                    message = self.responses[code][0]
                else:
                    message = ''
            if not hasattr(self, '_headers_buffer'):
                self._headers_buffer = []
            self._headers_buffer.append(("%s %d %s\r\n" %
                    (self.protocol_version, code, message)).encode(
                        'latin-1', 'strict'))

    def send_header(self, keyword, value):
        """Send a MIME header to the headers buffer."""
        if self.request_version != 'HTTP/0.9':
            if not hasattr(self, '_headers_buffer'):
                self._headers_buffer = []
            self._headers_buffer.append(
                ("%s: %s\r\n" % (keyword, value)).encode('latin-1', 'strict'))

        if keyword.lower() == 'connection':
            if value.lower() == 'close':
                self.close_connection = True
            elif value.lower() == 'keep-alive':
                self.close_connection = False

    def end_headers(self):
        """Send the blank line ending the MIME headers."""
        if self.request_version != 'HTTP/0.9':
            self._headers_buffer.append(b"\r\n")
            self.flush_headers()

    def flush_headers(self):
        if hasattr(self, '_headers_buffer'):
            self.wfile.write(b"".join(self._headers_buffer))
            self._headers_buffer = []

    def log_request(self, code='-', size='-'):
        """Log an accepted request.

        This is called by send_response().

        """
        if isinstance(code, HTTPStatus):
            code = code.value
        self.log_message('"%s" %s %s',
                         self.requestline, str(code), str(size))

    def log_error(self, format, *args):
        """Log an error.

        This is called when a request cannot be fulfilled.  By
        default it passes the message on to log_message().

        Arguments are the same as for log_message().

        XXX This should go to the separate error log.

        """

        self.log_message(format, *args)

    def log_message(self, format, *args):
        """Log an arbitrary message.

        This is used by all other logging functions.  Override
        it if you have specific logging wishes.

        The first argument, FORMAT, is a format string for the
        message to be logged.  If the format string contains
        any % escapes requiring parameters, they should be
        specified as subsequent arguments (it's just like
        printf!).

        The client ip and current date/time are prefixed to
        every message.

        """

        sys.stderr.write("%s - - [%s] %s\n" %
                         (self.address_string(),
                          self.log_date_time_string(),
                          format%args))

    def version_string(self):
        """Return the server software version string."""
        return self.server_version + ' ' + self.sys_version

    def date_time_string(self, timestamp=None):
        """Return the current date and time formatted for a message header."""
        if timestamp is None:
            timestamp = time.time()
        return email.utils.formatdate(timestamp, usegmt=True)

    def log_date_time_string(self):
        """Return the current time formatted for logging."""
        now = time.time()
        year, month, day, hh, mm, ss, x, y, z = time.localtime(now)
        s = "%02d/%3s/%04d %02d:%02d:%02d" % (
                day, self.monthname[month], year, hh, mm, ss)
        return s

    weekdayname = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

    monthname = [None,
                 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

    def address_string(self):
        """Return the client address."""

        return self.client_address[0]

    # Essentially static class variables

    # The version of the HTTP protocol we support.
    # Set this to HTTP/1.1 to enable automatic keepalive
    protocol_version = "HTTP/1.0"


    # hack to maintain backwards compatibility
    responses = {
        v: (v.phrase, v.description)
        for v in HTTPStatus.__members__.values()
    }


class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):

    """Simple HTTP request handler with GET and HEAD commands.

    This serves files from the current directory and any of its
    subdirectories.  The MIME type for files is determined by
    calling the .guess_type() method.

    The GET and HEAD requests are identical except that the HEAD
    request omits the actual contents of the file.

    """

    server_version = "SimpleHTTP/" + __version__

    def __init__(self, *args, directory=None, **kwargs):
        if directory is None:
            directory = os.getcwd()
        self.directory = directory
        super().__init__(*args, **kwargs)

    def do_GET(self):
        """Serve a GET request."""
        self.parse_body()
        self.send_response(HTTPStatus.OK)
        self.send_body()
#

    def do_HEAD(self):
        """Serve a HEAD request."""
        f = self.send_head()
        if f:
            f.close()

    def determine_function(self, path):

        path = path.strip('')

        if path.isidentifier() and not keyword.iskeyword(path):

            if hasattr(INDEX_MODULE, path) and callable(getattr(INDEX_MODULE, path)):
                return INDEX_MODULE.getattr(path)
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "File not found")
                return None
        else:
            self.send_error(HTTPStatus.BAD_REQUEST, "File not found")
            return None


    def process_function_call(self, function):
        pass
        

    def parse_body(self):

        content_type = self.headers.get('Content-Type')
        
        content_type = content_type.split(';')[0] if content_type else None
        if content_type != 'application/json':
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)

        content_length = self.headers.get('Content-Length')
        if not content_length:
            self.send_error(HTTPStatus.BAD_REQUEST)

        self.raw_request_body = self.rfile.read(int(content_length))
        

    def send_body(self):
        
        body = self.raw_request_body.decode("utf-8")
        body = body.encode('UTF-8', 'replace')

        self.send_header("Content-Type", 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()

        self.wfile.write(body)
    
def _get_best_family(*address):
    infos = socket.getaddrinfo(
        *address,
        type=socket.SOCK_STREAM,
        flags=socket.AI_PASSIVE,
    )
    family, type, proto, canonname, sockaddr = next(iter(infos))
    return family, sockaddr


def test(HandlerClass=BaseHTTPRequestHandler,
         ServerClass=ThreadingHTTPServer,
         protocol="HTTP/1.0", port=8000, bind=None):
    """Test the HTTP request handler class.

    This runs an HTTP server on port 8000 (or the port argument).

    """
    ServerClass.address_family, addr = _get_best_family(bind, port)

    HandlerClass.protocol_version = protocol
    with ServerClass(addr, HandlerClass) as httpd:
        host, port = httpd.socket.getsockname()[:2]
        url_host = f'[{host}]' if ':' in host else host
        print(
            f"Serving HTTP on {host} port {port} "
            f"(http://{url_host}:{port}/) ..."
        )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nKeyboard interrupt received, exiting.")
            sys.exit(0)

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--bind', '-b', metavar='ADDRESS',
                        help='Specify alternate bind address '
                             '[default: all interfaces]')
    parser.add_argument('port', action='store',
                        default=8000, type=int,
                        nargs='?',
                        help='Specify alternate port [default: 8000]')
    args = parser.parse_args()
    
    handler_class = partial(SimpleHTTPRequestHandler,
                                directory=args.directory)

    # ensure dual-stack is not disabled; ref #38907
    class DualStackServer(ThreadingHTTPServer):
        def server_bind(self):
            # suppress exception when protocol is IPv4
            with contextlib.suppress(Exception):
                self.socket.setsockopt(
                    socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            return super().server_bind()

    test(
        HandlerClass=handler_class,
        ServerClass=DualStackServer,
        port=args.port,
        bind=args.bind,
    )

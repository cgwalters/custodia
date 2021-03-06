# Copyright (C) 2015  Custodia Project Contributors - see LICENSE file

try:
    # pylint: disable=import-error
    from BaseHTTPServer import BaseHTTPRequestHandler
    from SocketServer import ForkingMixIn, UnixStreamServer
    from urlparse import urlparse, parse_qs
    from urllib import unquote
except ImportError:
    # pylint: disable=import-error,no-name-in-module
    from http.server import BaseHTTPRequestHandler
    from socketserver import ForkingMixIn, UnixStreamServer
    from urllib.parse import urlparse, parse_qs, unquote
from custodia.log import stacktrace
from custodia.log import debug as log_debug
import os
import shutil
import six
import socket
import struct

SO_PEERCRED = 17
MAX_REQUEST_SIZE = 10*1024*1024  # For now limit body to 10MiB


class HTTPError(Exception):

    def __init__(self, code=None, message=None):
        self.code = code if code is not None else 500
        self.mesg = message
        errstring = '%d: %s' % (self.code, self.mesg)
        log_debug(errstring)
        super(HTTPError, self).__init__(errstring)


class ForkingLocalHTTPServer(ForkingMixIn, UnixStreamServer):

    """
    A forking HTTP Server.
    Each request runs into a forked server so that the whole environment
    is clean and isolated, and parallel requests cannot unintentionally
    influence one another.

    When a request is received it is parsed by the handler_class provided
    at server initialization.
    """

    server_string = "Custodia/0.1"
    allow_reuse_address = True
    socket_file = None

    def __init__(self, server_address, handler_class, config):
        UnixStreamServer.__init__(self, server_address, handler_class)
        if 'consumers' not in config:
            raise ValueError('Configuration does not provide any consumer')
        self.config = config
        if 'server_string' in self.config:
            self.server_string = self.config['server_string']

    def server_bind(self):
        oldmask = os.umask(000)
        UnixStreamServer.server_bind(self)
        os.umask(oldmask)
        self.socket_file = self.socket.getsockname()


class LocalHTTPRequestHandler(BaseHTTPRequestHandler):

    """
    This request handler is a slight modification of BaseHTTPRequestHandler
    where the per-request handler is replaced.

    When a request comes in it is parsed and the 'request' dictionary is
    populated accordingly. Additionally a 'creds' structure is added to the
    request.

    The 'creds' structure contains the data retrieved via a call to
    getsockopt with the SO_PEERCRED option. This retrieves via kernel assist
    the uid,gid and pid of the process on the other side of the unix socket
    on which the request has been made. This can be used for authentication
    and/or authorization purposes.

    after the request is parsed the server's pipeline() function is invoked
    in order to handle it. The pipeline() should return a response object,
    where te return 'code', the 'output' and 'headers' may be found.

    If no 'code' is present the request is assumed to be successful and a
    '200 OK' status code will be sent back to the client.

    The 'output' parameter can be a string or a file like object.

    The 'headers' objct must be a dictionary where keys are headers names.

    By default we assume HTTP1.0
    """

    protocol_version = "HTTP/1.0"

    def __init__(self, *args, **kwargs):
        BaseHTTPRequestHandler.__init__(self, *args, **kwargs)
        self.requestline = ''
        self.request_version = ''
        self.command = ''
        self.raw_requestline = None
        self.close_connection = 0
        self.path = None
        self.query = None
        self.url = None
        self.body = None

    def version_string(self):
        return self.server.server_string

    @property
    def peer_creds(self):

        creds = self.request.getsockopt(socket.SOL_SOCKET, SO_PEERCRED,
                                        struct.calcsize('3i'))
        pid, uid, gid = struct.unpack('3i', creds)
        return {'pid': pid, 'uid': uid, 'gid': gid}

    def parse_request(self, *args, **kwargs):
        if not BaseHTTPRequestHandler.parse_request(self, *args, **kwargs):
            return False

        # after basic parsing also use urlparse to retrieve individual
        # elements of a request.
        url = urlparse(self.path)

        # Yes, override path with the path part only
        self.path = unquote(url.path)

        # Create dict out of query
        self.query = parse_qs(url.query)

        # keep the rest into the 'url' element in case someone needs it
        self.url = url

        return True

    def parse_body(self):
        length = int(self.headers.get('content-length', 0))
        if length > MAX_REQUEST_SIZE:
            raise HTTPError(413)
        if length == 0:
            self.body = None
        else:
            self.body = self.rfile.read(length)

    def handle_one_request(self):
        # Set a fake client address to make log functions happy
        self.client_address = ['127.0.0.1', 0]
        try:
            if not self.server.config:
                self.close_connection = 1
                return
            self.raw_requestline = self.rfile.readline(65537)
            if not self.raw_requestline:
                self.close_connection = 1
                return
            if len(self.raw_requestline) > 65536:
                self.requestline = ''
                self.request_version = ''
                self.command = ''
                self.send_error(414)
                self.wfile.flush()
                return
            if not self.parse_request():
                self.close_connection = 1
                return
            try:
                self.parse_body()
            except HTTPError as e:
                self.send_error(e.code, e.mesg)
                self.wfile.flush()
                return
            request = {'creds': self.peer_creds,
                       'command': self.command,
                       'path': self.path,
                       'query': self.query,
                       'url': self.url,
                       'version': self.request_version,
                       'headers': self.headers,
                       'body': self.body}
            try:
                response = self.pipeline(self.server.config, request)
                if response is None:
                    raise HTTPError(500)
            except HTTPError as e:
                self.send_error(e.code, e.mesg)
                self.wfile.flush()
                return
            except socket.timeout as e:
                self.log_error("Request timed out: %r", e)
                self.close_connection = 1
                return
            except Exception as e:  # pylint: disable=broad-except
                self.log_error("Handler failed: %r", e)
                self.log_traceback()
                self.send_error(500)
                self.wfile.flush()
                return
            self.send_response(response.get('code', 200))
            for header, value in six.iteritems(response.get('headers', {})):
                self.send_header(header, value)
            self.end_headers()
            output = response.get('output', None)
            if hasattr(output, 'read'):
                shutil.copyfileobj(output, self.wfile)
                output.close()
            elif output is not None:
                self.wfile.write(str(output).encode('utf-8'))
            else:
                self.close_connection = 1
            self.wfile.flush()
            return
        except socket.timeout as e:
            self.log_error("Request timed out: %r", e)
            self.close_connection = 1
            return

    def log_traceback(self):
        self.log_error('Traceback:\n%s' % stacktrace())

    def pipeline(self, config, request):
        """
        The pipeline() function handles authentication and invocation of
        the correct consumer based on the server configuration, that is
        provided at initialization time.

        When authentication is performed all the authenticators are
        executed. If any returns False, authentication fails and a 403
        error is raised. If none of them positively succeeds and they all
        return None then also authentication fails and a 403 error is
        raised. Authentication plugins can add attributes to the request
        object for use of authorization or other plugins.

        When authorization is performed and positive result will cause the
        operation to be accepted and any negative result will cause it to
        fail. If no authorization plugin returns a positive result a 403
        error is returned.

        Once authentication and authorization are successful the pipeline
        will parse the path component and find the consumer plugin that
        handles the provided path walking up the path component by
        component until a consumer is found.

        Paths are walked up from the leaf to the root, so if two consumers
        hang on the same tree, the one closer to the leaf will be used. If
        there is a trailing path when the conumer is selected then it will
        be stored in the request dicstionary named 'trail'. The 'trail' is
        an ordered list of the path components below the consumer entry
        point.
        """

        # auth framework here
        authers = config.get('authenticators')
        if authers is None:
            raise HTTPError(403)
        valid_once = False
        for auth in authers:
            valid = authers[auth].handle(request)
            if valid is False:
                raise HTTPError(403)
            elif valid is True:
                valid_once = True
        if valid_once is not True:
            raise HTTPError(403)

        # auhz framework here
        authzers = config.get('authorizers')
        if authzers is None:
            raise HTTPError(403)
        for authz in authzers:
            valid = authzers[authz].handle(request)
            if valid is not None:
                break
        if valid is not True:
            raise HTTPError(403)

        # Select consumer
        path = request.get('path', '')
        if not os.path.isabs(path):
            raise HTTPError(400)

        trail = []
        while path != '':
            if path in config['consumers']:
                con = config['consumers'][path]
                if len(trail) != 0:
                    request['trail'] = trail
                return con.handle(request)
            if path == '/':
                path = ''
            else:
                head, tail = os.path.split(path)
                trail.insert(0, tail)
                path = head

        raise HTTPError(404)


class LocalHTTPServer(object):

    def __init__(self, address, config):
        if address[0] != '/':
            raise ValueError('Must use absolute unix socket name')
        if os.path.exists(address):
            os.remove(address)
        self.httpd = ForkingLocalHTTPServer(address, LocalHTTPRequestHandler,
                                            config)

    def get_socket(self):
        return (self.httpd.socket, self.httpd.socket_file)

    def serve(self):
        return self.httpd.serve_forever()

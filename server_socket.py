import socketserver

class TestHandler(socketserver.ThreadingMixIn, socketserver.StreamRequestHandler):
    daemon_threads = True
    def handle(self) -> None:
        while True:
            data = self.rfile.readline()
            print(data)
            if data == b'close\n':
                break
                
            self.wfile.write(b'Hello '+data)

        

server = socketserver.TCPServer(('localhost',8000), TestHandler)
server.serve_forever()

import select
import socket
import threading
import time
from stage_common import connect
import socketserver
from contextlib import closing

def relay(left,right):
    with closing(left),closing(right):
        while True:
            ready,_,_=select.select([left,right],[],[],160)
            if not ready:return
            for source in ready:
                buf=source.recv(65536)
                if not buf:return
                (right if source is left else left).sendall(buf)

def to_widget(ch,origin,bind):
    def work():
        try: relay(ch,socket.create_connection(("127.0.0.1",8764),8))
        except OSError:ch.close()
    threading.Thread(target=work,daemon=True).start()

class Local(socketserver.ThreadingTCPServer):
    allow_reuse_address=True
    daemon_threads=True
class StageFeed(socketserver.BaseRequestHandler):
    def handle(self):
        try:relay(self.request,self.server.transport.open_channel("direct-tcpip",("127.0.0.1",18094),self.client_address,timeout=8))
        except OSError:pass
while True:
    try:
        with closing(connect()) as tr, Local(("127.0.0.1",18765),StageFeed) as server:
            tr.set_keepalive(25)
            tr.request_port_forward("127.0.0.1",18766,to_widget)
            server.transport=tr
            server.timeout=1
            while tr.is_active():server.handle_request()
    except Exception as exc:
        print(type(exc).__name__,flush=True);time.sleep(6)

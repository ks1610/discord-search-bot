import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import bot 

class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b"Bot fu is running!")

    # Ghi đè log để tránh làm rác Terminal của Render
    def log_message(self, format, *args):
        return

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    print(f"HTTP Server listening on port {port}...")
    server.serve_forever()

if __name__ == "__main__":
    # 1. Chạy HTTP Server ở background thread
    threading.Thread(target=run_http_server, daemon=True).start()
    
    # 2. Khởi chạy bot Discord chính
    # Đảm bảo file bot.py có biến TOKEN và đối tượng bot
    bot.bot.run(bot.TOKEN)
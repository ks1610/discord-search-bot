import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import bot

# HTTP Server nhẹ để lừa Render/Koyeb là ứng dụng Web
class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b"Bot fu is running!")

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    print(f"HTTP Server listening on port {port}...")
    server.serve_forever()

if __name__ == "__main__":
    # Chạy HTTP Server ở background thread
    threading.Thread(target=run_http_server, daemon=True).start()
    
    # Khởi chạy bot Discord chính
    bot.bot.run(bot.TOKEN)
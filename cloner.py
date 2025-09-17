from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit
import os
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote
import re
import base64
import mimetypes
import threading
from threading import Thread
import time
import webbrowser
import http.server
import socketserver
import socket
from pathlib import Path
import zipfile
import json

# Ensure gevent is used as the async engine
from gevent import monkey
monkey.patch_all()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'web_cloner_secret_key'
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

# Global variables for server management
preview_servers = {}  # Store multiple preview servers
current_preview_port = 9000

class WebClonerCore:
    """Core web cloning functionality - with enhancements"""
    
    def __init__(self, socketio_instance=None):
        self.socketio = socketio_instance
        self.downloaded_resources = set()
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
    
    def emit_status(self, message):
        """Emit status update via SocketIO"""
        if self.socketio:
            self.socketio.emit('status_update', {'message': message})
        print(f"Status: {message}")
    
    def clone_website(self, url, output_base_dir):
        """Main cloning function"""
        try:
            self.emit_status("Starting website cloning...")
            
            # Parse URL and create output directory
            parsed_url = urlparse(url)
            domain = parsed_url.netloc.replace(':', '_')
            output_dir = os.path.join(output_base_dir, domain)
            
            # Create output directory
            os.makedirs(output_dir, exist_ok=True)
            
            # Create assets directory
            assets_dir = os.path.join(output_dir, 'assets')
            os.makedirs(assets_dir, exist_ok=True)
            
            visited = set()
            main_file_path = self.get_local_filepath(url, output_dir)
            self.clone_page(url, main_file_path, url, assets_dir, visited, output_dir, depth=0, max_depth=2)
            
            # Create ZIP file
            self.emit_status("Creating downloadable archive...")
            zip_path = self.create_zip_archive(output_dir)
            
            self.emit_status(f"Website cloned successfully!")
            
            return {
                'success': True,
                'output_dir': output_dir,
                'zip_path': zip_path,
                'domain': domain
            }
            
        except Exception as e:
            self.emit_status(f"Error: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def clone_page(self, page_url, file_path, base_url, assets_dir, visited, output_dir, depth, max_depth):
        """Recursively clone a page and its internal links"""
        if depth > max_depth:
            return
        if page_url in visited:
            return
        visited.add(page_url)
        
        try:
            self.emit_status(f"Downloading and processing page: {page_url} (depth {depth})")
            response = self.session.get(page_url, timeout=30)
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            
            self.process_images(soup, base_url, assets_dir)
            self.process_css_files(soup, base_url, assets_dir)
            self.process_js_files(soup, base_url, assets_dir)
            
            # Process internal links recursively
            for link in soup.find_all('a', href=True):
                href = link['href']
                if not href or href.startswith(('#', 'javascript:', 'mailto:')):
                    continue
                sub_url = urljoin(base_url, href).split('#')[0]
                sub_parsed = urlparse(sub_url)
                if sub_parsed.netloc != urlparse(page_url).netloc:
                    continue
                sub_file_path = self.get_local_filepath(sub_url, output_dir)
                self.clone_page(sub_url, sub_file_path, sub_url, assets_dir, visited, output_dir, depth + 1, max_depth)
                rel_href = os.path.relpath(sub_file_path, os.path.dirname(file_path)).replace('\\', '/')
                link['href'] = rel_href
            
            # Save processed HTML
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(str(soup))
        
        except Exception as e:
            print(f"Error cloning page {page_url}: {e}")
    
    def get_local_filepath(self, url, output_dir):
        """Generate a local file path for a URL"""
        parsed = urlparse(url)
        path = unquote(parsed.path)
        query = parsed.query
        full_path = path + ('?' + query if query else '')
        if full_path == '' or full_path == '/':
            return os.path.join(output_dir, 'index.html')
        if full_path.endswith('/'):
            dir_path = os.path.join(output_dir, full_path.lstrip('/').rstrip('/'))
            os.makedirs(dir_path, exist_ok=True)
            return os.path.join(dir_path, 'index.html')
        else:
            dir_path = os.path.join(output_dir, os.path.dirname(full_path.lstrip('/')))
            filename = os.path.basename(full_path)
            filename = re.sub(r'[^a-zA-Z0-9\.\-_]', '_', filename)
            if not filename:
                filename = 'index.html'
            elif not os.path.splitext(filename)[1]:
                filename += '.html'
            os.makedirs(dir_path, exist_ok=True)
            return os.path.join(dir_path, filename)
    
    def create_zip_archive(self, source_dir):
        """Create ZIP archive of cloned website"""
        zip_name = f"{os.path.basename(source_dir)}_cloned.zip"
        zip_path = os.path.join(os.path.dirname(source_dir), zip_name)
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(source_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arc_name = os.path.relpath(file_path, source_dir)
                    zipf.write(file_path, arc_name)
        
        return zip_path
    
    def process_images(self, soup, base_url, assets_dir):
        """Download and process all images"""
        img_tags = soup.find_all(['img', 'source'])
        
        for i, tag in enumerate(img_tags):
            try:
                img_url = None
                if tag.get('src'):
                    img_url = tag['src']
                elif tag.get('data-src'):
                    img_url = tag['data-src']
                elif tag.get('data-lazy-src'):
                    img_url = tag['data-lazy-src']
                
                if img_url:
                    if tag.get('srcset'):
                        srcset_urls = self.parse_srcset(tag['srcset'])
                        for srcset_url in srcset_urls:
                            self.download_resource(srcset_url, base_url, assets_dir)
                    
                    local_path = self.download_resource(img_url, base_url, assets_dir)
                    if local_path:
                        tag['src'] = local_path
                        for attr in ['data-src', 'data-lazy-src', 'loading']:
                            if tag.get(attr):
                                del tag[attr]
                
            except Exception as e:
                print(f"Error processing image {i}: {e}")
        
        self.process_css_background_images(soup, base_url, assets_dir)
    
    def parse_srcset(self, srcset):
        """Parse srcset attribute to extract URLs"""
        urls = []
        if srcset:
            parts = srcset.split(',')
            for part in parts:
                url = part.strip().split()[0]
                urls.append(url)
        return urls
    
    def process_css_background_images(self, soup, base_url, assets_dir):
        """Process CSS background images"""
        for tag in soup.find_all(attrs={'style': True}):
            style = tag['style']
            urls = re.findall(r'url\(["\']?([^"\']+)["\']?\)', style)
            for url in urls:
                local_path = self.download_resource(url, base_url, assets_dir)
                if local_path:
                    tag['style'] = style.replace(url, local_path)
        
        for style_tag in soup.find_all('style'):
            if style_tag.string:
                css_content = style_tag.string
                urls = re.findall(r'url\(["\']?([^"\']+)["\']?\)', css_content)
                for url in urls:
                    local_path = self.download_resource(url, base_url, assets_dir)
                    if local_path:
                        css_content = css_content.replace(url, local_path)
                style_tag.string = css_content
    
    def process_css_files(self, soup, base_url, assets_dir):
        """Download and process CSS files"""
        css_links = soup.find_all('link', rel='stylesheet')
        for link in css_links:
            href = link.get('href')
            if href:
                local_path = self.download_and_process_css(href, base_url, assets_dir)
                if local_path:
                    link['href'] = local_path
    
    def download_and_process_css(self, css_url, base_url, assets_dir):
        """Download CSS file and process its resources"""
        full_css_url = urljoin(base_url, css_url)
        local_path = self.download_resource(css_url, base_url, assets_dir)
        if local_path and local_path.endswith('.css'):
            full_local_path = os.path.join(assets_dir, os.path.basename(local_path))
            try:
                with open(full_local_path, 'r', encoding='utf-8') as f:
                    css_content = f.read()
                urls = re.findall(r'url\s*\(\s*["\']?([^"\')]+)["\']?\s*\)', css_content)
                for res_url in set(urls):
                    if res_url.startswith(('data:', '#')):
                        continue
                    res_local = self.download_resource(res_url, full_css_url, assets_dir)
                    if res_local:
                        rel_to_css = os.path.basename(res_local)  # Since flat assets dir
                        css_content = re.sub(r'(url\s*\(\s*["\']?)' + re.escape(res_url) + r'(["\']?\s*\))', r'\1' + rel_to_css + r'\2', css_content)
                with open(full_local_path, 'w', encoding='utf-8') as f:
                    f.write(css_content)
            except Exception as e:
                print(f"Error processing CSS {full_css_url}: {e}")
        return local_path
    
    def process_js_files(self, soup, base_url, assets_dir):
        """Download and process JavaScript files"""
        js_scripts = soup.find_all('script', src=True)
        for script in js_scripts:
            src = script.get('src')
            if src:
                local_path = self.download_resource(src, base_url, assets_dir)
                if local_path:
                    script['src'] = local_path
    
    def download_resource(self, url, base_url, assets_dir):
        """Download a resource and return local path"""
        try:
            if url.startswith('data:'):
                return self.save_data_uri(url, assets_dir)
            
            full_url = urljoin(base_url, url)
            
            if full_url in self.downloaded_resources:
                return self.get_local_path(full_url, assets_dir)
            
            response = self.session.get(full_url, timeout=15)
            response.raise_for_status()
            
            parsed_url = urlparse(full_url)
            filename = os.path.basename(parsed_url.path) or 'resource'
            
            if '.' not in filename:
                content_type = response.headers.get('content-type', '')
                if 'image' in content_type:
                    ext = mimetypes.guess_extension(content_type) or '.jpg'
                    filename += ext
                elif 'css' in content_type:
                    filename += '.css'
                elif 'javascript' in content_type:
                    filename += '.js'
            
            counter = 1
            original_filename = filename
            while os.path.exists(os.path.join(assets_dir, filename)):
                name, ext = os.path.splitext(original_filename)
                filename = f"{name}_{counter}{ext}"
                counter += 1
            
            file_path = os.path.join(assets_dir, filename)
            with open(file_path, 'wb') as f:
                f.write(response.content)
            
            self.downloaded_resources.add(full_url)
            return f"assets/{filename}"
            
        except Exception as e:
            print(f"Error downloading resource {url}: {e}")
            return None
    
    def save_data_uri(self, data_uri, assets_dir):
        """Save data URI as file"""
        try:
            header, data = data_uri.split(',', 1)
            mime_type = header.split(';')[0].split(':')[1]
            
            ext = mimetypes.guess_extension(mime_type) or '.bin'
            filename = f"data_uri_{len(self.downloaded_resources)}{ext}"
            
            file_data = base64.b64decode(data)
            file_path = os.path.join(assets_dir, filename)
            with open(file_path, 'wb') as f:
                f.write(file_data)
            
            return f"assets/{filename}"
            
        except Exception as e:
            print(f"Error saving data URI: {e}")
            return None
    
    def get_local_path(self, url, assets_dir):
        """Get local path for already downloaded resource"""
        filename = os.path.basename(urlparse(url).path) or 'resource'
        return f"assets/{filename}"

# Preview server management
def find_available_port(start_port=9000):
    """Find an available port for preview server"""
    for port in range(start_port, start_port + 100):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('localhost', port))
                return port
        except OSError:
            continue
    return None

def start_preview_server(folder_path, domain):
    """Start preview server for a cloned website"""
    global preview_servers, current_preview_port
    
    # Find available port
    port = find_available_port(current_preview_port)
    if port is None:
        print(f"Error: No available port found for preview server (domain: {domain})")
        return None
    
    current_preview_port = port + 1
    
    # Create custom handler
    class CustomHandler(http.server.SimpleHTTPRequestHandler):
        def end_headers(self):
            self.send_header('Cache-Control', 'no-cache')
            super().end_headers()
        
        def do_GET(self):
            if self.path == '/' or self.path == '':
                self.path = '/index.html'
            elif self.path.endswith('/'):
                self.path += 'index.html'
            
            if '.' not in os.path.basename(self.path):
                if os.path.exists(os.path.join(folder_path, self.path.lstrip('/') + '.html')):
                    self.path += '.html'
                elif os.path.exists(os.path.join(folder_path, self.path.lstrip('/') + '/index.html')):
                    self.path += '/index.html'
            
            try:
                print(f"Serving file: {self.path} for domain: {domain}")
                return super().do_GET()
            except Exception as e:
                print(f"Error serving file {self.path}: {e}")
                self.send_error(404, f"File not found: {self.path}")
        
        def log_message(self, format, *args):
            print(f"Preview server request for {domain}: {format % args}")
    
    def run_server():
        original_dir = os.getcwd()
        try:
            os.chdir(folder_path)
            print(f"Starting preview server for {domain} on port {port} at path {folder_path}")
            with socketserver.TCPServer(("localhost", port), CustomHandler) as httpd:
                preview_servers[domain]['httpd'] = httpd
                httpd.serve_forever()
        except Exception as e:
            print(f"Preview server error for {domain}: {e}")
        finally:
            os.chdir(original_dir)
            if domain in preview_servers:
                del preview_servers[domain]
    
    # Initialize preview_servers entry
    preview_servers[domain] = {'port': port, 'thread': None}
    
    # Start server thread
    server_thread = Thread(target=run_server, daemon=True)
    server_thread.start()
    preview_servers[domain]['thread'] = server_thread
    
    # Wait briefly to ensure server starts
    time.sleep(1)
    
    # Verify server is running
    try:
        response = requests.get(f"http://localhost:{port}/index.html", timeout=5)
        if response.status_code == 200:
            print(f"Preview server for {domain} started successfully on port {port}")
            return port
        else:
            print(f"Preview server for {domain} failed to serve index.html (status: {response.status_code})")
            return None
    except Exception as e:
        print(f"Error verifying preview server for {domain}: {e}")
        return None

# Flask routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/download/<path:filename>')
def download_file(filename):
    """Serve downloaded files"""
    try:
        downloads_dir = os.path.join(os.getcwd(), 'cloned_websites')
        return send_from_directory(downloads_dir, filename, as_attachment=True)
    except Exception as e:
        return jsonify({'error': str(e)}), 404

@app.route('/api/set_download_location', methods=['POST'])
def set_download_location():
    """Set download location"""
    try:
        data = request.get_json()
        location = data.get('location', '')
        
        if not location:
            return jsonify({'error': 'No location provided'}), 400
        
        # Create directory if it doesn't exist
        os.makedirs(location, exist_ok=True)
        
        return jsonify({'success': True, 'location': location})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/get_cloned_websites')
def get_cloned_websites():
    """Get list of cloned websites"""
    try:
        downloads_dir = os.path.join(os.getcwd(), 'cloned_websites')
        if not os.path.exists(downloads_dir):
            return jsonify({'websites': []})
        
        websites = []
        for item in os.listdir(downloads_dir):
            item_path = os.path.join(downloads_dir, item)
            if os.path.isdir(item_path):
                index_path = os.path.join(item_path, 'index.html')
                if os.path.exists(index_path):
                    websites.append({
                        'domain': item,
                        'path': item_path,
                        'has_preview': item in preview_servers
                    })
        
        return jsonify({'websites': websites})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/preview/<domain>')
def preview_website(domain):
    """Start preview for a cloned website"""
    try:
        downloads_dir = os.path.join(os.getcwd(), 'cloned_websites')
        website_path = os.path.join(downloads_dir, domain)
        
        if not os.path.exists(website_path):
            return jsonify({'error': 'Website not found'}), 404
        
        index_path = os.path.join(website_path, 'index.html')
        if not os.path.exists(index_path):
            return jsonify({'error': 'No index.html found'}), 404
        
        # Start preview server
        if domain in preview_servers:
            port = preview_servers[domain]['port']
        else:
            port = start_preview_server(website_path, domain)
            if port is None:
                return jsonify({'error': 'Could not start preview server'}), 500
        
        preview_url = f"http://localhost:{port}"
        return jsonify({
            'success': True,
            'preview_url': preview_url,
            'domain': domain,
            'port': port
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# SocketIO events
@socketio.on('clone_website')
def handle_clone_request(data):
    """Handle website cloning request"""
    def clone_task():
        url = data.get('url')
        download_location = data.get('downloadLocation', os.path.join(os.getcwd(), 'cloned_websites'))
        
        if not url:
            emit('clone_error', {'error': 'No URL provided'})
            return
        
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        # Create download directory
        try:
            os.makedirs(download_location, exist_ok=True)
        except Exception as e:
            emit('clone_error', {'error': f'Cannot create download directory: {str(e)}'})
            return
        
        cloner = WebClonerCore(socketio)
        result = cloner.clone_website(url, download_location)
        
        if result['success']:
            zip_filename = os.path.basename(result['zip_path'])
            emit('clone_complete', {
                'domain': result['domain'],
                'download_url': f'/download/{zip_filename}',
                'folder_path': result['output_dir']
            })
        else:
            emit('clone_error', {'error': result['error']})
    
    # Run in separate thread
    thread = Thread(target=clone_task)
    thread.daemon = True
    thread.start()

# Create templates directory and files
def create_templates():
    """Create HTML templates"""
    templates_dir = 'templates'
    os.makedirs(templates_dir, exist_ok=True)
    
    html_content = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Web Cloner Pro - Professional Website Downloader</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.5/socket.io.min.js"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.6.0/css/all.min.css" rel="stylesheet">
    <style>
        :root {
            --primary: #1e3a8a;
            --primary-dark: #1e293b;
            --secondary: #facc15;
            --secondary-dark: #ca8a04;
            --background: #f8fafc;
            --card-bg: rgba(255, 255, 255, 0.95);
            --text-primary: #1e293b;
            --text-secondary: #475569;
            --success: #22c55e;
            --danger: #ef4444;
            --warning: #f59e0b;
            --shadow-sm: 0 2px 4px rgba(0, 0, 0, 0.1);
            --shadow-md: 0 4px 8px rgba(0, 0, 0, 0.15);
            --shadow-lg: 0 8px 16px rgba(0, 0, 0, 0.2);
            --ring: 0 0 0 3px rgba(30, 58, 138, 0.2);
            --gray-200: #e5e7eb;
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: var(--background);
            min-height: 100vh;
            padding: 40px;
            font-size: 16px;
            line-height: 1.6;
            color: var(--text-primary);
            position: relative;
            overflow-x: hidden;
        }

        body::before {
            content: '';
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: linear-gradient(135deg, rgba(30, 58, 138, 0.05) 0%, rgba(250, 204, 21, 0.05) 100%);
            z-index: -1;
        }

        .container {
            max-width: 1000px;
            margin: 0 auto;
            background: var(--card-bg);
            border-radius: 16px;
            padding: 40px;
            box-shadow: var(--shadow-lg);
            position: relative;
            z-index: 1;
            transition: transform 0.3s ease, box-shadow 0.3s ease;
        }

        .container:hover {
            transform: translateY(-4px);
            box-shadow: var(--shadow-lg);
        }

        .header {
            text-align: center;
            margin-bottom: 40px;
        }

        .title {
            font-size: 2.5rem;
            font-weight: 700;
            color: var(--primary);
            letter-spacing: -0.025em;
            margin-bottom: 12px;
        }

        .subtitle {
            font-size: 1.1rem;
            color: var(--text-secondary);
            font-weight: 400;
        }

        .logo {
            font-size: 3rem;
            color: var(--primary);
            margin-bottom: 16px;
        }

        .form-section {
            margin-bottom: 32px;
        }

        .input-group {
            margin-bottom: 24px;
        }

        .label {
            display: flex;
            align-items: center;
            gap: 8px;
            font-size: 0.95rem;
            font-weight: 500;
            color: var(--text-primary);
            margin-bottom: 8px;
        }

        .label i {
            color: var(--primary);
        }

        .input {
            width: 100%;
            padding: 12px 16px;
            border: 1px solid rgba(0, 0, 0, 0.1);
            border-radius: 8px;
            font-size: 0.95rem;
            transition: border-color 0.3s ease, box-shadow 0.3s ease;
            background: white;
        }

        .input:focus {
            outline: none;
            border-color: var(--primary);
            box-shadow: var(--ring);
        }

        .input:hover {
            border-color: var(--primary-dark);
        }

        .button {
            padding: 12px 24px;
            background: var(--primary);
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 0.95rem;
            font-weight: 500;
            cursor: pointer;
            transition: background 0.3s ease, transform 0.3s ease, box-shadow 0.3s ease;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }

        .button:hover {
            background: var(--primary-dark);
            transform: translateY(-2px);
            box-shadow: var(--shadow-md);
        }

        .button:disabled {
            background: #94a3b8;
            cursor: not-allowed;
            transform: none;
            box-shadow: none;
        }

        .button.secondary {
            background: var(--secondary);
            color: var(--primary-dark);
        }

        .button.secondary:hover {
            background: var(--secondary-dark);
        }

        .button.success {
            background: var(--success);
        }

        .button.ghost {
            background: transparent;
            border: 1px solid var(--primary);
            color: var(--primary);
        }

        .button.ghost:hover {
            background: var(--primary);
            color: white;
        }

        .button-group {
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            margin-bottom: 24px;
        }

        .location-selector {
            display: flex;
            gap: 12px;
            align-items: stretch;
        }

        .location-selector .input {
            flex: 1;
        }

        .location-selector .button {
            margin: 0;
        }

        .progress-container {
            margin-bottom: 24px;
            display: none;
        }

        .progress-wrapper {
            background: rgba(0, 0, 0, 0.05);
            border-radius: 8px;
            padding: 4px;
        }

        .progress-bar {
            height: 10px;
            background: rgba(0, 0, 0, 0.1);
            border-radius: 6px;
            overflow: hidden;
        }

        .progress-fill {
            height: 100%;
            background: var(--primary);
            transition: width 0.3s ease;
            width: 0%;
        }

        .progress-text {
            text-align: center;
            margin-top: 8px;
            font-size: 0.9rem;
            color: var(--text-secondary);
        }

        .status {
            margin-top: 16px;
            padding: 16px;
            border-radius: 8px;
            background: rgba(0, 0, 0, 0.05);
            color: var(--text-primary);
            text-align: center;
            display: none;
            font-size: 0.95rem;
        }

        .status.success {
            background: rgba(34, 197, 94, 0.1);
            color: var(--success);
        }

        .status.error {
            background: rgba(239, 68, 68, 0.1);
            color: var(--danger);
        }

        .status.info {
            background: rgba(59, 130, 246, 0.1);
            color: #3b82f6;
        }

        .download-actions {
            margin-top: 24px;
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            justify-content: center;
        }

        .download-link {
            padding: 12px 24px;
            background: var(--success);
            color: white;
            text-decoration: none;
            border-radius: 8px;
            font-weight: 500;
            transition: all 0.3s ease;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }

        .download-link:hover {
            background: #16a34a;
            transform: translateY(-2px);
            box-shadow: var(--shadow-md);
        }

        .preview-section {
            margin-top: 32px;
            padding: 24px;
            background: rgba(255, 255, 255, 0.8);
            border-radius: 12px;
            border: 1px solid rgba(0, 0, 0, 0.1);
            display: none;
        }

        .section-title {
            font-size: 1.5rem;
            font-weight: 600;
            color: var(--primary);
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .section-title i {
            color: var(--primary);
        }

        .websites-list {
            margin-top: 16px;
        }

        .website-item {
            background: white;
            border: 1px solid rgba(0, 0, 0, 0.1);
            padding: 20px;
            margin: 12px 0;
            border-radius: 8px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            transition: all 0.3s ease;
        }

        .website-item:hover {
            transform: translateY(-2px);
            box-shadow: var(--shadow-md);
        }

        .website-info {
            flex: 1;
        }

        .website-domain {
            font-weight: 600;
            font-size: 1.1rem;
            color: var(--primary);
            margin-bottom: 4px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .website-status {
            font-size: 0.9rem;
            color: var(--text-secondary);
            display: flex;
            align-items: center;
            gap: 6px;
        }

        .status-indicator {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            display: inline-block;
        }

        .status-indicator.online {
            background: var(--success);
        }

        .status-indicator.offline {
            background: #94a3b8;
        }

        .website-actions {
            display: flex;
            gap: 8px;
        }

        .small-button {
            padding: 8px 16px;
            font-size: 0.9rem;
        }

        .empty-state {
            text-align: center;
            padding: 40px 20px;
            color: var(--text-secondary);
        }

        .empty-state i {
            font-size: 3rem;
            color: #94a3b8;
            margin-bottom: 12px;
            display: block;
        }

        .empty-state h3 {
            font-size: 1.1rem;
            margin-bottom: 8px;
        }

        .loading-spinner {
            display: inline-block;
            width: 16px;
            height: 16px;
            border: 2px solid rgba(255, 255, 255, 0.3);
            border-radius: 50%;
            border-top-color: #fff;
            animation: spin 1s ease-in-out infinite;
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        .feature-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 16px;
            margin: 32px 0;
        }

        .feature-card {
            background: white;
            border: 1px solid rgba(0, 0, 0, 0.1);
            border-radius: 8px;
            padding: 20px;
            text-align: center;
            transition: all 0.3s ease;
        }

        .feature-card:hover {
            transform: translateY(-4px);
            box-shadow: var(--shadow-md);
        }

        .feature-icon {
            font-size: 2rem;
            color: var(--primary);
            margin-bottom: 12px;
        }

        .feature-title {
            font-weight: 600;
            color: var(--primary);
            margin-bottom: 8px;
        }

        .feature-desc {
            font-size: 0.9rem;
            color: var(--text-secondary);
        }

        @media (max-width: 768px) {
            body {
                padding: 20px;
            }

            .container {
                padding: 24px;
            }

            .title {
                font-size: 2rem;
            }

            .location-selector {
                flex-direction: column;
            }

            .button-group {
                justify-content: center;
            }

            .website-item {
                flex-direction: column;
                gap: 16px;
                text-align: center;
            }

            .website-actions {
                justify-content: center;
            }

            .download-actions {
                flex-direction: column;
            }

            .feature-grid {
                grid-template-columns: 1fr;
            }
        }

        @media (max-width: 480px) {
            .container {
                padding: 16px;
            }

            .title {
                font-size: 1.75rem;
            }

            .button {
                width: 100%;
                justify-content: center;
            }

            .location-selector .button {
                width: auto;
            }
        }

        ::-webkit-scrollbar {
            width: 8px;
        }

        ::-webkit-scrollbar-track {
            background: rgba(0, 0, 0, 0.05);
        }

        ::-webkit-scrollbar-thumb {
            background: var(--primary);
            border-radius: 4px;
        }

        ::-webkit-scrollbar-thumb:hover {
            background: var(--primary-dark);
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="logo">
                <i class="fas fa-globe"></i>
            </div>
            <h1 class="title">Web Cloner Pro</h1>
            <p class="subtitle">Effortlessly clone websites with all assets</p>
        </div>

        <div class="feature-grid">
            <div class="feature-card">
                <div class="feature-icon">
                    <i class="fas fa-download"></i>
                </div>
                <div class="feature-title">Full Asset Downloads</div>
                <div class="feature-desc">Capture HTML, CSS, JS, and images</div>
            </div>
            <div class="feature-card">
                <div class="feature-icon">
                    <i class="fas fa-eye"></i>
                </div>
                <div class="feature-title">Live Preview</div>
                <div class="feature-desc">View cloned sites instantly</div>
            </div>
            <div class="feature-card">
                <div class="feature-icon">
                    <i class="fas fa-archive"></i>
                </div>
                <div class="feature-title">ZIP Archives</div>
                <div class="feature-desc">Download sites as ZIP files</div>
            </div>
        </div>

        <form id="cloneForm" class="form-section">
            <div class="input-group">
                <label class="label" for="downloadLocation">
                    <i class="fas fa-folder"></i>
                    Download Location
                </label>
                <div class="location-selector">
                    <input 
                        type="text" 
                        id="downloadLocation" 
                        class="input" 
                        placeholder="Select a folder to save websites..."
                        readonly
                    >
                    <button type="button" class="button secondary" onclick="chooseDownloadLocation()">
                        <i class="fas fa-folder-open"></i>
                        Browse
                    </button>
                </div>
            </div>

            <div class="input-group">
                <label class="label" for="url">
                    <i class="fas fa-link"></i>
                    Website URL
                </label>
                <input 
                    type="url" 
                    id="url" 
                    class="input" 
                    placeholder="https://example.com"
                    required
                >
            </div>

            <div class="button-group">
                <button type="submit" class="button" id="cloneBtn">
                    <i class="fas fa-rocket"></i>
                    Clone Website
                </button>
                <button type="button" class="button ghost" onclick="showPreviewSection()">
                    <i class="fas fa-list"></i>
                    View Cloned Websites
                </button>
            </div>
        </form>

        <div class="progress-container" id="progressContainer">
            <div class="progress-wrapper">
                <div class="progress-bar">
                    <div class="progress-fill" id="progressFill"></div>
                </div>
            </div>
            <div class="progress-text" id="progressText">Initializing...</div>
        </div>

        <div class="status" id="status"></div>

        <div class="preview-section" id="previewSection">
            <h3 class="section-title">
                <i class="fas fa-server"></i>
                Cloned Websites
            </h3>
            <div class="websites-list" id="websitesList">
                <div class="empty-state">
                    <i class="fas fa-spinner fa-spin"></i>
                    <h3>Loading...</h3>
                    <p>Fetching your cloned websites</p>
                </div>
            </div>
        </div>
    </div>

    <script>
        const socket = io();
        const form = document.getElementById('cloneForm');
        const urlInput = document.getElementById('url');
        const locationInput = document.getElementById('downloadLocation');
        const cloneBtn = document.getElementById('cloneBtn');
        const progressContainer = document.getElementById('progressContainer');
        const progressFill = document.getElementById('progressFill');
        const progressText = document.getElementById('progressText');
        const status = document.getElementById('status');
        const previewSection = document.getElementById('previewSection');
        const websitesList = document.getElementById('websitesList');
        
        let currentDownloadLocation = '';
        
        // Initialize default download location
        window.addEventListener('load', () => {
            const defaultLocation = './cloned_websites';
            locationInput.value = defaultLocation;
            currentDownloadLocation = defaultLocation;
            console.log('Default download location set:', defaultLocation);
        });
        
        function chooseDownloadLocation() {
            const location = prompt('Enter the full path where you want to save cloned websites:', currentDownloadLocation || './cloned_websites');
            if (location) {
                fetch('/api/set_download_location', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({location: location})
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        locationInput.value = location;
                        currentDownloadLocation = location;
                        showStatus('Download location set successfully!', 'success');
                        console.log('Download location updated:', location);
                    } else {
                        showStatus('Error setting download location: ' + data.error, 'error');
                        console.error('Error setting download location:', data.error);
                    }
                })
                .catch(error => {
                    showStatus('Error: ' + error.message, 'error');
                    console.error('Error in chooseDownloadLocation:', error);
                });
            }
        }
        
        function showPreviewSection() {
            const isVisible = previewSection.style.display === 'block';
            previewSection.style.display = isVisible ? 'none' : 'block';
            if (!isVisible) {
                loadClonedWebsites();
            }
        }
        
        function loadClonedWebsites() {
            websitesList.innerHTML = `
                <div class="empty-state">
                    <i class="fas fa-spinner fa-spin"></i>
                    <h3>Loading...</h3>
                    <p>Fetching your cloned websites</p>
                </div>
            `;
            
            fetch('/api/get_cloned_websites')
                .then(response => response.json())
                .then(data => {
                    console.log('Cloned websites data:', data);
                    if (data.websites && data.websites.length > 0) {
                        displayWebsites(data.websites);
                    } else {
                        websitesList.innerHTML = `
                            <div class="empty-state">
                                <i class="fas fa-globe"></i>
                                <h3>No websites cloned yet</h3>
                                <p>Clone your first website to get started!</p>
                            </div>
                        `;
                    }
                })
                .catch(error => {
                    websitesList.innerHTML = `
                        <div class="empty-state">
                            <i class="fas fa-exclamation-triangle"></i>
                            <h3>Error loading websites</h3>
                            <p>${error.message}</p>
                        </div>
                    `;
                    console.error('Error loading cloned websites:', error);
                });
        }
        
        function displayWebsites(websites) {
            websitesList.innerHTML = '';
            
            websites.forEach(website => {
                const websiteDiv = document.createElement('div');
                websiteDiv.className = 'website-item';
                
                websiteDiv.innerHTML = `
                    <div class="website-info">
                        <div class="website-domain">
                            <i class="fas fa-globe"></i>
                            ${website.domain}
                        </div>
                        <div class="website-status">
                            <span class="status-indicator ${website.has_preview ? 'online' : 'offline'}"></span>
                            ${website.has_preview ? 'Preview Running' : 'Ready to Preview'}
                        </div>
                    </div>
                    <div class="website-actions">
                        <button class="button success small-button" onclick="previewWebsite('${website.domain}')">
                            <i class="fas fa-eye"></i>
                            Preview
                        </button>
                    </div>
                `;
                
                websitesList.appendChild(websiteDiv);
            });
        }
        
        function previewWebsite(domain) {
            showStatus(`Starting preview server for ${domain}...`, 'info');
            console.log(`Attempting to preview website: ${domain}`);
            
            fetch(`/api/preview/${domain}`)
                .then(response => {
                    if (!response.ok) {
                        throw new Error(`HTTP error! status: ${response.status}`);
                    }
                    return response.json();
                })
                .then(data => {
                    console.log('Preview response:', data);
                    if (data.success) {
                        const previewUrl = data.preview_url;
                        let retryCount = 0;
                        const maxRetries = 3;
                        
                        function tryOpenPreview() {
                            console.log(`Opening preview URL: ${previewUrl} (Attempt ${retryCount + 1})`);
                            const newWindow = window.open(previewUrl, '_blank');
                            if (!newWindow) {
                                console.warn('Popup blocked or failed to open');
                                if (retryCount < maxRetries) {
                                    retryCount++;
                                    setTimeout(tryOpenPreview, 1000);
                                } else {
                                    showStatus('Error: Could not open preview. Please allow popups or open the URL manually: ' + previewUrl, 'error');
                                }
                            } else {
                                showStatus(`Preview started for ${domain} at ${previewUrl}`, 'success');
                                setTimeout(() => {
                                    loadClonedWebsites();
                                }, 1000);
                            }
                        }
                        
                        tryOpenPreview();
                    } else {
                        showStatus('Error starting preview: ' + data.error, 'error');
                        console.error('Preview error:', data.error);
                    }
                })
                .catch(error => {
                    showStatus('Error: ' + error.message, 'error');
                    console.error('Error in previewWebsite:', error);
                });
        }
        
        function showStatus(message, type = 'info') {
            status.innerHTML = message;
            status.className = `status ${type}`;
            status.style.display = 'block';
            
            if (type !== 'error') {
                setTimeout(() => {
                    status.style.display = 'none';
                }, 5000);
            }
        }
        
        function resetForm() {
            try {
                console.log('Resetting form');
                cloneBtn.disabled = false;
                cloneBtn.innerHTML = '<i class="fas fa-rocket"></i> Clone Website';
                progressFill.style.width = '0%';
                progressContainer.style.display = 'none';
                progressText.textContent = 'Initializing...';
            } catch (error) {
                console.error('Error in resetForm:', error);
                showStatus('Error resetting form: ' + error.message, 'error');
            }
        }
        
        form.addEventListener('submit', (e) => {
            e.preventDefault();
            
            const url = urlInput.value.trim();
            const downloadLocation = currentDownloadLocation;
            
            if (!url) {
                showStatus('Please enter a website URL', 'error');
                console.error('No URL provided');
                return;
            }
            
            if (!downloadLocation) {
                showStatus('Please choose a download location', 'error');
                console.error('No download location provided');
                return;
            }
            
            cloneBtn.disabled = true;
            cloneBtn.innerHTML = '<span class="loading-spinner"></span> Cloning...';
            progressContainer.style.display = 'block';
            showStatus('Initializing cloning process...', 'info');
            progressFill.style.width = '0%';
            progressText.textContent = 'Initializing...';
            
            console.log('Starting clone process for URL:', url, 'Location:', downloadLocation);
            
            socket.emit('clone_website', { 
                url: url, 
                downloadLocation: downloadLocation 
            });
        });
        
        socket.on('status_update', (data) => {
            showStatus(data.message, 'info');
            console.log('Status update:', data);
        });
        
        socket.on('clone_complete', (data) => {
            console.log('Clone complete:', data);
            showStatus(`Website cloned successfully!`, 'success');
            progressText.textContent = 'Completed successfully!';
            
            const actionsHtml = `
                <div class="download-actions">
                    <a href="${data.download_url}" class="download-link">
                        <i class="fas fa-download"></i>
                        Download ZIP Archive
                    </a>
                    <button class="button success" onclick="previewWebsite('${data.domain}')">
                        <i class="fas fa-eye"></i>
                        Preview Website
                    </button>
                    <button class="button ghost" onclick="showPreviewSection()">
                        <i class="fas fa-list"></i>
                        Show All Websites
                    </button>
                </div>
            `;
            
            status.innerHTML += actionsHtml;
            resetForm();
            
            if (previewSection.style.display === 'block') {
                setTimeout(() => {
                    loadClonedWebsites();
                }, 1000);
            }
            
            setTimeout(() => {
                if (cloneBtn.disabled) {
                    console.warn('Forcing form reset due to timeout');
                    resetForm();
                }
            }, 2000);
        });
        
        socket.on('clone_error', (data) => {
            console.error('Clone error:', data);
            showStatus(`Error: ${data.error}`, 'error');
            progressText.textContent = 'Error occurred';
            resetForm();
        });
        
        window.addEventListener('load', () => {
            if (previewSection.style.display === 'block') {
                loadClonedWebsites();
            }
        });

        urlInput.addEventListener('input', function() {
            const url = this.value.trim();
            if (url && !url.match(/^https?:\/\//)) {
                this.style.borderColor = 'var(--warning)';
            } else {
                this.style.borderColor = 'var(--gray-200)';
            }
        });

        document.addEventListener('keydown', function(e) {
            if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
                if (!cloneBtn.disabled) {
                    form.dispatchEvent(new Event('submit'));
                }
            }
        });
    </script>
</body>
</html>'''
    
    with open(os.path.join(templates_dir, 'index.html'), 'w', encoding='utf-8') as f:
        f.write(html_content)

if __name__ == '__main__':
    # Create templates
    create_templates()
    
    # Create default output directory
    default_output = os.path.join(os.getcwd(), 'cloned_websites')
    os.makedirs(default_output, exist_ok=True)
    
    print("=" * 60)
    print("🌐 Web Cloner Pro - Enhanced Version")
    print("=" * 60)
    print("🚀 Features:")
    print("   • Modern, attractive UI with professional design")
    print("   • Choose custom download location")
    print("   • Preview cloned websites locally")
    print("   • Download ZIP archives")
    print("   • Manage multiple cloned sites")
    print("   • Real-time progress tracking")
    print("   • Responsive design for all devices")
    print()
    print("📱 Web Interface: http://localhost:5000")
    print("📁 Default Download Location:", default_output)
    print()
    print("🔧 Keep this terminal open to run the server")
    print("=" * 60)
    
    # Auto-open browser
    def open_browser():
        time.sleep(1.5)
        try:
            webbrowser.open('http://localhost:5000')
        except:
            pass
    
    Thread(target=open_browser, daemon=True).start()
    
    # Run Flask app with gevent
    try:
        from gevent.pywsgi import WSGIServer
        from geventwebsocket.handler import WebSocketHandler
        server = WSGIServer(('localhost', 5000), app, handler_class=WebSocketHandler)
        print("Starting server with gevent...")
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 Server stopped by user")
    except Exception as e:
        print(f"❌ Server error: {e}")
    finally:
        # Clean up preview servers
        for domain, server_info in list(preview_servers.items()):
            try:
                if 'httpd' in server_info:
                    server_info['httpd'].shutdown()
            except:
                pass
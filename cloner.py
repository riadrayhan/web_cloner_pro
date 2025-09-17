from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit
import os
import sys
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
import shutil
import json

app = Flask(__name__)
app.config['SECRET_KEY'] = 'web_cloner_secret_key'
socketio = SocketIO(app, cors_allowed_origins="*")

# Global variables for server management
preview_servers = {}  # Store multiple preview servers
current_preview_port = 9000

class WebClonerCore:
    """Core web cloning functionality - your existing logic with enhancements"""
    
    def __init__(self, socketio_instance=None):
        self.socketio = socketio_instance
        self.downloaded_resources = set()
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })
    
    def emit_status(self, message, progress=None):
        """Emit status update via SocketIO"""
        if self.socketio:
            data = {'message': message}
            if progress is not None:
                data['progress'] = progress
            self.socketio.emit('status_update', data)
        print(f"Status: {message} ({progress}%)" if progress else f"Status: {message}")
    
    def clone_website(self, url, output_base_dir):
        """Main cloning function - your existing logic"""
        try:
            self.emit_status("Starting website cloning...", 0)
            
            # Parse URL and create output directory
            parsed_url = urlparse(url)
            domain = parsed_url.netloc.replace(':', '_')
            output_dir = os.path.join(output_base_dir, domain)
            
            # Create output directory
            os.makedirs(output_dir, exist_ok=True)
            
            # Create assets directory
            assets_dir = os.path.join(output_dir, 'assets')
            os.makedirs(assets_dir, exist_ok=True)
            
            self.emit_status(f"Downloading main page from {url}...", 10)
            
            # Download main page
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            
            self.emit_status("Parsing HTML content...", 20)
            
            # Parse HTML
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Process and download resources
            self.emit_status("Processing images and resources...", 30)
            
            # Download images
            self.process_images(soup, url, assets_dir)
            self.emit_status("Images processed", 50)
            
            # Download CSS files
            self.emit_status("Processing CSS files...", 60)
            self.process_css_files(soup, url, assets_dir)
            
            # Download JavaScript files
            self.emit_status("Processing JavaScript files...", 70)
            self.process_js_files(soup, url, assets_dir)
            
            # Process internal links
            self.emit_status("Processing internal links...", 80)
            self.process_internal_links(soup, url, output_dir)
            
            # Save main HTML file
            self.emit_status("Saving HTML file...", 90)
            html_file = os.path.join(output_dir, 'index.html')
            with open(html_file, 'w', encoding='utf-8') as f:
                f.write(str(soup))
            
            # Create ZIP file
            self.emit_status("Creating downloadable archive...", 95)
            zip_path = self.create_zip_archive(output_dir)
            
            self.emit_status(f"Website cloned successfully!", 100)
            
            return {
                'success': True,
                'output_dir': output_dir,
                'zip_path': zip_path,
                'domain': domain
            }
            
        except Exception as e:
            self.emit_status(f"Error: {str(e)}", 0)
            return {
                'success': False,
                'error': str(e)
            }
    
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
    
    # Your existing methods (process_images, process_css_files, etc.)
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
                local_path = self.download_resource(href, base_url, assets_dir)
                if local_path:
                    link['href'] = local_path
    
    def process_js_files(self, soup, base_url, assets_dir):
        """Download and process JavaScript files"""
        js_scripts = soup.find_all('script', src=True)
        for script in js_scripts:
            src = script.get('src')
            if src:
                local_path = self.download_resource(src, base_url, assets_dir)
                if local_path:
                    script['src'] = local_path
    
    def process_internal_links(self, soup, base_url, output_dir):
        """Process internal page links"""
        base_domain = urlparse(base_url).netloc
        
        for link in soup.find_all('a', href=True):
            href = link['href']
            full_url = urljoin(base_url, href)
            link_domain = urlparse(full_url).netloc
            
            if link_domain == base_domain:
                try:
                    response = self.session.get(full_url, timeout=15)
                    if response.status_code == 200:
                        path = urlparse(full_url).path
                        if path.endswith('/') or not path:
                            filename = 'index.html'
                            local_dir = os.path.join(output_dir, path.strip('/'))
                        else:
                            filename = os.path.basename(path)
                            if not filename.endswith('.html'):
                                filename += '.html'
                            local_dir = os.path.join(output_dir, os.path.dirname(path).strip('/'))
                        
                        if local_dir != output_dir:
                            os.makedirs(local_dir, exist_ok=True)
                        
                        file_path = os.path.join(local_dir, filename)
                        with open(file_path, 'w', encoding='utf-8') as f:
                            f.write(response.text)
                        
                        rel_path = os.path.relpath(file_path, output_dir).replace('\\', '/')
                        link['href'] = rel_path
                        
                except Exception as e:
                    print(f"Error downloading internal page {full_url}: {e}")
    
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
                if os.path.exists(self.path.lstrip('/') + '.html'):
                    self.path += '.html'
                elif os.path.exists(self.path.lstrip('/') + '/index.html'):
                    self.path += '/index.html'
            
            try:
                return super().do_GET()
            except Exception:
                self.send_error(404, "File not found")
        
        def log_message(self, format, *args):
            pass
    
    def run_server():
        original_dir = os.getcwd()
        try:
            os.chdir(folder_path)
            with socketserver.TCPServer(("localhost", port), CustomHandler) as httpd:
                preview_servers[domain] = {'httpd': httpd, 'port': port, 'thread': None}
                httpd.serve_forever()
        except Exception as e:
            print(f"Preview server error for {domain}: {e}")
        finally:
            os.chdir(original_dir)
            if domain in preview_servers:
                del preview_servers[domain]
    
    # Start server thread
    server_thread = Thread(target=run_server, daemon=True)
    server_thread.start()
    preview_servers[domain] = {'port': port, 'thread': server_thread}
    
    return port

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
    
    # Enhanced main template with preview and download location features
    html_content = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Web Cloner - Professional Website Downloader</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.5.0/socket.io.js"></script>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        
        .container {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            padding: 40px;
            border-radius: 20px;
            box-shadow: 0 20px 40px rgba(0, 0, 0, 0.1);
            max-width: 800px;
            margin: 0 auto;
        }
        
        .title {
            text-align: center;
            color: #333;
            margin-bottom: 30px;
            font-size: 2.5rem;
            font-weight: bold;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        .input-group {
            margin-bottom: 25px;
        }
        
        .label {
            display: block;
            margin-bottom: 8px;
            color: #555;
            font-weight: 600;
            font-size: 1.1rem;
        }
        
        .input {
            width: 100%;
            padding: 15px;
            border: 2px solid #e0e0e0;
            border-radius: 10px;
            font-size: 1rem;
            transition: all 0.3s ease;
            background: rgba(255, 255, 255, 0.8);
        }
        
        .input:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        
        .button {
            padding: 15px 30px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 10px;
            font-size: 1.2rem;
            font-weight: bold;
            cursor: pointer;
            transition: all 0.3s ease;
            margin-right: 10px;
            margin-bottom: 10px;
        }
        
        .button:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 20px rgba(102, 126, 234, 0.3);
        }
        
        .button:disabled {
            background: #ccc;
            cursor: not-allowed;
            transform: none;
            box-shadow: none;
        }
        
        .button.secondary {
            background: linear-gradient(135deg, #f39c12 0%, #e67e22 100%);
        }
        
        .button.success {
            background: linear-gradient(135deg, #27ae60 0%, #2ecc71 100%);
        }
        
        .button.danger {
            background: linear-gradient(135deg, #e74c3c 0%, #c0392b 100%);
        }
        
        .button-group {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-bottom: 20px;
        }
        
        .location-selector {
            display: flex;
            gap: 10px;
            align-items: center;
        }
        
        .progress-container {
            margin-bottom: 20px;
            display: none;
        }
        
        .progress-bar {
            width: 100%;
            height: 12px;
            background: #e0e0e0;
            border-radius: 6px;
            overflow: hidden;
        }
        
        .progress-fill {
            height: 100%;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            transition: width 0.3s ease;
            width: 0%;
        }
        
        .status {
            margin-top: 10px;
            padding: 15px;
            border-radius: 10px;
            background: rgba(102, 126, 234, 0.1);
            color: #333;
            text-align: center;
            display: none;
        }
        
        .success {
            background: rgba(39, 174, 96, 0.1);
            color: #27ae60;
            border: 1px solid rgba(39, 174, 96, 0.3);
        }
        
        .error {
            background: rgba(231, 76, 60, 0.1);
            color: #e74c3c;
            border: 1px solid rgba(231, 76, 60, 0.3);
        }
        
        .download-actions {
            margin-top: 20px;
            text-align: center;
        }
        
        .download-link {
            display: inline-block;
            margin: 5px;
            padding: 12px 25px;
            background: #27ae60;
            color: white;
            text-decoration: none;
            border-radius: 8px;
            font-weight: bold;
            transition: all 0.3s ease;
        }
        
        .download-link:hover {
            background: #2ecc71;
            transform: translateY(-1px);
        }
        
        .preview-section {
            margin-top: 30px;
            padding: 20px;
            background: rgba(52, 73, 94, 0.1);
            border-radius: 10px;
            display: none;
        }
        
        .websites-list {
            margin-top: 20px;
        }
        
        .website-item {
            background: rgba(255, 255, 255, 0.8);
            padding: 15px;
            margin: 10px 0;
            border-radius: 8px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .website-info {
            flex: 1;
        }
        
        .website-domain {
            font-weight: bold;
            font-size: 1.1rem;
            color: #333;
        }
        
        .website-actions {
            display: flex;
            gap: 10px;
        }
        
        .small-button {
            padding: 8px 15px;
            font-size: 0.9rem;
        }
        
        .emoji {
            font-size: 1.2rem;
            margin-right: 8px;
        }
        
        .info-box {
            background: rgba(52, 152, 219, 0.1);
            border: 1px solid rgba(52, 152, 219, 0.3);
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
            color: #2c3e50;
        }
        
        @media (max-width: 600px) {
            .container {
                padding: 20px;
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
                gap: 10px;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1 class="title">🌐 Web Cloner</h1>
        <p style="text-align: center; color: #666; margin-bottom: 30px;">
            Professional Website Downloader - Clone any website with all assets
        </p>
        
        <div class="info-box">
            <strong>💡 How to use:</strong><br>
            1. Choose where to save your cloned websites<br>
            2. Enter the website URL you want to clone<br>
            3. Click "Clone Website" and wait for completion<br>
            4. Use "Preview" to view the cloned website locally
        </div>
        
        <form id="cloneForm">
            <div class="input-group">
                <label class="label" for="downloadLocation">📁 Download Location:</label>
                <div class="location-selector">
                    <input 
                        type="text" 
                        id="downloadLocation" 
                        class="input" 
                        placeholder="Choose folder to save cloned websites..."
                        readonly
                    >
                    <button type="button" class="button secondary" onclick="chooseDownloadLocation()">
                        <span class="emoji">📂</span>Browse
                    </button>
                </div>
            </div>
            
            <div class="input-group">
                <label class="label" for="url">🌐 Website URL:</label>
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
                    <span class="emoji">🚀</span>Clone Website
                </button>
                <button type="button" class="button secondary" onclick="showPreviewSection()">
                    <span class="emoji">👁️</span>Show Cloned Websites
                </button>
            </div>
        </form>
        
        <div class="progress-container" id="progressContainer">
            <div class="progress-bar">
                <div class="progress-fill" id="progressFill"></div>
            </div>
        </div>
        
        <div class="status" id="status"></div>
        
        <div class="preview-section" id="previewSection">
            <h3 style="color: #333; margin-bottom: 15px;">📚 Your Cloned Websites</h3>
            <div class="websites-list" id="websitesList">
                <p style="text-align: center; color: #666;">Loading...</p>
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
        const status = document.getElementById('status');
        const previewSection = document.getElementById('previewSection');
        const websitesList = document.getElementById('websitesList');
        
        let currentDownloadLocation = '';
        
        // Initialize default download location
        window.addEventListener('load', () => {
            const defaultLocation = './cloned_websites';
            locationInput.value = defaultLocation;
            currentDownloadLocation = defaultLocation;
        });
        
        function chooseDownloadLocation() {
            // For web interface, we'll use a simple prompt
            // In a real implementation, you might use a file picker API
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
                    } else {
                        showStatus('Error setting download location: ' + data.error, 'error');
                    }
                })
                .catch(error => {
                    showStatus('Error: ' + error.message, 'error');
                });
            }
        }
        
        function showPreviewSection() {
            previewSection.style.display = previewSection.style.display === 'none' ? 'block' : 'none';
            if (previewSection.style.display === 'block') {
                loadClonedWebsites();
            }
        }
        
        function loadClonedWebsites() {
            websitesList.innerHTML = '<p style="text-align: center; color: #666;">Loading...</p>';
            
            fetch('/api/get_cloned_websites')
                .then(response => response.json())
                .then(data => {
                    if (data.websites && data.websites.length > 0) {
                        displayWebsites(data.websites);
                    } else {
                        websitesList.innerHTML = '<p style="text-align: center; color: #666;">No cloned websites found. Clone a website first!</p>';
                    }
                })
                .catch(error => {
                    websitesList.innerHTML = '<p style="text-align: center; color: #e74c3c;">Error loading websites: ' + error.message + '</p>';
                });
        }
        
        function displayWebsites(websites) {
            websitesList.innerHTML = '';
            
            websites.forEach(website => {
                const websiteDiv = document.createElement('div');
                websiteDiv.className = 'website-item';
                
                websiteDiv.innerHTML = `
                    <div class="website-info">
                        <div class="website-domain">🌐 ${website.domain}</div>
                        <small style="color: #666;">Status: ${website.has_preview ? '🟢 Preview Running' : '⚪ Ready to Preview'}</small>
                    </div>
                    <div class="website-actions">
                        <button class="button success small-button" onclick="previewWebsite('${website.domain}')">
                            <span class="emoji">👁️</span>Preview
                        </button>
                    </div>
                `;
                
                websitesList.appendChild(websiteDiv);
            });
        }
        
        function previewWebsite(domain) {
            showStatus(`Starting preview server for ${domain}...`, 'info');
            
            fetch(`/api/preview/${domain}`)
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        // Open preview in new window/tab
                        window.open(data.preview_url, '_blank');
                        showStatus(`Preview started! Opening ${domain} at ${data.preview_url}`, 'success');
                        
                        // Refresh the websites list to update status
                        setTimeout(() => {
                            loadClonedWebsites();
                        }, 1000);
                    } else {
                        showStatus('Error starting preview: ' + data.error, 'error');
                    }
                })
                .catch(error => {
                    showStatus('Error: ' + error.message, 'error');
                });
        }
        
        function showStatus(message, type = 'info') {
            status.innerHTML = message;
            status.className = `status ${type}`;
            status.style.display = 'block';
        }
        
        form.addEventListener('submit', (e) => {
            e.preventDefault();
            
            const url = urlInput.value.trim();
            const downloadLocation = currentDownloadLocation;
            
            if (!url) {
                showStatus('Please enter a website URL', 'error');
                return;
            }
            
            if (!downloadLocation) {
                showStatus('Please choose a download location', 'error');
                return;
            }
            
            // Reset UI
            cloneBtn.disabled = true;
            cloneBtn.innerHTML = '<span class="emoji">⏳</span>Cloning...';
            progressContainer.style.display = 'block';
            showStatus('Initializing...', 'info');
            progressFill.style.width = '0%';
            
            // Start cloning
            socket.emit('clone_website', { 
                url: url, 
                downloadLocation: downloadLocation 
            });
        });
        
        socket.on('status_update', (data) => {
            showStatus(data.message, 'info');
            if (data.progress !== undefined) {
                progressFill.style.width = data.progress + '%';
            }
        });
        
        socket.on('clone_complete', (data) => {
            showStatus(`✅ Website cloned successfully!`, 'success');
            
            const actionsHtml = `
                <div class="download-actions">
                    <a href="${data.download_url}" class="download-link">
                        <span class="emoji">📥</span>Download ZIP Archive
                    </a>
                    <button class="button success" onclick="previewWebsite('${data.domain}')">
                        <span class="emoji">👁️</span>Preview Website
                    </button>
                    <button class="button secondary" onclick="showPreviewSection()">
                        <span class="emoji">📚</span>Show All Websites
                    </button>
                </div>
            `;
            
            status.innerHTML += actionsHtml;
            resetForm();
            
            // Auto-refresh websites list if it's visible
            if (previewSection.style.display === 'block') {
                setTimeout(() => {
                    loadClonedWebsites();
                }, 1000);
            }
        });
        
        socket.on('clone_error', (data) => {
            showStatus(`❌ Error: ${data.error}`, 'error');
            resetForm();
        });
        
        function resetForm() {
            cloneBtn.disabled = false;
            cloneBtn.innerHTML = '<span class="emoji">🚀</span>Clone Website';
            progressFill.style.width = '100%';
        }
        
        // Auto-load websites on page load if section is visible
        window.addEventListener('load', () => {
            if (previewSection.style.display === 'block') {
                loadClonedWebsites();
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
    print("🌐 Web Cloner Server - Enhanced Version")
    print("=" * 60)
    print("🚀 Features:")
    print("   • Choose custom download location")
    print("   • Preview cloned websites locally")
    print("   • Download ZIP archives")
    print("   • Manage multiple cloned sites")
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
    
    # Run Flask app
    try:
        socketio.run(app, host='localhost', port=5000, debug=False, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        print("\n🛑 Server stopped by user")
    except Exception as e:
        print(f"❌ Server error: {e}")
    finally:
        # Clean up preview servers
        for domain, server_info in preview_servers.items():
            try:
                if 'httpd' in server_info:
                    server_info['httpd'].shutdown()
            except:
                pass
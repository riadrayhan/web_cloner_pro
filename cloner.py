from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit
import os
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import re
import base64
import mimetypes
from threading import Thread
import time
import zipfile
import shutil
import tempfile
import uuid
from datetime import datetime
from werkzeug.exceptions import abort
from functools import wraps
import random

# Get port from environment variable (required for Render)
port = int(os.environ.get('PORT', 5000))

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'web_cloner_secret_key_' + str(uuid.uuid4()))

# Configure SocketIO for production deployment with gevent
socketio = SocketIO(app, 
                    async_mode='gevent',  
                    cors_allowed_origins="*",
                    logger=False,
                    engineio_logger=False)

# Use a persistent directory in the app root for both local and Render
base_output_dir = os.path.join(os.getcwd(), 'clones')
os.makedirs(base_output_dir, exist_ok=True)

def retry(max_retries=3, delay=1):
    """Retry decorator for download functions"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_retries - 1:
                        raise e
                    time.sleep(delay * (2 ** attempt) + random.uniform(0, 1))  # Exponential backoff
            return None
        return wrapper
    return decorator

class WebClonerCore:
    """Core web cloning functionality"""
    
    def __init__(self, socketio_instance=None, sid=None, namespace='/'):
        self.socketio = socketio_instance
        self.sid = sid
        self.namespace = namespace
        self.downloaded_resources = set()
        self.visited_pages = set()
        self.max_pages = 10  # Limit internal pages to prevent overload
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
        })
    
    def emit_status(self, message, progress=None):
        """Emit status update via SocketIO with explicit sid and namespace"""
        if self.socketio and self.sid:
            data = {'message': message}
            if progress is not None:
                data['progress'] = progress
            self.socketio.emit('status_update', data, room=self.sid, namespace=self.namespace)
        print(f"Status: {message} ({progress}%)" if progress else f"Status: {message}")
    
    def clone_website(self, url, output_base_dir, clone_name=None):
        """Main cloning function"""
        try:
            self.emit_status("Starting website cloning...", 0)
            
            parsed_url = urlparse(url)
            domain = parsed_url.netloc.replace(':', '_')
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            unique_dir = clone_name or f"{domain}_{timestamp}"
            output_dir = os.path.join(output_base_dir, unique_dir)
            
            os.makedirs(output_dir, exist_ok=True)
            assets_dir = os.path.join(output_dir, 'assets')
            os.makedirs(assets_dir, exist_ok=True)
            
            self.emit_status(f"Downloading main page from {url}...", 10)
            response = self._get_with_retry(url, timeout=60)
            if not response:
                raise Exception("Failed to download main page after retries")
            response.raise_for_status()
            self.visited_pages.add(url)
            
            self.emit_status("Parsing HTML content...", 20)
            soup = BeautifulSoup(response.content, 'html.parser')
            
            self.emit_status("Processing images and resources...", 30)
            self.process_images(soup, url, assets_dir)
            self.emit_status("Images processed", 50)
            
            self.emit_status("Processing CSS files...", 60)
            self.process_css_files(soup, url, assets_dir)
            
            self.emit_status("Processing JavaScript files...", 70)
            self.process_js_files(soup, url, assets_dir)
            
            self.emit_status("Processing fonts and other resources...", 75)
            self.process_fonts_and_resources(soup, url, assets_dir)
            
            self.emit_status("Processing internal links...", 80)
            self.process_internal_links(soup, url, output_dir)
            
            self.emit_status("Saving HTML file...", 90)
            html_file = os.path.join(output_dir, 'index.html')
            with open(html_file, 'w', encoding='utf-8') as f:
                f.write(str(soup))
            
            self.emit_status("Creating downloadable archive...", 95)
            zip_path = self.create_zip_archive(output_dir, unique_dir)
            
            self.emit_status(f"Website cloned successfully!", 100)
            
            return {
                'success': True,
                'output_dir': output_dir,
                'zip_path': zip_path,
                'domain': unique_dir
            }
            
        except Exception as e:
            self.emit_status(f"Error: {str(e)}", 0)
            return {
                'success': False,
                'error': str(e)
            }
    
    def _get_with_retry(self, url, timeout=30):
        """Get with retry"""
        @retry(max_retries=3, delay=2)
        def get_request():
            self.session.headers['Referer'] = urlparse(url).scheme + '://' + urlparse(url).netloc
            return self.session.get(url, timeout=timeout)
        return get_request()
    
    def create_zip_archive(self, source_dir, unique_name):
        """Create ZIP archive of cloned website"""
        zip_name = f"{unique_name}_cloned.zip"
        zip_path = os.path.join(base_output_dir, zip_name)
        
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
        
        for tag in img_tags:
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
                print(f"Error processing image: {e}")
        
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
    
    def process_fonts_and_resources(self, soup, base_url, assets_dir):
        """Process font files and other resources"""
        for link in soup.find_all('link'):
            href = link.get('href')
            if href:
                rel = link.get('rel', [])
                if 'stylesheet' not in rel:
                    local_path = self.download_resource(href, base_url, assets_dir)
                    if local_path:
                        link['href'] = local_path
    
    def process_internal_links(self, soup, base_url, output_dir):
        """Process internal page links with limits"""
        base_domain = urlparse(base_url).netloc
        internal_links = []
        
        for link in soup.find_all('a', href=True):
            href = link['href']
            full_url = urljoin(base_url, href)
            link_domain = urlparse(full_url).netloc
            
            if link_domain == base_domain and full_url not in self.visited_pages:
                internal_links.append(full_url)
        
        # Limit to max_pages
        for full_url in internal_links[:self.max_pages - len(self.visited_pages)]:
            try:
                self.emit_status(f"Downloading internal page: {full_url}", None)
                response = self._get_with_retry(full_url, timeout=45)
                if response and response.status_code == 200:
                    self.visited_pages.add(full_url)
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
                    # Update link href to relative path (this is approximate, as it's for main page links)
                    link['href'] = rel_path
                else:
                    print(f"Failed to download internal page {full_url}")
            except Exception as e:
                print(f"Error downloading internal page {full_url}: {e}")
    
    @retry(max_retries=3, delay=1)
    def download_resource(self, url, base_url, assets_dir):
        """Download a resource and return local path"""
        try:
            if url.startswith('data:'):
                return self.save_data_uri(url, assets_dir)
            
            full_url = urljoin(base_url, url)
            
            if full_url in self.downloaded_resources:
                return self.get_local_path(full_url, assets_dir)
            
            response = self._get_with_retry(full_url, timeout=30)
            if not response:
                raise Exception("Download failed after retries")
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

# Flask routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/download/<path:filename>')
def download_file(filename):
    """Serve downloaded files"""
    try:
        if '..' in filename or filename.startswith('/'):
            return jsonify({'error': 'Invalid filename'}), 400
            
        file_path = os.path.join(base_output_dir, filename)
        if os.path.exists(file_path) and os.path.commonpath([file_path, base_output_dir]) == base_output_dir:
            return send_from_directory(base_output_dir, filename, as_attachment=True)
        else:
            return jsonify({'error': 'File not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 404

@app.route('/api/get_cloned_websites')
def get_cloned_websites():
    """Get list of cloned websites"""
    try:
        websites = []
        for item in os.listdir(base_output_dir):
            item_path = os.path.join(base_output_dir, item)
            if os.path.isdir(item_path):
                index_path = os.path.join(item_path, 'index.html')
                if os.path.exists(index_path):
                    websites.append({
                        'domain': item,
                        'path': item_path,
                        'has_preview': True  # Previews are always available via Flask
                    })
        
        websites.sort(key=lambda x: os.path.getctime(x['path']), reverse=True)
        
        return jsonify({'websites': websites})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/preview/<domain>')
def preview_website(domain):
    """Get preview URL for a cloned website"""
    try:
        if '..' in domain or '/' in domain:
            return jsonify({'error': 'Invalid domain'}), 400
            
        website_path = os.path.join(base_output_dir, domain)
        
        if not os.path.exists(website_path):
            return jsonify({'error': 'Website not found'}), 404
        
        index_path = os.path.join(website_path, 'index.html')
        if not os.path.exists(index_path):
            return jsonify({'error': 'No index.html found'}), 404
        
        preview_url = f"/preview/{domain}/"
        
        return jsonify({
            'success': True,
            'preview_url': preview_url,
            'domain': domain
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/preview/<domain>/')
def preview_root(domain):
    return serve_cloned_file(domain, '')

@app.route('/preview/<domain>/<path:path>')
def preview_file(domain, path):
    return serve_cloned_file(domain, path)

def serve_cloned_file(domain, path):
    directory = os.path.join(base_output_dir, domain)
    if '..' in domain or '..' in path:
        abort(404)
    if not os.path.exists(directory):
        abort(404)

    if not path:
        path = 'index.html'
    elif path.endswith('/'):
        path += 'index.html'

    full_path = os.path.join(directory, path)
    if not os.path.exists(full_path) and '.' not in os.path.basename(path):
        html_path = path + '.html'
        if os.path.exists(os.path.join(directory, html_path)):
            path = html_path
        else:
            index_path = os.path.join(path, 'index.html')
            if os.path.exists(os.path.join(directory, index_path)):
                path = index_path

    if not os.path.exists(os.path.join(directory, path)) or not os.path.isfile(os.path.join(directory, path)):
        abort(404)

    response = send_from_directory(directory, path)
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response

@app.route('/api/delete_website/<domain>', methods=['DELETE'])
def delete_website(domain):
    """Delete a specific cloned website"""
    try:
        if '..' in domain or '/' in domain:
            return jsonify({'error': 'Invalid domain'}), 400
            
        website_path = os.path.join(base_output_dir, domain)
        zip_path = os.path.join(base_output_dir, f"{domain}_cloned.zip")
        
        deleted = False
        if os.path.exists(website_path):
            shutil.rmtree(website_path)
            deleted = True
        
        if os.path.exists(zip_path):
            os.remove(zip_path)
            deleted = True
        
        if deleted:
            return jsonify({'success': True, 'message': f'Website {domain} deleted'})
        else:
            return jsonify({'error': 'Website not found'}), 404
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/cleanup', methods=['POST'])
def cleanup_old_files():
    """Cleanup old files"""
    try:
        current_time = time.time()
        cleanup_count = 0
        max_age_hours = 24
        
        for item in os.listdir(base_output_dir):
            item_path = os.path.join(base_output_dir, item)
            if os.path.isfile(item_path):
                file_age = current_time - os.path.getctime(item_path)
                if file_age > (max_age_hours * 3600):
                    try:
                        os.remove(item_path)
                        cleanup_count += 1
                    except:
                        pass
            elif os.path.isdir(item_path):
                dir_age = current_time - os.path.getctime(item_path)
                if dir_age > (max_age_hours * 3600):
                    try:
                        shutil.rmtree(item_path)
                        cleanup_count += 1
                    except:
                        pass
        
        return jsonify({'success': True, 'cleaned': cleanup_count})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# SocketIO events
@socketio.on('connect')
def handle_connect():
    print(f"Client connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    print(f"Client disconnected: {request.sid}")

@socketio.on('clone_website')
def handle_clone_request(data):
    """Handle website cloning request"""
    sid = request.sid
    namespace = request.namespace
    
    def clone_task():
        url = data.get('url')
        clone_name = data.get('clone_name', None)  # User-provided clone name
        if not url:
            socketio.emit('clone_error', {'error': 'No URL provided'}, room=sid, namespace=namespace)
            return
        
        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url
        
        cloner = WebClonerCore(socketio, sid, namespace)
        result = cloner.clone_website(url, base_output_dir, clone_name)
        
        if result['success']:
            zip_filename = os.path.basename(result['zip_path'])
            socketio.emit('clone_complete', {
                'domain': result['domain'],
                'download_url': f'/download/{zip_filename}',
                'folder_path': result['output_dir']
            }, room=sid, namespace=namespace)
        else:
            socketio.emit('clone_error', {'error': result['error']}, room=sid, namespace=namespace)
    
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
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.5.0/socket.io.js"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
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

        .info-card {
            background: rgba(30, 58, 138, 0.05);
            border: 1px solid rgba(30, 58, 138, 0.1);
            padding: 24px;
            border-radius: 12px;
            margin-bottom: 32px;
        }

        .info-title {
            font-size: 1.25rem;
            font-weight: 600;
            color: var(--primary);
            margin-bottom: 16px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .info-steps {
            list-style: none;
            color: var(--text-secondary);
        }

        .info-steps li {
            margin: 12px 0;
            padding-left: 28px;
            position: relative;
            font-size: 0.95rem;
        }

        .info-steps li::before {
            content: counter(step-counter);
            counter-increment: step-counter;
            position: absolute;
            left: 0;
            top: 2px;
            background: var(--primary);
            color: white;
            width: 20px;
            height: 20px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.8rem;
            font-weight: 600;
        }

        .info-steps {
            counter-reset: step-counter;
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

        .button.danger {
            background: var(--danger);
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
            <div class="input-group">
                <label class="label" for="cloneName">
                    <i class="fas fa-tag"></i>
                    Clone Name (Optional - for custom ZIP/folder name)
                </label>
                <input 
                    type="text" 
                    id="cloneName" 
                    class="input" 
                    placeholder="MyClone_2025"
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
        const cloneNameInput = document.getElementById('cloneName');
        const cloneBtn = document.getElementById('cloneBtn');
        const progressContainer = document.getElementById('progressContainer');
        const progressFill = document.getElementById('progressFill');
        const progressText = document.getElementById('progressText');
        const status = document.getElementById('status');
        const previewSection = document.getElementById('previewSection');
        const websitesList = document.getElementById('websitesList');
        
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
                            <span class="status-indicator online"></span>
                            Preview Available
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
            showStatus(`Starting preview for ${domain}...`, 'info');
            
            fetch(`/api/preview/${domain}`)
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        const previewUrl = data.preview_url;
                        const newWindow = window.open(previewUrl, '_blank');
                        if (newWindow) {
                            showStatus(`Preview started for ${domain}`, 'success');
                            setTimeout(loadClonedWebsites, 1000);
                        } else {
                            showStatus('Please allow popups or open manually: ' + previewUrl, 'warning');
                        }
                    } else {
                        showStatus('Error: ' + data.error, 'error');
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
            
            if (type !== 'error') {
                setTimeout(() => {
                    status.style.display = 'none';
                }, 5000);
            }
        }
        
        function resetForm() {
            cloneBtn.disabled = false;
            cloneBtn.innerHTML = '<i class="fas fa-rocket"></i> Clone Website';
            progressContainer.style.display = 'none';
            progressFill.style.width = '0%';
            progressText.textContent = 'Initializing...';
        }
        
        form.addEventListener('submit', (e) => {
            e.preventDefault();
            
            let url = urlInput.value.trim();
            let cloneName = cloneNameInput.value.trim();
            if (!url) {
                showStatus('Please enter a website URL', 'error');
                return;
            }
            
            if (!url.match(/^https?:\/\//)) {
                url = 'https://' + url;
                urlInput.value = url;
            }
            
            cloneBtn.disabled = true;
            cloneBtn.innerHTML = '<span class="loading-spinner"></span> Cloning...';
            progressContainer.style.display = 'block';
            showStatus('Initializing cloning process...', 'info');
            progressFill.style.width = '0%';
            progressText.textContent = 'Initializing...';
            
            const payload = { url: url };
            if (cloneName) {
                payload.clone_name = cloneName;
            }
            socket.emit('clone_website', payload);
        });
        
        socket.on('status_update', (data) => {
            showStatus(data.message, 'info');
            if (data.progress !== undefined) {
                progressFill.style.width = data.progress + '%';
                progressText.textContent = `${data.message} (${data.progress}%)`;
            }
        });
        
        socket.on('clone_complete', (data) => {
            showStatus(`Website cloned successfully!`, 'success');
            progressText.textContent = 'Completed successfully!';
            
            const actionsHtml = `
                <div class="download-actions">
                    <a href="${data.download_url}" class="download-link" download>
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
                setTimeout(loadClonedWebsites, 1000);
            }
        });
        
        socket.on('clone_error', (data) => {
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
                this.style.borderColor = '';
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
    create_templates()
    
    print("=" * 60)
    print("🌐 Web Cloner Pro - Enhanced Version")
    print("=" * 60)
    print("🚀 Features:")
    print("   • Modern, attractive UI with professional design")
    print("   • Preview cloned websites locally")
    print("   • Download ZIP archives")
    print("   • Manage multiple cloned sites")
    print("   • Real-time progress tracking")
    print("   • Responsive design for all devices")
    print("   • Custom clone names for ZIP/folder")
    print("   • Improved reliability with retries and timeouts")
    print()
    print("📱 Web Interface: http://localhost:" + str(port))
    print("📁 Download Location:", base_output_dir)
    print()
    print("🔧 Keep this terminal open to run the server")
    print("=" * 60)
    
    socketio.run(app, host='0.0.0.0', port=port, allow_unsafe_werkzeug=True)
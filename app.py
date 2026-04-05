import os
import json
import hashlib
import time
import requests
from flask import Flask, request, render_template, Response, session, redirect, url_for
from werkzeug.utils import secure_filename

app = Flask(__name__)

# 配置 Session
app.secret_key = os.getenv('SECRET_KEY', os.urandom(32).hex())

# 配置
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'doc', 'docx'}
BLOB_TOKEN = os.getenv('BLOB_READ_WRITE_TOKEN')
PIN_HASH = os.getenv('PIN_HASH')  # 存储的是 SHA256 哈希值

# Vercel Blob Storage API 端点
BLOB_API_URL = "https://blob.vercel-storage.com"

# 登录尝试限制（内存存储，重启后重置）
login_attempts = {}
MAX_ATTEMPTS = 5
LOCKOUT_TIME = 300  # 5分钟锁定时间

def get_client_ip():
    """获取客户端 IP 地址"""
    if request.headers.getlist("X-Forwarded-For"):
        return request.headers.getlist("X-Forwarded-For")[0]
    return request.remote_addr

def is_ip_locked(ip):
    """检查 IP 是否被锁定"""
    if ip in login_attempts:
        attempts = login_attempts[ip]
        if attempts['count'] >= MAX_ATTEMPTS:
            if time.time() - attempts['last_attempt'] < LOCKOUT_TIME:
                return True
            else:
                # 锁定时间已过，重置计数器
                login_attempts[ip] = {'count': 0, 'last_attempt': 0}
    return False

def record_failed_attempt(ip):
    """记录失败的登录尝试"""
    if ip not in login_attempts:
        login_attempts[ip] = {'count': 0, 'last_attempt': 0}
    
    login_attempts[ip]['count'] += 1
    login_attempts[ip]['last_attempt'] = time.time()

def check_pin(pin):
    """验证 PIN 码"""
    if not PIN_HASH:
        # 如果没有设置 PIN_HASH，打印警告并允许任何 PIN 通过（仅用于开发/测试）
        print("警告：未设置 PIN_HASH 环境变量！任何人都可访问 /teacher 页面。请在 Vercel 环境变量中设置 PIN_HASH。")
        return True

    # 计算 PIN 的 SHA256 哈希值
    pin_hash = hashlib.sha256(pin.encode()).hexdigest()
    return pin_hash == PIN_HASH

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- 路由：普通用户 (上传) ---
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        if 'file' not in request.files:
            return '没有文件部分'
        
        file = request.files['file']
        
        if file.filename == '':
            return '没有选择文件'
        
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file_content = file.read()
            
            # 上传到 Blob Storage
            try:
                headers = {
                    'Authorization': f'Bearer {BLOB_TOKEN}',
                    'Content-Type': file.content_type or 'application/octet-stream'
                }
                
                response = requests.put(
                    f'{BLOB_API_URL}/uploads/{filename}',
                    data=file_content,
                    headers=headers
                )
                
                print(f"Upload response status: {response.status_code}")
                print(f"Upload response body: {response.text}")
                
                if response.status_code in [200, 201]:
                    return '文件上传成功！<br><a href="/">继续上传</a>'
                else:
                    return f'上传失败 (HTTP {response.status_code}): {response.text}'
                    
            except Exception as e:
                print(f"Upload error: {str(e)}")
                return f'上传失败: {str(e)}'
            
    return render_template('index.html')

# --- 路由：PIN 验证 ---
@app.route('/teacher/verify', methods=['GET', 'POST'])
def verify_pin():
    ip = get_client_ip()
    
    # 检查 IP 是否被锁定
    if is_ip_locked(ip):
        remaining_time = int(LOCKOUT_TIME - (time.time() - login_attempts[ip]['last_attempt']))
        return render_template('pin_verify.html',
                             error=f'登录尝试次数过多，请 {remaining_time} 秒后再试',
                             locked=True)

    if request.method == 'POST':
        pin = request.form.get('pin', '')

        if check_pin(pin):
            # 验证成功，清除该 IP 的失败记录
            if ip in login_attempts:
                del login_attempts[ip]

            # 设置 session
            session['authenticated'] = True
            session['auth_time'] = time.time()

            return redirect(url_for('teacher'))
        else:
            # 验证失败，记录尝试
            record_failed_attempt(ip)
            attempts_left = MAX_ATTEMPTS - login_attempts[ip]['count']

            if attempts_left <= 0:
                return render_template('pin_verify.html',
                                     error='登录尝试次数过多，请 5 分钟后再试',
                                     locked=True)
            else:
                return render_template('pin_verify.html',
                                     error=f'PIN 错误，剩余尝试次数: {attempts_left}')

    # GET 请求，显示验证页面
    return render_template('pin_verify.html')

# --- 路由：老师 (查看) ---
@app.route('/teacher')
def teacher():
    # 检查是否已验证
    if not session.get('authenticated'):
        return redirect(url_for('verify_pin'))
    
    # 检查 session 是否过期（30分钟）
    auth_time = session.get('auth_time', 0)
    if time.time() - auth_time > 1800:  # 30分钟
        session.clear()
        return redirect(url_for('verify_pin'))
    
    try:
        headers = {
            'Authorization': f'Bearer {BLOB_TOKEN}'
        }
        
        # 列出 Blob Storage 中的文件
        response = requests.get(
            f'{BLOB_API_URL}?prefix=uploads/',
            headers=headers
        )
        
        if response.status_code == 200:
            data = response.json()
            
            # 打印调试信息，查看实际的数据结构
            print(f"Blob API response: {json.dumps(data, indent=2)}")
            
            # 尝试多种可能的字段名
            files = []
            if 'blobs' in data:
                for blob in data['blobs']:
                    # 尝试获取文件名
                    if 'name' in blob:
                        filename = blob['name']
                    elif 'path' in blob:
                        filename = blob['path']
                    elif 'url' in blob:
                        filename = blob['url'].split('/')[-1]
                    elif isinstance(blob, str):
                        filename = blob
                    else:
                        filename = str(blob)
                    
                    # 移除 uploads/ 前缀
                    if filename.startswith('uploads/'):
                        filename = filename.replace('uploads/', '')
                    files.append(filename)
            elif isinstance(data, list):
                # 如果直接返回数组
                for blob in data:
                    if isinstance(blob, dict):
                        filename = blob.get('name', blob.get('path', blob.get('url', str(blob))))
                        if filename.startswith('uploads/'):
                            filename = filename.replace('uploads/', '')
                        files.append(filename)
                    else:
                        files.append(str(blob))
            else:
                files = [str(data)]
            
            return render_template('teacher.html', files=files)
        else:
            return f'获取文件列表失败 (HTTP {response.status_code}): {response.text}'
            
    except Exception as e:
        return f'获取文件列表失败: {str(e)}'

# --- 路由：退出登录 ---
@app.route('/teacher/logout')
def logout():
    session.clear()
    return redirect(url_for('verify_pin'))

# --- 路由：下载/预览文件 ---
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    try:
        headers = {
            'Authorization': f'Bearer {BLOB_TOKEN}'
        }
        
        response = requests.get(
            f'{BLOB_API_URL}/uploads/{filename}',
            headers=headers
        )
        
        print(f"Download response status: {response.status_code}")
        print(f"Download response headers: {dict(response.headers)}")
        
        if response.status_code == 200:
            file_data = response.content
            file_response = Response(file_data)
            file_response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
            file_response.headers['Content-Type'] = response.headers.get('Content-Type', 'application/octet-stream')
            return file_response
        else:
            return f'文件不存在 (HTTP {response.status_code}): {response.text}'
            
    except Exception as e:
        print(f"Download error: {str(e)}")
        return f'文件不存在: {str(e)}'

# Vercel Serverless Function 入口点
if __name__ == '__main__':
    app.run(debug=False, port=5000)

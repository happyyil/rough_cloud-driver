import os
import json
import hashlib
import time
import base64
import boto3
import requests
from botocore.config import Config
from flask import Flask, request, render_template, Response, session, redirect, url_for, jsonify
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', os.urandom(32).hex())

ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'doc', 'docx', 'py', 'zip', 'mp4', 'mp3'}
PIN_HASH = os.getenv('PIN_HASH')

# Vercel Blob 配置（小文件存储）
BLOB_READ_WRITE_TOKEN = os.getenv('BLOB_READ_WRITE_TOKEN')

# Cloudflare R2 配置（大文件存储）
R2_ACCOUNT_ID = os.getenv('R2_ACCOUNT_ID')
R2_ACCESS_KEY_ID = os.getenv('R2_ACCESS_KEY_ID')
R2_SECRET_KEY = os.getenv('R2_SECRET_KEY')
R2_BUCKET_NAME = os.getenv('R2_BUCKET_NAME', 'my-uploads')
R2_PUBLIC_URL = os.getenv('R2_PUBLIC_URL')

# 大小文件分界线：4.5MB
LARGE_FILE_THRESHOLD = 4.5 * 1024 * 1024

# 创建 S3 客户端（R2 兼容 S3 API）
s3_client = None
if all([R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_KEY]):
    s3_client = boto3.client(
        's3',
        endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_KEY,
        config=Config(signature_version='s3v4')
    )

# 登录限制 - 文件持久化存储
MAX_ATTEMPTS = 5
LOCKOUT_TIME = 300
LOGIN_ATTEMPTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.login_attempts.json')

# ========== 下载链接编码 ==========

def encode_download_token(filename, storage):
    """将文件名+存储类型编码为 URL 安全的 token"""
    payload = f'{filename}:{storage}'
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip('=')

def decode_download_token(token):
    """解码 token，返回 (filename, storage) 或 None"""
    try:
        padding = 4 - len(token) % 4
        if padding != 4:
            token += '=' * padding
        payload = base64.urlsafe_b64decode(token.encode()).decode()
        filename, storage = payload.rsplit(':', 1)
        return filename, storage
    except Exception:
        return None

def load_login_attempts():
    """从文件加载登录尝试记录"""
    try:
        if os.path.exists(LOGIN_ATTEMPTS_FILE):
            with open(LOGIN_ATTEMPTS_FILE, 'r') as f:
                data = json.load(f)
                # 清理过期记录
                current_time = time.time()
                return {ip: v for ip, v in data.items()
                        if current_time - v['last_attempt'] < LOCKOUT_TIME}
    except (json.JSONDecodeError, IOError):
        pass
    return {}

def save_login_attempts(attempts):
    """保存登录尝试记录到文件"""
    try:
        with open(LOGIN_ATTEMPTS_FILE, 'w') as f:
            json.dump(attempts, f)
    except IOError:
        pass

def get_client_ip():
    if request.headers.getlist("X-Forwarded-For"):
        return request.headers.getlist("X-Forwarded-For")[0]
    return request.remote_addr

def is_ip_locked(ip):
    login_attempts = load_login_attempts()
    if ip in login_attempts:
        attempts = login_attempts[ip]
        if attempts['count'] >= MAX_ATTEMPTS:
            if time.time() - attempts['last_attempt'] < LOCKOUT_TIME:
                return True
            else:
                del login_attempts[ip]
                save_login_attempts(login_attempts)
    return False

def record_failed_attempt(ip):
    login_attempts = load_login_attempts()
    if ip not in login_attempts:
        login_attempts[ip] = {'count': 0, 'last_attempt': 0}
    login_attempts[ip]['count'] += 1
    login_attempts[ip]['last_attempt'] = time.time()
    save_login_attempts(login_attempts)

def clear_failed_attempts(ip):
    """清除指定 IP 的失败记录"""
    login_attempts = load_login_attempts()
    if ip in login_attempts:
        del login_attempts[ip]
        save_login_attempts(login_attempts)

def check_pin(pin):
    if not PIN_HASH:
        print("警告：未设置 PIN_HASH 环境变量！")
        return True
    pin_hash = hashlib.sha256(pin.encode()).hexdigest()
    return pin_hash == PIN_HASH

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_r2_url(key):
    """获取 R2 文件的公共 URL"""
    if R2_PUBLIC_URL:
        return f'{R2_PUBLIC_URL}/{key}'
    return f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com/{R2_BUCKET_NAME}/{key}'

# ========== Vercel Blob 操作 ==========

def blob_upload(filename, file_data, content_type):
    """上传文件到 Vercel Blob"""
    if not BLOB_READ_WRITE_TOKEN:
        raise Exception('BLOB_READ_WRITE_TOKEN 未配置')

    pathname = f'uploads/{filename}'

    headers = {
        'Authorization': f'Bearer {BLOB_READ_WRITE_TOKEN}',
        'x-vercel-blob-access': 'public',
        'x-vercel-blob-content-type': content_type,
    }

    response = requests.put(
        f'https://blob.vercel-storage.com/{pathname}',
        headers=headers,
        data=file_data,
        timeout=60
    )

    if response.status_code not in (200, 201):
        raise Exception(f'Blob upload failed [{response.status_code}]: {response.text}')

    return response.json()

def blob_list():
    """列出 Vercel Blob 中的所有文件"""
    if not BLOB_READ_WRITE_TOKEN:
        return []

    headers = {
        'Authorization': f'Bearer {BLOB_READ_WRITE_TOKEN}',
    }

    response = requests.get(
        'https://blob.vercel-storage.com/?prefix=uploads/',
        headers=headers,
        timeout=30
    )

    if response.status_code != 200:
        return []

    data = response.json()
    return data.get('blobs', [])

# ========== R2 上传相关 API ==========

@app.route('/api/r2/presign', methods=['POST'])
def get_presigned_url():
    """生成预签名上传 URL，客户端用此 URL 直传到 R2"""
    if not s3_client:
        return jsonify({'error': 'R2 未配置'}), 500
    
    data = request.get_json() or {}
    filename = data.get('filename', '')
    
    if not filename or not allowed_file(filename):
        return jsonify({'error': '不支持的文件类型'}), 400
    
    ext = filename.rsplit('.', 1)[1].lower() if '.' in filename else ''
    timestamp = str(int(time.time()))
    unique_name = f'{timestamp}.{ext}' if ext else timestamp
    key = f'uploads/{unique_name}'
    
    try:
        presigned_url = s3_client.generate_presigned_url(
            'put_object',
            Params={
                'Bucket': R2_BUCKET_NAME,
                'Key': key,
                'ContentType': data.get('contentType', 'application/octet-stream')
            },
            ExpiresIn=900
        )

        token = encode_download_token(unique_name, 'r2')

        return jsonify({
            'presignedUrl': presigned_url,
            'downloadUrl': f'/d/{token}',
            'key': key,
            'filename': unique_name
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ========== 多文件上传 API ==========

@app.route('/api/upload', methods=['POST'])
def upload_files():
    """批量上传文件（小文件 → Vercel Blob，大文件 → R2）"""
    if 'files' not in request.files:
        return jsonify({'success': False, 'error': '没有文件部分'}), 400

    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'success': False, 'error': '没有选择文件'}), 400

    uploaded = []
    errors = []

    for file in files:
        if file.filename == '':
            continue

        original_filename = file.filename
        if '.' not in original_filename:
            errors.append(f'{original_filename}: 没有扩展名')
            continue

        ext = original_filename.rsplit('.', 1)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            errors.append(f'{original_filename}: 不支持的文件类型 .{ext}')
            continue

        timestamp = str(int(time.time()))
        filename = f'{timestamp}.{ext}'
        file_data = file.read()
        content_type = file.content_type or 'application/octet-stream'

        try:
            if len(file_data) <= LARGE_FILE_THRESHOLD:
                # 小文件上传到 Vercel Blob
                if not BLOB_READ_WRITE_TOKEN:
                    errors.append(f'{original_filename}: BLOB_READ_WRITE_TOKEN 未配置')
                    continue
                result = blob_upload(filename, file_data, content_type)
                result_url = result.get('url')
                storage = 'blob'
            else:
                # 大文件上传到 R2
                if not s3_client:
                    errors.append(f'{original_filename}: R2 未配置')
                    continue
                key = f'uploads/{filename}'
                s3_client.put_object(
                    Bucket=R2_BUCKET_NAME,
                    Key=key,
                    Body=file_data,
                    ContentType=content_type
                )
                result_url = get_r2_url(key)
                storage = 'r2'

            token = encode_download_token(filename, storage)
            uploaded.append({'name': original_filename, 'download_url': f'/d/{token}', 'storage': storage})
        except Exception as e:
            errors.append(f'{original_filename}: {str(e)}')

    if errors and not uploaded:
        return jsonify({'success': False, 'error': '; '.join(errors)})

    return jsonify({
        'success': True,
        'uploaded': uploaded,
        'errors': errors if errors else None
    })

# ========== 原有路由 ==========

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        if 'file' not in request.files:
            return '没有文件部分'
        
        file = request.files['file']
        
        if file.filename == '':
            return '没有选择文件'
        
        original_filename = file.filename
        if '.' not in original_filename:
            return '文件没有扩展名'
        
        ext = original_filename.rsplit('.', 1)[1].lower()
        
        if ext not in ALLOWED_EXTENSIONS:
            return f'不支持的文件类型: .{ext}。允许: {", ".join(ALLOWED_EXTENSIONS)}'
        
        timestamp = str(int(time.time()))
        filename = f'{timestamp}.{ext}'
        key = f'uploads/{filename}'
        
        if not s3_client:
            return 'R2 未配置，请联系管理员', 500
        
        try:
            s3_client.put_object(
                Bucket=R2_BUCKET_NAME,
                Key=key,
                Body=file.read(),
                ContentType=file.content_type or 'application/octet-stream'
            )
            return f'文件上传成功！<br><a href="/">继续上传</a>'
            
        except Exception as e:
            return f'上传失败: {str(e)}'
    
    return render_template('index.html')

@app.route('/teacher/verify', methods=['GET', 'POST'])
def verify_pin():
    ip = get_client_ip()

    if is_ip_locked(ip):
        login_attempts = load_login_attempts()
        last_attempt = login_attempts.get(ip, {}).get('last_attempt', time.time())
        remaining_time = int(LOCKOUT_TIME - (time.time() - last_attempt))
        return render_template('pin_verify.html',
                             error=f'登录尝试次数过多，请 {remaining_time} 秒后再试',
                             locked=True)

    if request.method == 'POST':
        pin = request.form.get('pin', '')

        if check_pin(pin):
            clear_failed_attempts(ip)

            session['authenticated'] = True
            session['auth_time'] = time.time()

            return redirect(url_for('teacher'))
        else:
            record_failed_attempt(ip)
            login_attempts = load_login_attempts()
            attempts_left = MAX_ATTEMPTS - login_attempts.get(ip, {}).get('count', 0)

            if attempts_left <= 0:
                return render_template('pin_verify.html',
                                     error='登录尝试次数过多，请 5 分钟后再试',
                                     locked=True)
            else:
                return render_template('pin_verify.html',
                                     error=f'PIN 错误，剩余尝试次数: {attempts_left}')

    return render_template('pin_verify.html')

@app.route('/teacher')
def teacher():
    if not session.get('authenticated'):
        return redirect(url_for('verify_pin'))

    auth_time = session.get('auth_time', 0)
    if time.time() - auth_time > 1800:
        session.clear()
        return redirect(url_for('verify_pin'))

    files = []

    # 从 Vercel Blob 获取文件
    try:
        blob_files = blob_list()
        for blob in blob_files:
            pathname = blob.get('pathname', '')
            if not pathname.startswith('uploads/'):
                continue
            filename = pathname.replace('uploads/', '', 1)
            files.append({
                'name': filename,
                'url': blob.get('url'),
                'size': blob.get('size', 0),
                'uploaded_at': blob.get('uploadedAt', ''),
                'storage': 'blob',
                'token': encode_download_token(filename, 'blob')
            })
    except Exception as e:
        pass

    # 从 R2 获取文件
    if s3_client:
        try:
            continuation_token = None
            while True:
                list_kwargs = {
                    'Bucket': R2_BUCKET_NAME,
                    'Prefix': 'uploads/',
                    'MaxKeys': 1000
                }
                if continuation_token:
                    list_kwargs['ContinuationToken'] = continuation_token

                response = s3_client.list_objects_v2(**list_kwargs)

                for obj in response.get('Contents', []):
                    key = obj['Key']
                    filename = key.replace('uploads/', '', 1)
                    files.append({
                        'name': filename,
                        'url': get_r2_url(key),
                        'size': obj['Size'],
                        'uploaded_at': obj['LastModified'].isoformat(),
                        'storage': 'r2',
                        'token': encode_download_token(filename, 'r2')
                    })

                if response.get('IsTruncated'):
                    continuation_token = response['NextContinuationToken']
                else:
                    break
        except Exception as e:
            pass

    files.sort(key=lambda x: x.get('uploaded_at', ''), reverse=True)
    return render_template('teacher.html', files=files)

@app.route('/teacher/logout')
def logout():
    session.clear()
    return redirect(url_for('verify_pin'))

@app.route('/d/<token>')
def download_file(token):
    result = decode_download_token(token)
    if not result:
        return '无效的链接', 400
    filename, storage = result
    if storage == 'blob':
        blob_files = blob_list()
        for blob in blob_files:
            if blob.get('pathname') == f'uploads/{filename}':
                return redirect(blob.get('url', ''))
        return '文件不存在', 404
    return redirect(get_r2_url(f'uploads/{filename}'))

if __name__ == '__main__':
    app.run(debug=False, port=5000)

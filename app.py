import os
import json
import hashlib
import hmac
import time
import base64
import boto3
import requests
from botocore.config import Config
from flask import Flask, request, render_template, Response, session, redirect, url_for, jsonify, send_file
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', os.urandom(32).hex())

# Token 配置
TOKEN_EXPIRE_HOURS = 24  # Token 有效期

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

def encode_download_token(original_name, stored_name, storage, disposition='inline'):
    """将原始文件名+存储文件名+存储类型+下载方式编码为 URL 安全的 token"""
    payload = f'{original_name}:{stored_name}:{storage}:{disposition}'
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip('=')

def decode_download_token(token):
    """解码 token，返回 (original_name, stored_name, storage, disposition) 或 None"""
    try:
        padding = 4 - len(token) % 4
        if padding != 4:
            token += '=' * padding
        payload = base64.urlsafe_b64decode(token.encode()).decode()
        parts = payload.rsplit(':', 3)
        if len(parts) == 4:
            return parts[0], parts[1], parts[2], parts[3]
        if len(parts) == 3:
            return parts[0], parts[1], parts[2], 'inline'
        return None
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

def generate_api_token(pin):
    """生成 API Token（HMAC 签名，含过期时间）"""
    expire_time = int(time.time()) + TOKEN_EXPIRE_HOURS * 3600
    payload = f'api_user:{expire_time}'
    signature = hmac.new(
        app.secret_key.encode(),
        payload.encode(),
        hashlib.sha256
    ).hexdigest()
    token_data = f'{payload}:{signature}'
    return base64.urlsafe_b64encode(token_data.encode()).decode().rstrip('=')

def verify_api_token(token):
    """验证 API Token，返回 True/False"""
    try:
        # 补全 base64 padding
        padding = 4 - len(token) % 4
        if padding != 4:
            token += '=' * padding
        token_data = base64.urlsafe_b64decode(token.encode()).decode()
        parts = token_data.rsplit(':', 2)
        if len(parts) != 3:
            return False
        user_id, expire_time_str, signature = parts
        expire_time = int(expire_time_str)
        # 检查过期
        if time.time() > expire_time:
            return False
        # 验证签名
        payload = f'{user_id}:{expire_time_str}'
        expected_sig = hmac.new(
            app.secret_key.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature, expected_sig)
    except Exception:
        return False

def get_api_user():
    """从请求头获取并验证 API 用户，返回 (user_id, error_response)"""
    auth_header = request.headers.get('Authorization', '')
    token = None
    if auth_header.startswith('Bearer '):
        token = auth_header[7:]
    else:
        token = request.headers.get('X-API-Token')
    if not token:
        return None, (jsonify({'success': False, 'error': '未提供 Token'}), 401)
    if not verify_api_token(token):
        return None, (jsonify({'success': False, 'error': 'Token 无效或已过期'}), 401)
    return 'api_user', None

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
    disposition = data.get('disposition', 'inline')
    if disposition not in ('inline', 'attachment'):
        disposition = 'inline'
    
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

        token = encode_download_token(filename, unique_name, 'r2', disposition)

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

    disposition = request.form.get('disposition', 'inline')
    if disposition not in ('inline', 'attachment'):
        disposition = 'inline'

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

            token = encode_download_token(original_filename, filename, storage, disposition)
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
            token = encode_download_token(filename, filename, 'blob')
            files.append({
                'name': filename,
                'url': blob.get('url'),
                'size': blob.get('size', 0),
                'uploaded_at': blob.get('uploadedAt', ''),
                'storage': 'blob',
                'token': token,
                'disposition': 'inline'
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
                        'token': encode_download_token(filename, filename, 'r2'),
                        'disposition': 'inline'
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
    original_name, stored_name, storage, disposition = result

    if storage == 'blob':
        blob_files = blob_list()
        for blob in blob_files:
            if blob.get('pathname') == f'uploads/{stored_name}':
                resp = requests.get(blob.get('url', ''), timeout=60)
                response = Response(resp.content, content_type=resp.headers.get('Content-Type', 'application/octet-stream'))
                response.headers['Content-Disposition'] = f'{disposition}; filename="{original_name}"'
                return response
        return '文件不存在', 404
    else:
        url = get_r2_url(f'uploads/{stored_name}')
        resp = requests.get(url, timeout=60)
        response = Response(resp.content, content_type=resp.headers.get('Content-Type', 'application/octet-stream'))
        response.headers['Content-Disposition'] = f'{disposition}; filename="{original_name}"'
        return response

@app.route('/api/disposition', methods=['POST'])
def toggle_disposition():
    """切换文件的预览/下载模式，返回新的 token"""
    data = request.get_json() or {}
    token = data.get('token', '')
    result = decode_download_token(token)
    if not result:
        return jsonify({'error': '无效的 token'}), 400
    original_name, stored_name, storage, disposition = result
    new_disposition = 'attachment' if disposition == 'inline' else 'inline'
    new_token = encode_download_token(original_name, stored_name, storage, new_disposition)
    return jsonify({'token': new_token, 'disposition': new_disposition})

# ========== 程序化调用 API v1 ==========

@app.route('/api/v1/auth', methods=['POST'])
def api_v1_auth():
    """
    API 认证：用 PIN 码获取 Token

    请求体：
        { "pin": "123456" }

    响应：
        { "success": true, "token": "xxx", "expire_in": 86400 }
    """
    data = request.get_json() or {}
    pin = data.get('pin', '')

    if not pin:
        return jsonify({'success': False, 'error': '缺少 pin'}), 400

    # IP 限流检查
    ip = get_client_ip()
    if is_ip_locked(ip):
        return jsonify({'success': False, 'error': '尝试次数过多，请稍后再试'}), 429

    if not check_pin(pin):
        record_failed_attempt(ip)
        attempts_left = MAX_ATTEMPTS - load_login_attempts().get(ip, {}).get('count', 0)
        if attempts_left <= 0:
            return jsonify({'success': False, 'error': '尝试次数过多，请 5 分钟后再试'}), 429
        return jsonify({'success': False, 'error': f'PIN 错误，剩余尝试次数: {attempts_left}'}), 401

    # PIN 正确，生成 Token
    clear_failed_attempts(ip)
    token = generate_api_token(pin)
    return jsonify({
        'success': True,
        'token': token,
        'expire_in': TOKEN_EXPIRE_HOURS * 3600
    })

@app.route('/api/v1/upload', methods=['POST'])
def api_v1_upload():
    """
    程序化上传文件 API（需要 Token 认证）

    支持两种上传方式：
    1. multipart/form-data: 上传一个或多个文件，字段名 'file' 或 'files'
    2. JSON + base64: 单文件上传，适合小文件（<4.5MB）

    请求头：
        Authorization: Bearer <token>
        或 X-API-Token: <token>

    multipart/form-data 方式：
        file: 文件内容
        disposition: 'inline'（默认）或 'attachment'

    JSON base64 方式：
        {
            "filename": "test.txt",
            "content": "base64编码的内容",
            "content_type": "text/plain",
            "disposition": "inline"
        }

    响应格式：
        {
            "success": true,
            "data": {
                "files": [
                    {
                        "original_name": "test.txt",
                        "stored_name": "1234567890.txt",
                        "download_url": "/d/xxx",
                        "storage": "blob"
                    }
                ]
            }
        }
    """
    user, error = get_api_user()
    if error:
        return error

    disposition = request.form.get('disposition', 'inline') if request.form else 'inline'
    if disposition not in ('inline', 'attachment'):
        disposition = 'inline'

    # 方式一：JSON base64 上传
    if request.is_json:
        data = request.get_json()
        filename = data.get('filename', '')
        content_b64 = data.get('content', '')
        content_type = data.get('content_type', 'application/octet-stream')

        if not filename or not content_b64:
            return jsonify({'success': False, 'error': '缺少 filename 或 content'}), 400

        if '.' not in filename:
            return jsonify({'success': False, 'error': '文件名必须包含扩展名'}), 400

        ext = filename.rsplit('.', 1)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            return jsonify({
                'success': False,
                'error': f'不支持的文件类型 .{ext}，允许: {", ".join(sorted(ALLOWED_EXTENSIONS))}'
            }), 400

        try:
            file_data = base64.b64decode(content_b64)
        except Exception:
            return jsonify({'success': False, 'error': 'content 不是有效的 base64 编码'}), 400

        timestamp = str(int(time.time()))
        stored_name = f'{timestamp}.{ext}'

        try:
            if len(file_data) <= LARGE_FILE_THRESHOLD:
                if not BLOB_READ_WRITE_TOKEN:
                    return jsonify({'success': False, 'error': '存储服务未配置'}), 500
                blob_upload(stored_name, file_data, content_type)
                storage = 'blob'
            else:
                if not s3_client:
                    return jsonify({'success': False, 'error': '存储服务未配置'}), 500
                s3_client.put_object(
                    Bucket=R2_BUCKET_NAME,
                    Key=f'uploads/{stored_name}',
                    Body=file_data,
                    ContentType=content_type
                )
                storage = 'r2'

            token = encode_download_token(filename, stored_name, storage, disposition)
            return jsonify({
                'success': True,
                'data': {
                    'files': [{
                        'original_name': filename,
                        'stored_name': stored_name,
                        'download_url': f'/d/{token}',
                        'storage': storage
                    }]
                }
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    # 方式二：multipart/form-data 上传
    files = request.files.getlist('files') or ([request.files['file']] if 'file' in request.files else [])
    if not files or all(f.filename == '' for f in files):
        return jsonify({'success': False, 'error': '没有提供文件'}), 400

    uploaded = []
    errors = []

    for file in files:
        if file.filename == '':
            continue

        original_filename = file.filename
        if '.' not in original_filename:
            errors.append({'filename': original_filename, 'error': '文件名必须包含扩展名'})
            continue

        ext = original_filename.rsplit('.', 1)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            errors.append({'filename': original_filename, 'error': f'不支持的文件类型 .{ext}'})
            continue

        timestamp = str(int(time.time()))
        stored_name = f'{timestamp}.{ext}'
        file_data = file.read()
        content_type = file.content_type or 'application/octet-stream'

        try:
            if len(file_data) <= LARGE_FILE_THRESHOLD:
                if not BLOB_READ_WRITE_TOKEN:
                    errors.append({'filename': original_filename, 'error': '存储服务未配置'})
                    continue
                blob_upload(stored_name, file_data, content_type)
                storage = 'blob'
            else:
                if not s3_client:
                    errors.append({'filename': original_filename, 'error': '存储服务未配置'})
                    continue
                s3_client.put_object(
                    Bucket=R2_BUCKET_NAME,
                    Key=f'uploads/{stored_name}',
                    Body=file_data,
                    ContentType=content_type
                )
                storage = 'r2'

            token = encode_download_token(original_filename, stored_name, storage, disposition)
            uploaded.append({
                'original_name': original_filename,
                'stored_name': stored_name,
                'download_url': f'/d/{token}',
                'storage': storage
            })
        except Exception as e:
            errors.append({'filename': original_filename, 'error': str(e)})

    if errors and not uploaded:
        return jsonify({'success': False, 'error': '所有文件上传失败', 'details': errors}), 500

    return jsonify({
        'success': True,
        'data': {
            'files': uploaded,
            'errors': errors if errors else None
        }
    })

if __name__ == '__main__':
    app.run(debug=False, port=5000)

import os
import json
import secrets
import time
import base64
import hashlib
import boto3
import requests
from botocore.config import Config
from flask import Flask, request, render_template, Response, session, redirect, url_for, jsonify, send_file
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'default-secret-key-for-session')

# Token 配置
TOKEN_EXPIRE_HOURS = 24  # Token 有效期

ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'doc', 'docx', 'py', 'zip', 'mp4', 'mp3', 'ogg'}
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

# URL 映射存储路径前缀
URL_MAPPINGS_PREFIX = '.url_mappings/'

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

# Token 存储 - R2 存储
TOKENS_KEY = '.tokens/tokens.json'

# ========== 下载链接编码 ==========

def encode_download_token(original_name, stored_name, storage, disposition='inline', path=''):
    """将原始文件名+存储文件名+存储类型+下载方式+路径编码为 URL 安全的 token"""
    payload = f'{original_name}:{stored_name}:{storage}:{disposition}:{path}'
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip('=')

def decode_download_token(token):
    """解码 token，返回 (original_name, stored_name, storage, disposition, path) 或 None"""
    try:
        padding = 4 - len(token) % 4
        if padding != 4:
            token += '=' * padding
        payload = base64.urlsafe_b64decode(token.encode()).decode()
        parts = payload.rsplit(':', 4)
        if len(parts) == 5:
            return parts[0], parts[1], parts[2], parts[3], parts[4]
        if len(parts) == 4:
            return parts[0], parts[1], parts[2], parts[3], ''
        if len(parts) == 3:
            return parts[0], parts[1], parts[2], 'inline', ''
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

# ========== Token 管理（随机 Token + 文件存储） ==========

def load_tokens():
    """从 R2 加载 Token 存储，并清理过期 Token"""
    if not s3_client:
        return {}
    try:
        response = s3_client.get_object(Bucket=R2_BUCKET_NAME, Key=TOKENS_KEY)
        data = json.loads(response['Body'].read().decode('utf-8'))
        # 清理过期 Token
        current_time = time.time()
        cleaned = {k: v for k, v in data.items()
                   if v.get('expire', 0) > current_time}
        # 如果有过期 Token 被清理，保存更新
        if len(cleaned) != len(data):
            save_tokens(cleaned)
        return cleaned
    except s3_client.exceptions.NoSuchKey:
        return {}
    except Exception as e:
        print(f"加载 Token 失败: {e}")
        return {}

def save_tokens(tokens):
    """保存 Token 存储到 R2"""
    if not s3_client:
        return
    try:
        s3_client.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=TOKENS_KEY,
            Body=json.dumps(tokens).encode('utf-8'),
            ContentType='application/json'
        )
    except Exception as e:
        print(f"保存 Token 失败: {e}")

def generate_token(user_type, expire_hours):
    """生成随机 Token"""
    token = secrets.token_urlsafe(32)  # 43 个随机字符
    tokens = load_tokens()
    tokens[token] = {
        'user_type': user_type,
        'expire': time.time() + expire_hours * 3600
    }
    save_tokens(tokens)
    return token

def verify_token(token, allowed_types=('api_user',)):
    """验证 Token"""
    tokens = load_tokens()
    if token not in tokens:
        return False
    token_data = tokens[token]
    # 检查类型
    if token_data.get('user_type') not in allowed_types:
        return False
    # 检查过期
    if token_data.get('expire', 0) < time.time():
        return False
    return True

def generate_api_token(pin):
    """生成 API Token（随机 Token，24 小时有效）"""
    return generate_token('api_user', TOKEN_EXPIRE_HOURS)

def generate_web_presign_token():
    """网页端大文件上传用的短期 Token（1 小时有效）"""
    return generate_token('presign_web', 1)

def verify_api_token(token, allowed_users=('api_user',)):
    """验证 API Token（兼容旧接口）"""
    return verify_token(token, allowed_users)

def _extract_bearer_token():
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:]
    return request.headers.get('X-API-Token')

def get_api_user():
    """从请求头获取并验证 API 用户，返回 (user_id, error_response)"""
    token = _extract_bearer_token()
    if not token:
        return None, (jsonify({'success': False, 'error': '未提供 Token'}), 401)
    if not verify_api_token(token, allowed_users=('api_user',)):
        return None, (jsonify({'success': False, 'error': 'Token 无效或已过期'}), 401)
    return 'api_user', None

def require_presign_auth():
    """预签名接口认证：API Token 或网页短期 Token"""
    token = _extract_bearer_token()
    if not token:
        return None, (jsonify({'success': False, 'error': '未提供 Token'}), 401)
    if not verify_api_token(token, allowed_users=('api_user', 'presign_web')):
        return None, (jsonify({'success': False, 'error': 'Token 无效或已过期'}), 401)
    return 'presign_user', None

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def is_path_safe(path):
    """验证路径安全性，防止路径遍历攻击"""
    if not path:
        return True
    # 检查路径遍历攻击
    if '..' in path or path.startswith('\\'):
        return False
    # 允许 /v/ 开头的固定路径
    if path.startswith('/v/'):
        # 只允许 /v/ + 文件名（字母、数字、下划线、连字符、点）
        import re
        return bool(re.match(r'^/v/[a-zA-Z0-9_\-\.]+$', path))
    # 普通路径不能以 / 开头
    if path.startswith('/'):
        return False
    # 检查路径长度
    if len(path) > 255:
        return False
    # 检查路径字符（只允许字母、数字、下划线、连字符、斜杠、点）
    import re
    if not re.match(r'^[a-zA-Z0-9_\-/\.]+$', path):
        return False
    return True

def get_r2_url(key):
    """获取 R2 文件的公共 URL"""
    if R2_PUBLIC_URL:
        return f'{R2_PUBLIC_URL}/{key}'
    return f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com/{R2_BUCKET_NAME}/{key}'

# ========== URL 映射操作 ==========

def save_url_mapping(name, target_url):
    """保存 URL 映射到 R2"""
    if not s3_client:
        raise Exception('R2 未配置')
    
    mapping_key = f'{URL_MAPPINGS_PREFIX}{name}.json'
    mapping_data = json.dumps({
        'url': target_url,
        'created_at': time.time()
    })
    
    s3_client.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=mapping_key,
        Body=mapping_data.encode(),
        ContentType='application/json'
    )

def get_url_mapping(name):
    """获取 URL 映射，返回目标 URL 或 None"""
    if not s3_client:
        return None
    
    mapping_key = f'{URL_MAPPINGS_PREFIX}{name}.json'
    
    try:
        response = s3_client.get_object(
            Bucket=R2_BUCKET_NAME,
            Key=mapping_key
        )
        data = json.loads(response['Body'].read().decode())
        return data.get('url')
    except Exception:
        return None

def validate_url(url):
    """验证 URL 是否可访问"""
    try:
        resp = requests.get(url, timeout=10, allow_redirects=True)
        return resp.status_code == 200
    except Exception:
        return False

# ========== Vercel Blob 操作 ==========

def blob_upload(pathname, file_data, content_type):
    """上传文件到 Vercel Blob"""
    if not BLOB_READ_WRITE_TOKEN:
        raise Exception('BLOB_READ_WRITE_TOKEN 未配置')

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

def blob_list(prefix='uploads/'):
    """列出 Vercel Blob 中的所有文件"""
    if not BLOB_READ_WRITE_TOKEN:
        return []

    headers = {
        'Authorization': f'Bearer {BLOB_READ_WRITE_TOKEN}',
    }

    response = requests.get(
        f'https://blob.vercel-storage.com/?prefix={prefix}',
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
    """生成预签名上传 URL，客户端用此 URL 直传到 R2（需要 Token）"""
    _, error = require_presign_auth()
    if error:
        return error

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

    # 获取自定义路径参数
    custom_path = data.get('path', '').strip()
    if custom_path and not is_path_safe(custom_path):
        return jsonify({'error': '无效的路径'}), 400

    # 检查是否是固定路径 (/v/{filename})
    is_fixed_path = custom_path and custom_path.startswith('/v/')
    if is_fixed_path:
        fixed_filename = custom_path[3:]
        key = f'v/{fixed_filename}'  # R2 key 不以 / 开头
    else:
        if custom_path:
            key = f'{custom_path}/{unique_name}'
        else:
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

        if is_fixed_path:
            download_url = f'/v/{fixed_filename}'
        else:
            token = encode_download_token(filename, unique_name, 'r2', disposition, custom_path)
            download_url = f'/d/{token}'

        return jsonify({
            'presignedUrl': presigned_url,
            'downloadUrl': download_url,
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

    # 获取自定义路径参数
    custom_path = request.form.get('path', '').strip()
    if custom_path and not is_path_safe(custom_path):
        return jsonify({'success': False, 'error': '无效的路径'}), 400

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

        # 检查是否是固定路径 (/v/{filename})
        is_fixed_path = custom_path and custom_path.startswith('/v/')
        if is_fixed_path:
            # 固定路径：使用原始文件名
            fixed_filename = custom_path[3:]  # 去掉 '/v/' 前缀
            storage_path = f'/v/{fixed_filename}'
            r2_key = f'v/{fixed_filename}'  # R2 key 不以 / 开头
        else:
            # 普通路径：使用时间戳文件名
            if custom_path:
                storage_path = f'{custom_path}/{filename}'
            else:
                storage_path = f'uploads/{filename}'
            r2_key = storage_path

        try:
            if len(file_data) <= LARGE_FILE_THRESHOLD:
                # 小文件上传到 Vercel Blob
                if not BLOB_READ_WRITE_TOKEN:
                    errors.append(f'{original_filename}: BLOB_READ_WRITE_TOKEN 未配置')
                    continue
                result = blob_upload(storage_path, file_data, content_type)
                result_url = result.get('url')
                storage = 'blob'
            else:
                # 大文件上传到 R2
                if not s3_client:
                    errors.append(f'{original_filename}: R2 未配置')
                    continue
                s3_client.put_object(
                    Bucket=R2_BUCKET_NAME,
                    Key=r2_key,
                    Body=file_data,
                    ContentType=content_type
                )
                result_url = get_r2_url(storage_path)
                storage = 'r2'

            if is_fixed_path:
                # 固定路径：下载链接直接是 /v/{filename}
                download_url = f'/v/{fixed_filename}'
            else:
                # 普通路径：下载链接是 /d/{token}
                token = encode_download_token(original_filename, filename, storage, disposition, custom_path)
                download_url = f'/d/{token}'

            uploaded.append({'name': original_filename, 'download_url': download_url, 'storage': storage, 'path': custom_path})
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
    
    return render_template('index.html', upload_token=generate_web_presign_token())

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

    # 从 Vercel Blob 获取文件（列出所有文件）
    try:
        blob_files = blob_list('')
        for blob in blob_files:
            pathname = blob.get('pathname', '')
            # 跳过非文件（如目录）
            if not pathname or pathname.endswith('/'):
                continue
            
            # 解析路径和文件名
            if '/' in pathname:
                path_parts = pathname.rsplit('/', 1)
                if len(path_parts) == 2:
                    path, filename = path_parts
                else:
                    path, filename = '', pathname
            else:
                path, filename = '', pathname
            
            token = encode_download_token(filename, filename, 'blob', 'inline', path)
            files.append({
                'name': filename,
                'url': blob.get('url'),
                'size': blob.get('size', 0),
                'uploaded_at': blob.get('uploadedAt', ''),
                'storage': 'blob',
                'token': token,
                'disposition': 'inline',
                'path': path
            })
    except Exception as e:
        pass

    # 从 R2 获取文件（列出所有文件）
    if s3_client:
        try:
            continuation_token = None
            while True:
                list_kwargs = {
                    'Bucket': R2_BUCKET_NAME,
                    'MaxKeys': 1000
                }
                if continuation_token:
                    list_kwargs['ContinuationToken'] = continuation_token

                response = s3_client.list_objects_v2(**list_kwargs)

                for obj in response.get('Contents', []):
                    key = obj['Key']
                    # 跳过目录
                    if key.endswith('/'):
                        continue
                    
                    # 解析路径和文件名
                    if '/' in key:
                        path_parts = key.rsplit('/', 1)
                        if len(path_parts) == 2:
                            path, filename = path_parts
                        else:
                            path, filename = '', key
                    else:
                        path, filename = '', key
                    
                    files.append({
                        'name': filename,
                        'url': get_r2_url(key),
                        'size': obj['Size'],
                        'uploaded_at': obj['LastModified'].isoformat(),
                        'storage': 'r2',
                        'token': encode_download_token(filename, filename, 'r2', 'inline', path),
                        'disposition': 'inline',
                        'path': path
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
    original_name, stored_name, storage, disposition, path = result

    # 构建存储路径
    if path:
        storage_path = f'{path}/{stored_name}'
    else:
        storage_path = f'uploads/{stored_name}'

    if storage == 'blob':
        # 对于自定义路径，需要列出对应前缀的文件
        prefix = f'{path}/' if path else 'uploads/'
        blob_files = blob_list(prefix)
        for blob in blob_files:
            if blob.get('pathname') == storage_path:
                resp = requests.get(blob.get('url', ''), timeout=60)
                response = Response(resp.content, content_type=resp.headers.get('Content-Type', 'application/octet-stream'))
                response.headers['Content-Disposition'] = f'{disposition}; filename="{original_name}"'
                return response
        return '文件不存在', 404
    else:
        url = get_r2_url(storage_path)
        resp = requests.get(url, timeout=60)
        response = Response(resp.content, content_type=resp.headers.get('Content-Type', 'application/octet-stream'))
        response.headers['Content-Disposition'] = f'{disposition}; filename="{original_name}"'
        return response

@app.route('/v/<filename>')
def download_fixed_path(filename):
    """固定路径下载：/v/{filename} 直接返回文件或重定向到 URL"""
    # 安全检查：只允许字母、数字、下划线、连字符、点
    import re
    if not re.match(r'^[a-zA-Z0-9_\-\.]+$', filename):
        return '无效的文件名', 400

    # 优先检查 URL 映射（URL重定向功能）
    target_url = get_url_mapping(filename)
    if target_url:
        return redirect(target_url)

    # R2 key 不以 / 开头，直接使用 filename
    r2_key = f'v/{filename}'

    # 优先尝试从 R2 获取（因为 R2 key 是 v/{filename} 格式，与用户访问的 /v/{filename} 对应）
    if s3_client:
        try:
            url = get_r2_url(r2_key)
            print(f"DEBUG: 尝试从R2获取文件: {r2_key}, URL: {url}")
            resp = requests.get(url, timeout=10)  # 短超时测试连通性
            print(f"DEBUG: R2响应状态码: {resp.status_code}")
            if resp.status_code == 200:
                # 直接重定向到R2 URL，避免代理大文件
                return redirect(url)
            elif resp.status_code == 403 and R2_PUBLIC_URL:
                # 如果403且有公共URL，直接重定向到公共URL
                public_url = f'{R2_PUBLIC_URL}/{r2_key}'
                print(f"DEBUG: 使用公共URL重定向: {public_url}")
                return redirect(public_url)
        except Exception as e:
            print(f"DEBUG: 从R2获取文件失败: {e}")
            pass

    # 如果 R2 没有，再尝试从 Vercel Blob 获取
    if BLOB_READ_WRITE_TOKEN:
        storage_path = f'/v/{filename}'
        print(f"DEBUG: 尝试从Blob获取文件: {storage_path}")
        blob_files = blob_list('/v/')
        for blob in blob_files:
            print(f"DEBUG: Blob文件列表项: {blob.get('pathname')}")
            if blob.get('pathname') == storage_path:
                # 直接重定向到Blob URL，避免代理大文件
                blob_url = blob.get('url', '')
                print(f"DEBUG: 使用Blob URL重定向: {blob_url}")
                return redirect(blob_url)

    print(f"DEBUG: 文件不存在 - filename: {filename}, r2_key: {r2_key}")
    return '文件不存在', 404

@app.route('/api/disposition', methods=['POST'])
def toggle_disposition():
    """切换文件的预览/下载模式，返回新的 token"""
    data = request.get_json() or {}
    token = data.get('token', '')
    result = decode_download_token(token)
    if not result:
        return jsonify({'error': '无效的 token'}), 400
    original_name, stored_name, storage, disposition, path = result
    new_disposition = 'attachment' if disposition == 'inline' else 'inline'
    new_token = encode_download_token(original_name, stored_name, storage, new_disposition, path)
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
        path: 自定义存储路径（可选）
            - 普通路径：'documents/2024' → 下载链接 /d/{token}
            - 固定路径：'/v/report.pdf' → 下载链接 /v/report.pdf

    JSON base64 方式：
        {
            "filename": "test.txt",
            "content": "base64编码的内容",
            "content_type": "text/plain",
            "disposition": "inline",
            "path": "/v/report.pdf"
        }

    响应格式：
        {
            "success": true,
            "data": {
                "files": [
                    {
                        "original_name": "test.txt",
                        "stored_name": "1234567890.txt",
                        "download_url": "/v/report.pdf",
                        "storage": "blob",
                        "path": "/v/report.pdf"
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
        custom_path = data.get('path', '').strip()
        upload_type = data.get('type', 'file')  # 新增：type 参数，默认为 file

        # URL 类型处理
        if upload_type == 'url':
            # URL 类型必须使用固定路径
            if not custom_path or not custom_path.startswith('/v/'):
                return jsonify({'success': False, 'error': 'URL 类型必须使用固定路径（/v/开头）'}), 400
            
            # 解码 URL
            try:
                target_url = base64.b64decode(content_b64).decode()
            except Exception:
                return jsonify({'success': False, 'error': 'content 不是有效的 base64 编码'}), 400
            
            # 验证 URL 可访问性
            if not validate_url(target_url):
                return jsonify({'success': False, 'error': 'URL 无法访问'}), 400
            
            # 提取名称（去掉 /v/ 前缀）
            name = custom_path[3:]
            
            # 保存 URL 映射
            try:
                save_url_mapping(name, target_url)
            except Exception as e:
                return jsonify({'success': False, 'error': str(e)}), 500
            
            # 返回无扩展名的下载链接
            return jsonify({
                'success': True,
                'data': {
                    'files': [{
                        'original_name': filename,
                        'stored_name': name,
                        'download_url': f'/v/{name}',
                        'storage': 'url_mapping',
                        'path': custom_path,
                        'type': 'url'
                    }]
                }
            })

        # 文件类型处理（原有逻辑）
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

        if custom_path and not is_path_safe(custom_path):
            return jsonify({'success': False, 'error': '无效的路径'}), 400

        try:
            file_data = base64.b64decode(content_b64)
        except Exception:
            return jsonify({'success': False, 'error': 'content 不是有效的 base64 编码'}), 400

        timestamp = str(int(time.time()))
        stored_name = f'{timestamp}.{ext}'

        # 检查是否是固定路径 (/v/{filename})
        is_fixed_path = custom_path and custom_path.startswith('/v/')
        if is_fixed_path:
            # 固定路径：使用原始文件名
            fixed_filename = custom_path[3:]  # 去掉 '/v/' 前缀
            storage_path = f'/v/{fixed_filename}'
            r2_key = f'v/{fixed_filename}'  # R2 key 不以 / 开头
        else:
            # 普通路径：使用时间戳文件名
            if custom_path:
                storage_path = f'{custom_path}/{stored_name}'
            else:
                storage_path = f'uploads/{stored_name}'
            r2_key = storage_path

        try:
            if len(file_data) <= LARGE_FILE_THRESHOLD:
                if not BLOB_READ_WRITE_TOKEN:
                    return jsonify({'success': False, 'error': '存储服务未配置'}), 500
                blob_upload(storage_path, file_data, content_type)
                storage = 'blob'
            else:
                if not s3_client:
                    return jsonify({'success': False, 'error': '存储服务未配置'}), 500
                s3_client.put_object(
                    Bucket=R2_BUCKET_NAME,
                    Key=r2_key,
                    Body=file_data,
                    ContentType=content_type
                )
                storage = 'r2'

            if is_fixed_path:
                # 固定路径：下载链接直接是 /v/{filename}
                download_url = f'/v/{fixed_filename}'
            else:
                # 普通路径：下载链接是 /d/{token}
                token = encode_download_token(filename, stored_name, storage, disposition, custom_path)
                download_url = f'/d/{token}'

            return jsonify({
                'success': True,
                'data': {
                    'files': [{
                        'original_name': filename,
                        'stored_name': stored_name,
                        'download_url': download_url,
                        'storage': storage,
                        'path': custom_path
                    }]
                }
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    # 方式二：multipart/form-data 上传
    files = request.files.getlist('files') or ([request.files['file']] if 'file' in request.files else [])
    if not files or all(f.filename == '' for f in files):
        return jsonify({'success': False, 'error': '没有提供文件'}), 400

    # 获取自定义路径参数
    custom_path = request.form.get('path', '').strip()
    if custom_path and not is_path_safe(custom_path):
        return jsonify({'success': False, 'error': '无效的路径'}), 400
    
    # 获取 type 参数
    upload_type = request.form.get('type', 'file')
    
    # URL 类型处理
    if upload_type == 'url':
        # URL 类型必须使用固定路径
        if not custom_path or not custom_path.startswith('/v/'):
            return jsonify({'success': False, 'error': 'URL 类型必须使用固定路径（/v/开头）'}), 400
        
        # 读取文件内容作为 URL
        file = files[0]
        target_url = file.read().decode().strip()
        
        # 验证 URL 可访问性
        if not validate_url(target_url):
            return jsonify({'success': False, 'error': 'URL 无法访问'}), 400
        
        # 提取名称（去掉 /v/ 前缀）
        name = custom_path[3:]
        
        # 保存 URL 映射
        try:
            save_url_mapping(name, target_url)
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500
        
        # 返回无扩展名的下载链接
        return jsonify({
            'success': True,
            'data': {
                'files': [{
                    'original_name': file.filename,
                    'stored_name': name,
                    'download_url': f'/v/{name}',
                    'storage': 'url_mapping',
                    'path': custom_path,
                    'type': 'url'
                }]
            }
        })

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

        # 检查是否是固定路径 (/v/{filename})
        is_fixed_path = custom_path and custom_path.startswith('/v/')
        if is_fixed_path:
            # 固定路径：使用原始文件名
            fixed_filename = custom_path[3:]  # 去掉 '/v/' 前缀
            storage_path = f'/v/{fixed_filename}'
            r2_key = f'v/{fixed_filename}'  # R2 key 不以 / 开头
        else:
            # 普通路径：使用时间戳文件名
            if custom_path:
                storage_path = f'{custom_path}/{stored_name}'
            else:
                storage_path = f'uploads/{stored_name}'
            r2_key = storage_path

        try:
            if len(file_data) <= LARGE_FILE_THRESHOLD:
                if not BLOB_READ_WRITE_TOKEN:
                    errors.append({'filename': original_filename, 'error': '存储服务未配置'})
                    continue
                blob_upload(storage_path, file_data, content_type)
                storage = 'blob'
            else:
                if not s3_client:
                    errors.append({'filename': original_filename, 'error': '存储服务未配置'})
                    continue
                s3_client.put_object(
                    Bucket=R2_BUCKET_NAME,
                    Key=r2_key,
                    Body=file_data,
                    ContentType=content_type
                )
                storage = 'r2'

            if is_fixed_path:
                # 固定路径：下载链接直接是 /v/{filename}
                download_url = f'/v/{fixed_filename}'
            else:
                # 普通路径：下载链接是 /d/{token}
                token = encode_download_token(original_filename, stored_name, storage, disposition, custom_path)
                download_url = f'/d/{token}'

            uploaded.append({
                'original_name': original_filename,
                'stored_name': stored_name,
                'download_url': download_url,
                'storage': storage,
                'path': custom_path
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

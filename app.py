import os
import json
import hashlib
import time
import boto3
from botocore.config import Config
from flask import Flask, request, render_template, Response, session, redirect, url_for, jsonify
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', os.urandom(32).hex())

ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'doc', 'docx', 'py', 'zip', 'mp4', 'mp3'}
PIN_HASH = os.getenv('PIN_HASH')

# Cloudflare R2 配置
R2_ACCOUNT_ID = os.getenv('R2_ACCOUNT_ID')
R2_ACCESS_KEY_ID = os.getenv('R2_ACCESS_KEY_ID')
R2_SECRET_KEY = os.getenv('R2_SECRET_KEY')
R2_BUCKET_NAME = os.getenv('R2_BUCKET_NAME', 'my-uploads')
R2_PUBLIC_URL = os.getenv('R2_PUBLIC_URL')

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

# 登录限制
login_attempts = {}
MAX_ATTEMPTS = 5
LOCKOUT_TIME = 300

def get_client_ip():
    if request.headers.getlist("X-Forwarded-For"):
        return request.headers.getlist("X-Forwarded-For")[0]
    return request.remote_addr

def is_ip_locked(ip):
    if ip in login_attempts:
        attempts = login_attempts[ip]
        if attempts['count'] >= MAX_ATTEMPTS:
            if time.time() - attempts['last_attempt'] < LOCKOUT_TIME:
                return True
            else:
                login_attempts[ip] = {'count': 0, 'last_attempt': 0}
    return False

def record_failed_attempt(ip):
    if ip not in login_attempts:
        login_attempts[ip] = {'count': 0, 'last_attempt': 0}
    login_attempts[ip]['count'] += 1
    login_attempts[ip]['last_attempt'] = time.time()

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
        
        return jsonify({
            'presignedUrl': presigned_url,
            'publicUrl': get_r2_url(key),
            'key': key,
            'filename': unique_name
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
        remaining_time = int(LOCKOUT_TIME - (time.time() - login_attempts[ip]['last_attempt']))
        return render_template('pin_verify.html',
                             error=f'登录尝试次数过多，请 {remaining_time} 秒后再试',
                             locked=True)

    if request.method == 'POST':
        pin = request.form.get('pin', '')

        if check_pin(pin):
            if ip in login_attempts:
                del login_attempts[ip]

            session['authenticated'] = True
            session['auth_time'] = time.time()

            return redirect(url_for('teacher'))
        else:
            record_failed_attempt(ip)
            attempts_left = MAX_ATTEMPTS - login_attempts[ip]['count']

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
    
    if not s3_client:
        return 'R2 未配置，请联系管理员', 500
    
    try:
        response = s3_client.list_objects_v2(
            Bucket=R2_BUCKET_NAME,
            Prefix='uploads/'
        )
        
        files = []
        for obj in response.get('Contents', []):
            key = obj['Key']
            filename = key.replace('uploads/', '', 1)
            
            files.append({
                'name': filename,
                'url': get_r2_url(key),
                'size': obj['Size'],
                'uploaded_at': obj['LastModified'].isoformat()
            })
        
        files.sort(key=lambda x: x['uploaded_at'], reverse=True)
        return render_template('teacher.html', files=files)
        
    except Exception as e:
        return f'获取文件列表失败: {str(e)}'

@app.route('/teacher/logout')
def logout():
    session.clear()
    return redirect(url_for('verify_pin'))

@app.route('/d/<path:pathname>')
def download_file(pathname):
    if not pathname.startswith('uploads/'):
        return '无效的文件路径', 400
    
    return redirect(get_r2_url(pathname))

if __name__ == '__main__':
    app.run(debug=False, port=5000)

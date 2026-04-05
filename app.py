import os
import json
import requests
from flask import Flask, request, render_template, Response
from werkzeug.utils import secure_filename

app = Flask(__name__)

# 配置
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'doc', 'docx'}
BLOB_TOKEN = os.getenv('BLOB_READ_WRITE_TOKEN')

# Vercel Blob Storage API 端点
BLOB_API_URL = "https://blob.vercel-storage.com"

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
                    'Content-Type': file.content_type
                }
                
                response = requests.put(
                    f'{BLOB_API_URL}/uploads/{filename}',
                    data=file_content,
                    headers=headers
                )
                
                if response.status_code == 200:
                    return '文件上传成功！<br><a href="/">继续上传</a>'
                else:
                    return f'上传失败: {response.text}'
                    
            except Exception as e:
                return f'上传失败: {str(e)}'
            
    return render_template('index.html')

# --- 路由：老师 (查看) ---
@app.route('/teacher')
def teacher():
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
            files = [blob['name'].replace('uploads/', '') for blob in data.get('blobs', [])]
            return render_template('teacher.html', files=files)
        else:
            return f'获取文件列表失败: {response.text}'
            
    except Exception as e:
        return f'获取文件列表失败: {str(e)}'

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
        
        if response.status_code == 200:
            file_data = response.content
            file_response = Response(file_data)
            file_response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
            file_response.headers['Content-Type'] = response.headers.get('Content-Type', 'application/octet-stream')
            return file_response
        else:
            return f'文件不存在: {response.text}'
            
    except Exception as e:
        return f'文件不存在: {str(e)}'

# Vercel Serverless Function 入口点
if __name__ == '__main__':
    app.run(debug=False, port=5000)

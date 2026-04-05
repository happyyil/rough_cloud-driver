import os
from flask import Flask, request, render_template, redirect, url_for, send_from_directory, Response
from werkzeug.utils import secure_filename
from waitress import serve
from dotenv import load_dotenv
import vercel.storage as storage

load_dotenv()

app = Flask(__name__)

# 配置子路径（可选，根据 Vercel 配置调整）
app.config['APPLICATION_ROOT'] = '/clouddriver'

# 配置
ALLOWED_EXTENSIONS = {'txt', 'pdf', 'png', 'jpg', 'jpeg', 'gif', 'doc', 'docx'}
BLOB_TOKEN = os.getenv('BLOB_READ_WRITE_TOKEN')

# 检查文件后缀是否允许
def allowed_file(filename):
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- 路由：普通用户 (上传) ---
@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        # 检查是否有文件部分
        if 'file' not in request.files:
            return '没有文件部分'
        
        file = request.files['file']
        
        if file.filename == '':
            return '没有选择文件'
        
        if file and allowed_file(file.filename):
            # 安全文件名处理
            filename = secure_filename(file.filename)
            
            # 读取文件内容
            file_content = file.read()
            
            # 上传到 Blob Storage
            try:
                storage.put(f"uploads/{filename}", file_content, token=BLOB_TOKEN)
                return '文件上传成功！<br><a href="/">继续上传</a>'
            except Exception as e:
                return f'上传失败: {str(e)}'
            
    return render_template('index.html')

# --- 路由：老师 (查看) ---
@app.route('/teacher')
def teacher():
    try:
        # 获取 Blob Storage 中的所有文件
        blobs = storage.list("uploads", token=BLOB_TOKEN)
        # 只返回文件名，不包含路径
        files = [blob.url.split('/')[-1] if '/' in blob.url else blob.name for blob in blobs]
        return render_template('teacher.html', files=files)
    except Exception as e:
        return f'获取文件列表失败: {str(e)}'

# --- 路由：下载/预览文件 ---
@app.route('/uploads/<filename>')
def uploaded_file(filename):
    try:
        # 从 Blob Storage 获取文件
        file_data = storage.get(f"uploads/{filename}", token=BLOB_TOKEN)
        # 创建响应
        response = Response(file_data)
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    except Exception as e:
        return f'文件不存在: {str(e)}'

if __name__ == '__main__':
    print("生产环境服务器已启动...")
    serve(app, host='0.0.0.0', port=5000)

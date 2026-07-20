"""
程序化上传 API 使用示例
"""
import requests
import base64
import os

# 配置
API_BASE = "https://your-app.vercel.app"  # 替换为你的应用地址
API_KEY = os.getenv("API_KEY")  # 从环境变量获取 API Key

headers = {
    "Authorization": f"Bearer {API_KEY}"
}

# ========== 方式一：multipart/form-data 上传 ==========

def upload_file_multipart(file_path, disposition="inline"):
    """使用 multipart/form-data 上传文件"""
    with open(file_path, "rb") as f:
        files = {"files": (os.path.basename(file_path), f)}
        data = {"disposition": disposition}
        resp = requests.post(
            f"{API_BASE}/api/v1/upload",
            headers=headers,
            files=files,
            data=data
        )
    return resp.json()


# ========== 方式二：JSON base64 上传 ==========

def upload_file_base64(file_path, disposition="inline"):
    """使用 JSON base64 上传文件（适合小文件）"""
    with open(file_path, "rb") as f:
        content = base64.b64encode(f.read()).decode()

    data = {
        "filename": os.path.basename(file_path),
        "content": content,
        "content_type": "application/octet-stream",
        "disposition": disposition
    }
    resp = requests.post(
        f"{API_BASE}/api/v1/upload",
        headers=headers,
        json=data
    )
    return resp.json()


# ========== 示例 ==========

if __name__ == "__main__":
    # 测试上传
    test_file = "test.txt"

    # 创建测试文件
    with open(test_file, "w") as f:
        f.write("Hello, API!")

    # 方式一：multipart 上传
    print("=== multipart/form-data 上传 ===")
    result = upload_file_multipart(test_file)
    print(result)

    # 方式二：base64 上传
    print("\n=== JSON base64 上传 ===")
    result = upload_file_base64(test_file)
    print(result)

    # 清理
    os.remove(test_file)

"""
程序化上传 API 使用示例

流程：
1. 用 PIN 码调用 /api/v1/auth 获取 Token
2. 用 Token 调用 /api/v1/upload 上传文件（小文件 < 4.5MB）
   或调用 /api/r2/presign 获取预签名 URL 直传 R2（大文件 >= 4.5MB）
"""
import requests
import base64
import os

# 配置
API_BASE = "https://your-app.vercel.app"  # 替换为你的应用地址
PIN_CODE = "123456"  # 替换为你的 PIN 码


def get_token(pin):
    """用 PIN 码获取 Token"""
    resp = requests.post(
        f"{API_BASE}/api/v1/auth",
        json={"pin": pin}
    )
    data = resp.json()
    if data.get("success"):
        print(f"获取 Token 成功，有效期 {data['expire_in'] // 3600} 小时")
        return data["token"]
    else:
        print(f"认证失败: {data.get('error')}")
        return None


def upload_file_multipart(token, file_path, disposition="inline", path=""):
    """使用 multipart/form-data 上传文件（小文件 < 4.5MB）"""
    headers = {"Authorization": f"Bearer {token}"}
    with open(file_path, "rb") as f:
        files = {"files": (os.path.basename(file_path), f)}
        data = {"disposition": disposition, "path": path}
        resp = requests.post(
            f"{API_BASE}/api/v1/upload",
            headers=headers,
            files=files,
            data=data
        )
    return resp.json()


def upload_file_base64(token, file_path, disposition="inline", path=""):
    """使用 JSON base64 上传文件（适合小文件）"""
    headers = {"Authorization": f"Bearer {token}"}
    with open(file_path, "rb") as f:
        content = base64.b64encode(f.read()).decode()

    data = {
        "filename": os.path.basename(file_path),
        "content": content,
        "content_type": "application/octet-stream",
        "disposition": disposition,
        "path": path
    }
    resp = requests.post(
        f"{API_BASE}/api/v1/upload",
        headers=headers,
        json=data
    )
    return resp.json()


def upload_large_file(token, file_path, disposition="inline", path=""):
    """使用预签名 URL 上传大文件（>= 4.5MB）"""
    headers = {"Authorization": f"Bearer {token}"}
    filename = os.path.basename(file_path)

    # 获取文件 MIME 类型（须与 PUT 时 Content-Type 一致）
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    mime_types = {
        "ogg": "audio/ogg",
        "mp3": "audio/mpeg",
        "mp4": "video/mp4",
        "pdf": "application/pdf",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
    }
    content_type = mime_types.get(ext, "application/octet-stream")

    # Step 1: 获取预签名 URL
    presign_data = {
        "filename": filename,
        "contentType": content_type,
        "disposition": disposition,
        "path": path
    }
    resp = requests.post(
        f"{API_BASE}/api/r2/presign",
        headers=headers,
        json=presign_data
    )
    presign_result = resp.json()

    if "error" in presign_result:
        return {"success": False, "error": presign_result["error"]}

    presigned_url = presign_result["presignedUrl"]
    download_url = presign_result["downloadUrl"]

    # Step 2: 直传到 R2
    with open(file_path, "rb") as f:
        file_data = f.read()

    resp = requests.put(
        presigned_url,
        headers={"Content-Type": content_type},
        data=file_data
    )

    if resp.status_code in (200, 201):
        return {
            "success": True,
            "download_url": download_url,
            "full_url": f"{API_BASE}{download_url}"
        }
    else:
        return {"success": False, "error": f"上传失败: {resp.status_code}"}


if __name__ == "__main__":
    # 1. 获取 Token
    token = get_token(PIN_CODE)
    if not token:
        exit(1)

    # 2. 创建测试文件
    test_file = "test.txt"
    with open(test_file, "w") as f:
        f.write("Hello, API!")

    # 3. 方式一：multipart 上传（带自定义路径）
    print("\n=== multipart/form-data 上传 ===")
    result = upload_file_multipart(token, test_file, path="examples/test")
    print(result)

    # 4. 方式二：base64 上传
    print("\n=== JSON base64 上传 ===")
    result = upload_file_base64(token, test_file)
    print(result)

    # 5. 方式三：大文件预签名上传（带固定路径）
    print("\n=== 预签名 URL 上传（大文件） ===")
    # 使用测试文件模拟大文件上传
    result = upload_large_file(token, test_file, path="/v/example.txt")
    print(result)

    # 6. 清理
    os.remove(test_file)

    # 7. 使用示例：上传任意路径的大文件
    print("\n=== 使用示例 ===")
    print("""
# 上传任意路径的大文件：
result = upload_large_file(
    token,
    r"D:\\Downloads\\minecraft\\sounds\\credits.ogg",
    disposition="inline",
    path="/v/credits.ogg"
)
print(f"下载链接: {result['full_url']}")
""")

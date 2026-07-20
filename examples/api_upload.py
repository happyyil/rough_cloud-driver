"""
程序化上传 API 使用示例

流程：
1. 用 PIN 码调用 /api/v1/auth 获取 Token
2. 用 Token 调用 /api/v1/upload 上传文件
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


def upload_file_multipart(token, file_path, disposition="inline"):
    """使用 multipart/form-data 上传文件"""
    headers = {"Authorization": f"Bearer {token}"}
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


def upload_file_base64(token, file_path, disposition="inline"):
    """使用 JSON base64 上传文件（适合小文件）"""
    headers = {"Authorization": f"Bearer {token}"}
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


if __name__ == "__main__":
    # 1. 获取 Token
    token = get_token(PIN_CODE)
    if not token:
        exit(1)

    # 2. 创建测试文件
    test_file = "test.txt"
    with open(test_file, "w") as f:
        f.write("Hello, API!")

    # 3. 方式一：multipart 上传
    print("\n=== multipart/form-data 上传 ===")
    result = upload_file_multipart(token, test_file)
    print(result)

    # 4. 方式二：base64 上传
    print("\n=== JSON base64 上传 ===")
    result = upload_file_base64(token, test_file)
    print(result)

    # 5. 清理
    os.remove(test_file)

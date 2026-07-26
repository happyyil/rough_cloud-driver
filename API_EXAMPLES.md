# 上传 API 使用示例

## 认证

首先获取 API Token：

```bash
curl -X POST https://your-domain.com/api/v1/auth \
  -H "Content-Type: application/json" \
  -d '{"pin": "123456"}'
```

响应：
```json
{
  "success": true,
  "token": "xxx",
  "expire_in": 86400
}
```

---

## 上传文件（普通路径）

### 方式一：multipart/form-data

```bash
curl -X POST https://your-domain.com/api/v1/upload \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "files=@report.pdf" \
  -F "path=documents/2024/Q3"
```

响应：
```json
{
  "success": true,
  "data": {
    "files": [
      {
        "original_name": "report.pdf",
        "stored_name": "1234567890.pdf",
        "download_url": "/d/xxx",
        "storage": "blob",
        "path": "documents/2024/Q3"
      }
    ]
  }
}
```

### 方式二：JSON base64（适合小文件）

```bash
curl -X POST https://your-domain.com/api/v1/upload \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "filename": "report.pdf",
    "content": "base64编码的内容...",
    "content_type": "application/pdf",
    "path": "documents/2024/Q3"
  }'
```

---

## 上传文件（固定路径）

固定路径以 `/v/` 开头，下载链接直接是 `/v/{filename}`，而不是 `/d/{token}`。

### multipart/form-data

```bash
curl -X POST https://your-domain.com/api/v1/upload \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "files=@report.pdf" \
  -F "path=/v/report.pdf"
```

响应：
```json
{
  "success": true,
  "data": {
    "files": [
      {
        "original_name": "report.pdf",
        "stored_name": "1234567890.pdf",
        "download_url": "/v/report.pdf",
        "storage": "blob",
        "path": "/v/report.pdf"
      }
    ]
  }
}
```

### JSON base64

```bash
curl -X POST https://your-domain.com/api/v1/upload \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "filename": "report.pdf",
    "content": "base64编码的内容...",
    "content_type": "application/pdf",
    "path": "/v/report.pdf"
  }'
```

### 固定路径下载

上传后，直接访问下载链接即可：
```
https://your-domain.com/v/report.pdf
```

---

## 常用路径示例

| 路径 | 说明 | 下载链接格式 |
|------|------|-------------|
| `documents/2024/Q3` | 按年份和季度分类 | `/d/{token}` |
| `images/products` | 按类型分类 | `/d/{token}` |
| `user_123/attachments` | 按用户分类 | `/d/{token}` |
| `/v/report.pdf` | 固定路径 | `/v/report.pdf` |
| `/v/logo.png` | 固定路径 | `/v/logo.png` |
| （空） | 默认存储到 uploads/ 目录 | `/d/{token}` |

---

## 完整示例脚本（Python）

```python
import requests
import base64

# 配置
API_URL = "https://your-domain.com"
PIN = "123456"

# 1. 获取 Token
auth_resp = requests.post(f"{API_URL}/api/v1/auth", json={"pin": PIN})
token = auth_resp.json()["token"]

# 2. 上传文件到自定义路径
headers = {"Authorization": f"Bearer {token}"}

with open("report.pdf", "rb") as f:
    files = {"files": ("report.pdf", f, "application/pdf")}
    data = {"path": "documents/2024/Q3"}
    
    upload_resp = requests.post(
        f"{API_URL}/api/v1/upload",
        headers=headers,
        files=files,
        data=data
    )

result = upload_resp.json()
print(f"下载链接: {API_URL}{result['data']['files'][0]['download_url']}")
```

---

## 完整示例脚本（JavaScript/Node.js）

```javascript
const fs = require('fs');
const FormData = require('form-data');
const axios = require('axios');

const API_URL = 'https://your-domain.com';
const PIN = '123456';

async function uploadFile(filePath, storagePath) {
  // 1. 获取 Token
  const authResp = await axios.post(`${API_URL}/api/v1/auth`, { pin: PIN });
  const token = authResp.data.token;

  // 2. 上传文件
  const form = new FormData();
  form.append('files', fs.createReadStream(filePath));
  form.append('path', storagePath);

  const uploadResp = await axios.post(`${API_URL}/api/v1/upload`, form, {
    headers: {
      'Authorization': `Bearer ${token}`,
      ...form.getHeaders()
    }
  });

  return uploadResp.data;
}

// 使用
uploadFile('./report.pdf', 'documents/2024/Q3')
  .then(result => {
    console.log('下载链接:', `${API_URL}${result.data.files[0].download_url}`);
  });
```

---

## 批量上传多文件

```bash
curl -X POST https://your-domain.com/api/v1/upload \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "files=@file1.pdf" \
  -F "files=@file2.pdf" \
  -F "files=@file3.pdf" \
  -F "path=documents/2024/Q3"
```

---

## 大文件上传（预签名 URL）

Vercel 限制请求体最大 4.5MB。对于大文件，使用预签名 URL 直传 Cloudflare R2：

### 普通路径

```bash
# Step 1: 获取预签名 URL
curl -X POST https://your-domain.com/api/r2/presign \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"filename":"video.mp4","disposition":"inline","path":"videos/2024"}'

# 响应：
# {
#   "presignedUrl": "https://xxx.r2.cloudflarestorage.com/...",
#   "downloadUrl": "/d/xxx",
#   "key": "videos/2024/1234567890.mp4",
#   "filename": "1234567890.mp4"
# }

# Step 2: 使用预签名 URL 直传
curl -X PUT "PRESIGNED_URL" \
  -H "Content-Type: video/mp4" \
  --data-binary "@large_video.mp4"
```

### 固定路径

```bash
# 获取预签名 URL（固定路径以 /v/ 开头）
curl -X POST https://your-domain.com/api/r2/presign \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"filename":"credits.ogg","disposition":"inline","path":"/v/credits.ogg"}'

# 直传后，下载链接直接是：https://your-domain.com/v/credits.ogg
```

---

## 完整示例脚本（PowerShell - 大文件）

> 注意：PowerShell 会拆坏 `curl.exe -d '{"..."}'` 里的 JSON，导致服务端 400。下面用 `Invoke-RestMethod`；若坚持用 curl，加 `--%` 停止 PowerShell 解析参数。

```powershell
$body = '{"filename":"credits.ogg","disposition":"inline","path":"/v/credits.ogg"}'

# Step 1: 获取预签名 URL
$presign = Invoke-RestMethod -Uri "https://your-domain.com/api/r2/presign" `
    -Method POST -ContentType "application/json" -Body $body

# Step 2: 直传 R2（--data-binary 路径按实际文件改）
curl.exe -X PUT $presign.presignedUrl `
    -H "Content-Type: audio/ogg" `
    --data-binary "@D:\Downloads\minecraft\sounds\credits.ogg"

Write-Host "下载链接: https://your-domain.com$($presign.downloadUrl)"
```

---

## 注意事项

1. **路径安全**：路径只允许字母、数字、下划线、连字符、斜杠、点
2. **路径长度**：最大 255 字符
3. **禁止路径遍历**：不能使用 `..`、`\` 开头
4. **文件大小**：< 4.5MB 使用 `/api/v1/upload`，>= 4.5MB 使用 `/api/r2/presign` + 直传
5. **固定路径**：以 `/v/` 开头的路径，下载链接直接是 `/v/{filename}`，适合创建永久链接
6. **覆盖行为**：固定路径如果文件已存在，会被新文件覆盖
7. **预签名 URL 有效期**：15 分钟内有效

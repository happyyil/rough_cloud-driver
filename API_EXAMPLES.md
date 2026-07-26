# 上传 API 使用示例

> bash 示例里的 `curl -d '{"..."}'` 在 **PowerShell 中会失败**（JSON 被拆坏 → 服务端 400）。PowerShell 请用文末的 `Invoke-RestMethod` 示例，或给 `curl.exe` 加 `--%`。

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

后续请求在 Header 携带：`Authorization: Bearer YOUR_TOKEN`  
（`/api/v1/upload` 与 `/api/r2/presign` 都需要。）

---

## 上传文件（普通路径，小文件 < 4.5MB）

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
  -d '{"filename":"report.pdf","content":"base64编码的内容...","content_type":"application/pdf","path":"documents/2024/Q3"}'
```

---

## 上传文件（固定路径，小文件 < 4.5MB）

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
  -d '{"filename":"report.pdf","content":"base64编码的内容...","content_type":"application/pdf","path":"/v/report.pdf"}'
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

## 完整示例脚本（Python，小文件 < 4.5MB）

```python
import requests

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

大文件请用下面的预签名流程，或参考 `examples/api_upload.py`。

---

## 完整示例脚本（JavaScript/Node.js，小文件 < 4.5MB）

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

## 批量上传多文件（小文件 < 4.5MB）

```bash
curl -X POST https://your-domain.com/api/v1/upload \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "files=@file1.pdf" \
  -F "files=@file2.pdf" \
  -F "files=@file3.pdf" \
  -F "path=documents/2024/Q3"
```

> 注意：多个小文件合计也受 Vercel 4.5MB 请求体限制；过大请改走预签名逐个直传。

---

## 大文件上传（预签名 URL，>= 4.5MB）

Vercel 限制请求体最大 4.5MB。大文件先拿预签名 URL，再直传 Cloudflare R2。

**重要：** 预签名请求里的 `contentType` 必须与 Step 2 `PUT` 的 `Content-Type` 一致，否则 R2 会拒收（签名绑定了 Content-Type）。

### 普通路径

```bash
# Step 1: 获取预签名 URL（需要 Token）
curl -X POST https://your-domain.com/api/r2/presign \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"filename":"video.mp4","contentType":"video/mp4","disposition":"inline","path":"videos/2024"}'

# 响应：
# {
#   "presignedUrl": "https://xxx.r2.cloudflarestorage.com/...",
#   "downloadUrl": "/d/xxx",
#   "key": "videos/2024/1234567890.mp4",
#   "filename": "1234567890.mp4"
# }

# Step 2: 使用预签名 URL 直传（Content-Type 与上一步 contentType 一致）
curl -X PUT "PRESIGNED_URL" \
  -H "Content-Type: video/mp4" \
  --data-binary "@large_video.mp4"
```

### 固定路径

```bash
# Step 1: 获取预签名 URL（固定路径以 /v/ 开头）
curl -X POST https://your-domain.com/api/r2/presign \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"filename":"credits.ogg","contentType":"audio/ogg","disposition":"inline","path":"/v/credits.ogg"}'

# 响应：
# {
#   "presignedUrl": "https://xxx.r2.cloudflarestorage.com/...",
#   "downloadUrl": "/v/credits.ogg",
#   "key": "v/credits.ogg",
#   "filename": "1234567890.ogg"
# }

# Step 2: 直传 R2
curl -X PUT "PRESIGNED_URL" \
  -H "Content-Type: audio/ogg" \
  --data-binary "@credits.ogg"

# 下载链接：https://your-domain.com/v/credits.ogg
```

---

## 完整示例脚本（Python - 大文件）

```python
import requests
import os

API_URL = "https://your-domain.com"
PIN = "123456"
FILE_PATH = "large_video.mp4"
CONTENT_TYPE = "video/mp4"
PATH = "videos/2024"  # 或固定路径 "/v/video.mp4"

token = requests.post(f"{API_URL}/api/v1/auth", json={"pin": PIN}).json()["token"]
headers = {"Authorization": f"Bearer {token}"}

presign = requests.post(
    f"{API_URL}/api/r2/presign",
    headers=headers,
    json={
        "filename": os.path.basename(FILE_PATH),
        "contentType": CONTENT_TYPE,
        "disposition": "inline",
        "path": PATH,
    },
).json()

with open(FILE_PATH, "rb") as f:
    put = requests.put(
        presign["presignedUrl"],
        headers={"Content-Type": CONTENT_TYPE},
        data=f,
    )

print("status:", put.status_code)
print("下载链接:", f"{API_URL}{presign['downloadUrl']}")
```

---

## 完整示例脚本（PowerShell - 大文件）

> PowerShell 会拆坏 `curl.exe -d '{"..."}'` 里的 JSON，导致服务端 400。下面用 `Invoke-RestMethod`；若坚持用 curl，加 `--%` 停止 PowerShell 解析参数。

```powershell
$token = "YOUR_TOKEN"
$contentType = "audio/ogg"
$body = @{
    filename    = "credits.ogg"
    contentType = $contentType
    disposition = "inline"
    path        = "/v/credits.ogg"
} | ConvertTo-Json

# Step 1: 获取预签名 URL（需要 Token）
$presign = Invoke-RestMethod -Uri "https://your-domain.com/api/r2/presign" `
    -Method POST -ContentType "application/json" -Body $body `
    -Headers @{ Authorization = "Bearer $token" }

# Step 2: 直传 R2（Content-Type 必须与 contentType 一致）
curl.exe -X PUT $presign.presignedUrl `
    -H "Content-Type: $contentType" `
    --data-binary "@D:\Downloads\minecraft\sounds\credits.ogg"

Write-Host "下载链接: https://your-domain.com$($presign.downloadUrl)"
```

---

## 注意事项

1. **认证**：`/api/v1/auth` 换 Token；`/api/v1/upload` 与 `/api/r2/presign` 均需 `Authorization: Bearer ...`
2. **文件大小**：< 4.5MB 用 `/api/v1/upload`；>= 4.5MB 用 `/api/r2/presign` + 直传（不要把大文件 POST 到 upload）
3. **contentType**：预签名里的 `contentType` 必须与 PUT 的 `Content-Type` 完全一致
4. **路径安全**：只允许字母、数字、下划线、连字符、斜杠、点；禁止 `..`、`\` 开头；最长 255
5. **固定路径**：以 `/v/` 开头 → 下载链接为 `/v/{filename}`；已存在则覆盖
6. **R2 key**：固定路径在存储中为 `v/{filename}`（无前导 `/`）
7. **预签名 URL 有效期**：15 分钟
8. **PowerShell**：不要用 `curl.exe -d '{"..."}'` 传 JSON，改用 `Invoke-RestMethod` 或 `curl.exe --%`

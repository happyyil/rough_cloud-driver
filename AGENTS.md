# AGENTS.md - 项目技术文档

> 本文档为 AI 助手提供项目上下文，包含架构、API、配置等技术细节。

## 项目概述

这是一个基于 Flask 的文件上传服务，部署在 Vercel 平台上。核心特性是**三种存储类型架构**：
- **文件上传**：小文件（< 4.5MB）→ Vercel Blob，大文件（>= 4.5MB）→ Cloudflare R2
- **URL 重定向**：短链接功能，访问 `/v/<name>` 重定向到目标 URL

### 核心功能
- **Web 界面**：用户友好的文件上传界面，支持多文件上传、进度显示
- **RESTful API**：完整的 API 接口，支持程序化调用
- **双存储策略**：自动根据文件大小选择存储后端
- **URL 重定向**：短链接功能，支持 URL 验证和重定向
- **自定义路径**：支持按业务逻辑组织文件（如 `documents/2024/Q3`）
- **固定路径**：支持固定 URL 的文件（如 `/v/logo.png`），适合需要稳定访问链接的场景
- **认证系统**：PIN 码认证 + HMAC 签名 Token，支持 IP 限流保护

## 技术栈

- **后端框架**：Flask 3.0.0
- **存储服务**：
  - Vercel Blob Storage（小文件，< 4.5MB）
  - Cloudflare R2（大文件，>= 4.5MB，兼容 S3 API）
  - URL 映射存储（R2: `.url_mappings/`）
- **AWS SDK**：boto3 / botocore（用于 R2 操作）
- **HTTP 客户端**：requests
- **部署平台**：Vercel（Serverless Functions）

## 项目结构

```
upload_app/
├── app.py                    # 主应用文件（单文件架构，所有路由和逻辑）
├── requirements.txt          # Python 依赖
├── vercel.json              # Vercel 部署配置
├── .env.example             # 环境变量示例
├── API_EXAMPLES.md          # API 使用文档（详细示例）
├── QWEN.md                  # 开发经验与教训总结
├── AGENTS.md                # 项目技术文档（本文件）
├── templates/               # HTML 模板
│   ├── index.html          # 上传页面
│   ├── teacher.html       # 文件管理后台
│   └── pin_verify.html    # PIN 码验证页面
└── examples/
    └── api_upload.py       # API 使用示例脚本
```

## 环境变量配置

必需的环境变量（在 Vercel Dashboard 或 `.env.local` 中配置）：

```bash
# Vercel Blob Token（Vercel 自动提供）
BLOB_READ_WRITE_TOKEN=

# PIN 码哈希（SHA256）
# 生成方法：echo -n "你的PIN码" | sha256sum
PIN_HASH=

# Flask Session 密钥
SECRET_KEY=

# Cloudflare R2 配置
R2_ACCOUNT_ID=
R2_ACCESS_KEY_ID=
R2_SECRET_KEY=
R2_BUCKET_NAME=
R2_PUBLIC_URL=              # 可选：自定义域名
```

## 本地开发

### 安装依赖
```bash
pip install -r requirements.txt
```

### 运行开发服务器
```bash
python app.py
```
Flask 默认在 `http://localhost:5000` 启动。

### 环境变量设置
复制 `.env.example` 为 `.env.local` 并填写实际值：
```bash
cp .env.example .env.local
```

## 部署到 Vercel

### 一键部署
项目已配置 `vercel.json`，直接推送代码到 Git 仓库，Vercel 会自动部署。

### 手动部署
```bash
vercel --prod
```

### 环境变量配置
在 Vercel Dashboard → Settings → Environment Variables 中配置所有必需变量。

## API 使用指南

### 认证流程
```bash
# 1. 获取 Token（有效期 24 小时）
curl -X POST https://your-domain.com/api/v1/auth \
  -H "Content-Type: application/json" \
  -d '{"pin": "123456"}'

# 响应：{"success": true, "token": "xxx", "expire_in": 86400}
```

### 小文件上传（< 4.5MB）
```bash
# 方式一：multipart/form-data
curl -X POST https://your-domain.com/api/v1/upload \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "files=@report.pdf" \
  -F "path=documents/2024/Q3"

# 方式二：JSON base64
curl -X POST https://your-domain.com/api/v1/upload \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"filename":"report.pdf","content":"base64...","path":"documents/2024/Q3"}'
```

### 大文件上传（>= 4.5MB）
```bash
# Step 1: 获取预签名 URL
curl -X POST https://your-domain.com/api/r2/presign \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"filename":"video.mp4","contentType":"video/mp4","path":"videos/2024"}'

# Step 2: 直传到 R2（Content-Type 必须与 contentType 一致）
curl -X PUT "PRESIGNED_URL" \
  -H "Content-Type: video/mp4" \
  --data-binary "@large_video.mp4"
```

**重要提示**：
- 预签名请求中的 `contentType` 必须与 Step 2 的 `Content-Type` 完全一致，否则 R2 会拒收
- 预签名 URL 有效期：15 分钟

### URL 重定向（短链接）
上传 URL 类型，实现短链接功能。访问 `/v/<name>` 时自动重定向到目标 URL：
```bash
# 方式一：JSON base64
URL_B64=$(echo -n "https://www.example.com" | base64)
curl -X POST https://your-domain.com/api/v1/upload \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"filename\":\"mylink.txt\",\"content\":\"$URL_B64\",\"type\":\"url\",\"path\":\"/v/mylink\"}"

# 方式二：multipart/form-data
echo -n "https://www.example.com" > url.txt
curl -X POST https://your-domain.com/api/v1/upload \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "files=@url.txt" \
  -F "type=url" \
  -F "path=/v/mylink"

# 访问 https://your-domain.com/v/mylink → 重定向到 https://www.example.com
```

**URL 重定向特性**：
- 必须使用固定路径（`/v/` 开头）
- 上传时验证 URL 可访问性（HTTP 200）
- 下载链接无扩展名
- 访问时优先检查 URL 映射，再查找文件

### 固定路径上传
固定路径以 `/v/` 开头，下载链接直接是 `/v/{filename}`，适合需要稳定 URL 的场景：
```bash
curl -X POST https://your-domain.com/api/v1/upload \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "files=@logo.png" \
  -F "path=/v/logo.png"

# 下载链接：https://your-domain.com/v/logo.png
```

### 路径规则
| 路径格式 | 说明 | 下载链接 |
|---------|------|---------|
| `documents/2024/Q3` | 普通路径 | `/d/{token}` |
| `/v/report.pdf` | 固定路径 | `/v/report.pdf` |
| `/v/mylink` (type=url) | URL 重定向 | `/v/mylink` → 重定向 |
| （空） | 默认 uploads/ | `/d/{token}` |

**路径安全限制**：
- 只允许字母、数字、下划线、连字符、斜杠、点
- 禁止 `..`、`\` 开头
- 最长 255 字符

## 核心架构

### 三种存储类型
```
上传请求
    ↓
检查 type 参数
    ↓
├─ type=url → URL 映射存储
│              (存储到 R2: .url_mappings/<name>.json)
│              访问 /v/<name> → 重定向到目标 URL
│
├─ type=file + < 4.5MB → Vercel Blob Storage
│                        (通过 Vercel API 上传)
│
└─ type=file + >= 4.5MB → Cloudflare R2
                           (预签名 URL 直传)
```

**为什么是 4.5MB？**
Vercel Serverless Functions 请求体限制为 4.5MB，超过此限制的请求会被拒绝。

### URL 映射存储
- **存储位置**：R2 存储桶的 `.url_mappings/` 路径
- **文件格式**：JSON，包含 `url` 和 `created_at`
- **访问优先级**：访问 `/v/<name>` 时，优先检查 URL 映射，再查找文件

### Token 认证机制
- **API Token**：HMAC 签名，有效期 24 小时，用于所有 API 调用
- **Web Presign Token**：短期 Token（1 小时），仅用于网页端大文件上传的 `/api/r2/presign` 接口
- **签名验证**：`user_id:expire_time` + HMAC-SHA256 签名

### 安全特性
- **IP 限流**：5 次失败后锁定 5 分钟
- **路径遍历防护**：严格验证路径字符，防止 `..` 攻击
- **文件类型白名单**：只允许特定扩展名
- **Token 过期机制**：所有 Token 都有过期时间

## 关键代码位置

### 主应用文件 `app.py`
- **配置和初始化**：第 1-50 行
- **Token 生成/验证**：`_sign_token()`, `verify_api_token()` 函数
- **存储操作**：
  - Vercel Blob：`blob_upload()`, `blob_list()`
  - R2：`get_presigned_url()`, `get_r2_url()`
  - URL 映射：`save_url_mapping()`, `get_url_mapping()`, `validate_url()`
- **API 路由**：
  - `/api/v1/auth`：认证接口
  - `/api/v1/upload`：小文件上传 + URL 重定向
  - `/api/r2/presign`：大文件预签名
  - `/api/upload`：批量上传（Web 界面用）
- **Web 路由**：
  - `/`：上传页面
  - `/teacher`：文件管理后台
  - `/d/<token>`：下载文件
  - `/v/<filename>`：固定路径下载 + URL 重定向

### 前端逻辑 `templates/index.html`
- 自动检测文件大小，选择上传策略
- 小文件：逐个 POST 到 `/api/upload`
- 大文件：获取预签名 URL，XHR 直传 R2
- 进度条显示

## 常见问题

### Q: URL 重定向上传失败，提示"URL 无法访问"？
A: 上传时会验证 URL 是否返回 HTTP 200。确保目标 URL 可公开访问，且服务器响应正常。内网 URL 或需要认证的 URL 无法通过验证。

### Q: 如何删除或更新 URL 映射？
A: 目前需要手动删除 R2 存储桶中 `.url_mappings/<name>.json` 文件，或重新上传同名映射覆盖。

### Q: URL 映射和文件同名会怎样？
A: 访问 `/v/<name>` 时，URL 映射优先级更高。如果存在 URL 映射，会重定向到 URL，不会下载文件。

### Q: 大文件上传失败，返回 403？
A: 检查预签名请求的 `contentType` 是否与 PUT 请求的 `Content-Type` 完全一致。R2 签名绑定了 Content-Type。

### Q: PowerShell 中 `curl -d '{"..."}'` 失败？
A: PowerShell 会拆坏 JSON 字符串。使用 `Invoke-RestMethod` 或给 `curl.exe` 加 `--%` 停止 PowerShell 解析。

### Q: 如何生成 PIN_HASH？
A: `echo -n "你的PIN码" | sha256sum`，将输出的哈希值配置到环境变量。

### Q: 固定路径文件会被覆盖吗？
A: 是的，固定路径（`/v/{filename}`）如果已存在会被新文件覆盖。这是设计行为，适合需要稳定 URL 的场景。

### Q: R2 key 为什么不以 `/` 开头？
A: R2 存储规范要求 key 不能以 `/` 开头。代码中固定路径 `/v/report.pdf` 在 R2 中存储为 `v/report.pdf`。

## 开发约定

### 代码风格
- 单文件架构（`app.py` 包含所有逻辑）
- 函数命名：snake_case
- 路由命名：RESTful 风格
- 错误处理：返回 JSON 格式 `{"success": false, "error": "..."}`

### 测试
目前没有自动化测试。手动测试可使用 `examples/api_upload.py`。

### Git 提交规范
- `feat:` 新功能
- `fix:` 修复 bug
- `docs:` 文档更新
- `refactor:` 重构

## 参考文档

- **API 详细示例**：`API_EXAMPLES.md`
- **API 使用脚本**：`examples/api_upload.py`
- **开发经验总结**：`QWEN.md`
- **Vercel Blob 文档**：https://vercel.com/docs/storage/vercel-blob
- **Cloudflare R2 文档**：https://developers.cloudflare.com/r2/
- **Flask 文档**：https://flask.palletsprojects.com/

## 注意事项

1. **文件大小限制**：Vercel 请求体限制 4.5MB，大文件必须用预签名 URL
2. **Token 安全**：Token 包含签名和过期时间，不要泄露
3. **R2 配置**：确保 R2 存储桶已创建，且访问密钥有读写权限
4. **CORS**：R2 直传需要配置 CORS 规则（已在 R2 存储桶设置中配置）
5. **环境变量**：生产环境不要提交 `.env.local` 到 Git
6. **URL 验证**：URL 重定向功能会验证目标 URL 可访问性，内网 URL 无法通过验证
7. **URL 映射优先级**：访问 `/v/<name>` 时，URL 映射优先于同名文件

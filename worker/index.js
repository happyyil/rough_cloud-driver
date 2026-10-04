function decodeDownloadToken(token) {
  try {
    const base64 = token.replace(/-/g, '+').replace(/_/g, '/');
    const binary = atob(base64 + '='.repeat((4 - base64.length % 4) % 4));
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    const parts = new TextDecoder().decode(bytes).split(':');

    if (parts.length >= 5) {
      const path = parts.pop();
      const disposition = parts.pop();
      const storage = parts.pop();
      const storedName = parts.pop();
      return {
        originalName: parts.join(':'),
        storedName,
        storage,
        disposition,
        path
      };
    }

    if (parts.length === 4) {
      const [originalName, storedName, storage, disposition] = parts;
      return { originalName, storedName, storage, disposition, path: '' };
    }

    if (parts.length === 3) {
      const [originalName, storedName, storage] = parts;
      return { originalName, storedName, storage, disposition: 'inline', path: '' };
    }
  } catch (error) {
    return null;
  }

  return null;
}

function isSafeObjectPart(value) {
  return /^[A-Za-z0-9_.-]+$/.test(value) && value !== '.' && value !== '..';
}

function makeContentDisposition(disposition, filename) {
  const safeDisposition = disposition === 'attachment' ? 'attachment' : 'inline';
  const fallback = filename
    .replace(/[^\x20-\x7E]|["\\]/g, '_')
    .replace(/[\r\n]/g, '_') || 'download';
  const encoded = encodeURIComponent(filename).replace(/[!'()*]/g, (character) =>
    `%${character.charCodeAt(0).toString(16).toUpperCase()}`
  );

  return `${safeDisposition}; filename="${fallback}"; filename*=UTF-8''${encoded}`;
}

export default {
  async fetch(request, env) {
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('方法不允许', { status: 405, headers: { Allow: 'GET, HEAD' } });
    }

    const token = new URL(request.url).pathname.split('/').pop();
    const payload = decodeDownloadToken(token);
    if (!payload || payload.storage !== 'r2') {
      return new Response('无效的下载链接', { status: 400 });
    }

    if (!payload.originalName || !isSafeObjectPart(payload.storedName)) {
      return new Response('无效的文件路径', { status: 400 });
    }

    const pathParts = payload.path ? payload.path.split('/') : [];
    if (pathParts.some((part) => !isSafeObjectPart(part))) {
      return new Response('无效的文件路径', { status: 400 });
    }

    const key = [...(pathParts.length ? pathParts : ['uploads']), payload.storedName].join('/');
    if (key.startsWith('.tokens/') || key.startsWith('.url_mappings/') || key.startsWith('.login_attempts')) {
      return new Response('无效的文件路径', { status: 400 });
    }

    let object;
    try {
      if (request.method === 'HEAD') {
        object = await env.UPLOADS.head(key);
      } else if (request.headers.has('Range')) {
        object = await env.UPLOADS.get(key, { range: request.headers });
      } else {
        object = await env.UPLOADS.get(key);
      }
    } catch (error) {
      console.error('R2 download failed', error);
      return new Response('读取文件失败', { status: 502 });
    }

    if (!object) {
      return new Response('文件不存在', { status: 404 });
    }

    const headers = new Headers();
    object.writeHttpMetadata(headers);
    headers.set('Content-Type', object.httpMetadata?.contentType || 'application/octet-stream');
    headers.set('Content-Disposition', makeContentDisposition(payload.disposition, payload.originalName));
    headers.set('Accept-Ranges', 'bytes');
    headers.set('ETag', object.httpEtag);

    let status = 200;
    if (object.range) {
      const { offset, length } = object.range;
      headers.set('Content-Length', String(length));
      headers.set('Content-Range', `bytes ${offset}-${offset + length - 1}/${object.size}`);
      status = 206;
    } else {
      headers.set('Content-Length', String(object.size));
    }

    return new Response(request.method === 'HEAD' ? null : object.body, { status, headers });
  }
};

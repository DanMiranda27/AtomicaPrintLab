"""Transferência temporária de imagens do smartphone para o Atômica Print Lab.

O servidor escuta somente na rede local (0.0.0.0) e usa tokens aleatórios com
expiração. Os arquivos ficam em memória até serem consumidos pela sessão Streamlit.
"""
from __future__ import annotations

import html
import ipaddress
import json
import os
import secrets
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import qrcode

MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_SESSION_BYTES = 250 * 1024 * 1024
MAX_SESSION_FILES = 50
SESSION_TTL_SECONDS = 30 * 60
ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

_LOCK = threading.RLock()
_SESSIONS: dict[str, dict] = {}
_SERVER: ThreadingHTTPServer | None = None
_SERVER_THREAD: threading.Thread | None = None
_SERVER_PORT: int | None = None


def _purge_expired_locked(now: float | None = None) -> None:
    now = time.time() if now is None else now
    expired = [token for token, session in _SESSIONS.items() if session["expires_at"] < now]
    for token in expired:
        _SESSIONS.pop(token, None)


def create_upload_session() -> str:
    """Cria uma sessão de upload com token imprevisível e validade de 30 min."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _LOCK:
        _purge_expired_locked(now)
        _SESSIONS[token] = {
            "created_at": now,
            "expires_at": now + SESSION_TTL_SECONDS,
            "files": deque(),
            "total_bytes": 0,  # bytes ainda na fila, aguardando o Pool
            "received_files": 0,
            "received_bytes": 0,
            "upload_ids": set(),
        }
    return token


def renew_upload_session(token: str) -> bool:
    with _LOCK:
        _purge_expired_locked()
        session = _SESSIONS.get(str(token))
        if not session:
            return False
        session["expires_at"] = time.time() + SESSION_TTL_SECONDS
        return True


def close_upload_session(token: str) -> None:
    with _LOCK:
        _SESSIONS.pop(str(token), None)


def upload_session_is_active(token: str) -> bool:
    """Informa se o token ainda existe sem renovar o prazo de inatividade."""
    with _LOCK:
        _purge_expired_locked()
        return str(token) in _SESSIONS


def take_uploaded_files(token: str) -> list[tuple[str, bytes]]:
    """Retira da fila os arquivos recebidos; cada item é (nome, bytes)."""
    with _LOCK:
        _purge_expired_locked()
        session = _SESSIONS.get(str(token))
        if not session:
            return []
        # A consulta periódica do Streamlit não renova a sessão: somente o
        # smartphone renova a validade ao abrir a página ou enviar um arquivo.
        files = list(session["files"])
        session["files"].clear()
        session["total_bytes"] = 0
        return files


def get_mobile_upload_port() -> int:
    """Inicia o servidor local uma única vez e retorna a porta utilizada."""
    global _SERVER, _SERVER_THREAD, _SERVER_PORT
    if _SERVER is not None and _SERVER_PORT is not None:
        return _SERVER_PORT

    class UploadHandler(BaseHTTPRequestHandler):
        server_version = "AtomicaMobileUpload/1.0"

        def log_message(self, _format, *_args):
            # Não registra tokens, nomes de arquivos nem dados recebidos no console.
            return

        def _send(self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8"):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/")
            ping_prefix = "/api/ping/"
            if path.startswith(ping_prefix):
                token = path[len(ping_prefix):]
                if not renew_upload_session(token):
                    self._send(410, b'{"ok":false,"error":"Sessao expirada."}', "application/json; charset=utf-8")
                    return
                self._send(200, b'{"ok":true}', "application/json; charset=utf-8")
                return

            prefix = "/upload/"
            if not path.startswith(prefix):
                self._send(404, b"Pagina nao encontrada.")
                return
            token = path[len(prefix):]
            if not renew_upload_session(token):
                self._send(410, b"Este QR Code expirou. Gere um novo codigo no computador.")
                return
            page = _mobile_upload_html(token).encode("utf-8")
            self._send(200, page, "text/html; charset=utf-8")

        def do_POST(self):
            path = urlparse(self.path).path.rstrip("/")
            prefix = "/api/upload/"
            if not path.startswith(prefix):
                self._send(404, b'{"ok":false,"error":"Rota nao encontrada."}', "application/json; charset=utf-8")
                return
            token = path[len(prefix):]
            if not renew_upload_session(token):
                self._send(410, b'{"ok":false,"error":"QR Code expirado."}', "application/json; charset=utf-8")
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_UPLOAD_BYTES:
                self._send(413, b'{"ok":false,"error":"Arquivo vazio ou maior que 100 MB."}', "application/json; charset=utf-8")
                return

            raw_name = self.headers.get("X-Filename", "imagem.jpg")
            upload_id = str(self.headers.get("X-Upload-Id", "")).strip()[:100]
            if not upload_id:
                upload_id = secrets.token_urlsafe(18)
            filename = unquote(raw_name).replace("\\", "/").split("/")[-1].strip()
            if not filename or len(filename) > 180:
                filename = "imagem.jpg"
            extension = os.path.splitext(filename)[1].lower()
            if extension not in ALLOWED_EXTENSIONS:
                self._send(415, b'{"ok":false,"error":"Use PNG, JPG, JPEG ou WEBP."}', "application/json; charset=utf-8")
                return

            data = self.rfile.read(length)
            if len(data) != length:
                self._send(400, b'{"ok":false,"error":"Transferencia incompleta."}', "application/json; charset=utf-8")
                return

            with _LOCK:
                _purge_expired_locked()
                session = _SESSIONS.get(token)
                if not session:
                    self._send(410, b'{"ok":false,"error":"QR Code expirado."}', "application/json; charset=utf-8")
                    return
                if upload_id in session.setdefault("upload_ids", set()):
                    # Um retry pode chegar depois de o servidor já ter recebido
                    # o arquivo, mas antes de o navegador receber a resposta.
                    # A chave idempotente evita duplicar a arte no Pool.
                    duplicate = True
                else:
                    duplicate = False
                    if session["received_files"] >= MAX_SESSION_FILES:
                        self._send(429, b'{"ok":false,"error":"Limite de 50 imagens por sessao."}', "application/json; charset=utf-8")
                        return
                    if session["received_bytes"] + len(data) > MAX_SESSION_BYTES:
                        self._send(429, b'{"ok":false,"error":"Limite total de 250 MB por sessao atingido."}', "application/json; charset=utf-8")
                        return
                    session["files"].append((filename, data))
                    session["upload_ids"].add(upload_id)
                    session["total_bytes"] += len(data)
                    session["received_files"] += 1
                    session["received_bytes"] += len(data)
                session["expires_at"] = time.time() + SESSION_TTL_SECONDS

            response = json.dumps({"ok": True, "duplicate": duplicate, "name": filename, "bytes": len(data)}, ensure_ascii=False).encode("utf-8")
            self._send(200, response, "application/json; charset=utf-8")

    last_error: OSError | None = None
    for port in range(8765, 8786):
        try:
            server = ThreadingHTTPServer(("0.0.0.0", port), UploadHandler)
            server.daemon_threads = True
            server.allow_reuse_address = True
            _SERVER = server
            _SERVER_PORT = port
            _SERVER_THREAD = threading.Thread(target=server.serve_forever, name="AtomicaMobileUpload", daemon=True)
            _SERVER_THREAD.start()
            return port
        except OSError as exc:
            last_error = exc
    raise RuntimeError(f"Não foi possível iniciar o recebimento local de imagens: {last_error}")


def get_lan_ip() -> str | None:
    """Descobre o IPv4 usado pelo computador na rede local, sem transmitir dados."""
    configured = os.environ.get("ATOMICA_MOBILE_UPLOAD_HOST", "").strip()
    if configured:
        return configured
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
        address = sock.getsockname()[0]
        parsed = ipaddress.ip_address(address)
        if parsed.version == 4 and parsed.is_private and not parsed.is_loopback and not parsed.is_link_local:
            return address
    except OSError:
        pass
    finally:
        sock.close()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = item[4][0]
            parsed = ipaddress.ip_address(address)
            if parsed.version == 4 and parsed.is_private and not parsed.is_loopback and not parsed.is_link_local:
                return address
    except OSError:
        pass
    return None


def make_qr_code_png(url: str) -> bytes:
    """Gera o QR Code em PNG para exibição no Streamlit."""
    image = qrcode.make(url, border=3, box_size=8)
    output = __import__("io").BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _mobile_upload_html(token: str) -> str:
    token_json = json.dumps(token)
    return """<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#101923"><title>Enviar imagens · Atômica Print Lab</title>
<style>
:root{color-scheme:dark;font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#101923;color:#eef4fa}
*{box-sizing:border-box}body{margin:0;padding:24px 16px 40px;min-height:100vh;background:radial-gradient(circle at 50% 0,#1a3550,#101923 65%)}
main{max-width:520px;margin:0 auto}.brand{font-size:12px;letter-spacing:.12em;font-weight:800;color:#6ed3e1}.panel{margin-top:18px;padding:20px;border:1px solid #34516e;border-radius:18px;background:#14263a;box-shadow:0 15px 40px #0005}
h1{font-size:24px;line-height:1.15;margin:8px 0 10px}p{color:#b8c9d9;font-size:14px;line-height:1.5}.picker{display:flex;align-items:center;justify-content:center;min-height:58px;margin:20px 0 12px;padding:12px;border:1px dashed #5c8eae;border-radius:12px;background:#1a3550;color:#e8f8ff;font-weight:750;text-align:center}
input{position:absolute;width:1px;height:1px;opacity:0}.send{width:100%;min-height:48px;border:0;border-radius:11px;background:#0e7c91;color:white;font-size:15px;font-weight:800}.send:disabled{opacity:.45}.status{margin-top:14px;font-size:13px;color:#9edce5;white-space:pre-line}.files{font-size:12px;color:#c0d2df;margin-top:10px;overflow-wrap:anywhere}.fine{font-size:11px;color:#91a8bb;margin-top:20px}
</style></head><body><main><div class="brand">ATÔMICA PRINT LAB</div><section class="panel"><div aria-hidden="true">📲</div><h1>Enviar imagens para o computador</h1><p>Selecione fotos da galeria ou tire uma fotografia. As imagens serão enviadas diretamente para o Atômica Print Lab aberto no computador.</p>
<label class="picker" for="photos">＋ Escolher imagens ou abrir a câmera</label><input id="photos" type="file" accept="image/png,image/jpeg,image/webp" multiple>
<button class="send" id="send" disabled>Enviar imagens</button><div class="files" id="files"></div><div class="status" id="status" role="status" aria-live="polite"></div>
<p class="fine">Use somente uma rede Wi‑Fi confiável. Este código é temporário e expira após 30 minutos de inatividade. Limite: 50 imagens e 250 MB por sessão, até 100 MB por arquivo. Formatos aceitos: PNG, JPG e WEBP.</p></section></main>
<script>
const token = __TOKEN__;
const input = document.getElementById('photos');
const button = document.getElementById('send');
const status = document.getElementById('status');
const filesLabel = document.getElementById('files');
let pendingFiles = [];
const uploadIds = new WeakMap();
const uploadIdFor = file => {
  if (!uploadIds.has(file)) {
    const id = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    uploadIds.set(file, id);
  }
  return uploadIds.get(file);
};
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

// Renova a sessao sem depender do WebSocket do Streamlit.
const keepAlive = () => fetch(`/api/ping/${token}`, {cache:'no-store'}).catch(() => {});
setInterval(keepAlive, 3 * 60 * 1000);

input.addEventListener('change', () => {
  pendingFiles = [...input.files];
  button.disabled = pendingFiles.length === 0;
  filesLabel.textContent = pendingFiles.map(f => `${f.name} · ${(f.size/1048576).toFixed(1)} MB`).join('\\n');
  status.textContent = pendingFiles.length ? `${pendingFiles.length} imagem(ns) selecionada(s).` : '';
});

function uploadOne(file, index, total, attempt) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `/api/upload/${token}`, true);
    xhr.timeout = 180000;
    xhr.setRequestHeader('Content-Type', file.type || 'application/octet-stream');
    xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
    xhr.setRequestHeader('X-Upload-Id', uploadIdFor(file));
    xhr.upload.addEventListener('progress', event => {
      if (event.lengthComputable) {
        const pct = Math.round(event.loaded / event.total * 100);
        status.textContent = `Enviando ${index} de ${total} · ${file.name} · ${pct}%` + (attempt ? ` · tentativa ${attempt + 1}` : '');
      }
    });
    xhr.onload = () => {
      let result = {};
      try { result = JSON.parse(xhr.responseText || '{}'); } catch (_) {}
      if (xhr.status >= 200 && xhr.status < 300 && result.ok) resolve(result);
      else reject(new Error(result.error || `Servidor respondeu ${xhr.status}.`));
    };
    xhr.onerror = () => reject(new Error('Conexao interrompida durante o envio.'));
    xhr.ontimeout = () => reject(new Error('O envio demorou mais que o limite de 3 minutos.'));
    xhr.onabort = () => reject(new Error('Envio cancelado.'));
    xhr.send(file);
  });
}

async function uploadWithRetry(file, index, total) {
  let lastError;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      return await uploadOne(file, index, total, attempt);
    } catch (error) {
      lastError = error;
      if (attempt < 2) {
        status.textContent = `${file.name}: falha temporaria. Tentando novamente (${attempt + 2}/3)…`;
        await delay(700 * (attempt + 1));
        await keepAlive();
      }
    }
  }
  throw lastError || new Error('Falha no envio.');
}

button.addEventListener('click', async () => {
  const files = [...pendingFiles];
  if (!files.length) return;
  button.disabled = true;
  const failed = [];
  let sent = 0;
  for (let index = 0; index < files.length; index++) {
    const file = files[index];
    status.textContent = `Preparando ${index + 1} de ${files.length}: ${file.name}`;
    try {
      await uploadWithRetry(file, index + 1, files.length);
      sent++;
    } catch (error) {
      failed.push(file);
    }
  }
  pendingFiles = failed;
  if (!failed.length) {
    status.textContent = `${sent} de ${files.length} imagem(ns) enviada(s) com sucesso. Pode voltar ao Atômica Print Lab.`;
    input.value = '';
    filesLabel.textContent = '';
  } else {
    status.textContent = `${sent} de ${files.length} imagem(ns) enviada(s). ${failed.length} falharam; toque novamente para reenviar somente as pendentes.`;
    filesLabel.textContent = failed.map(f => `${f.name} · ${(f.size/1048576).toFixed(1)} MB`).join('\\n');
  }
  button.textContent = failed.length ? 'Tentar novamente as pendentes' : 'Enviar imagens';
  button.disabled = failed.length === 0;
});
</script></body></html>""".replace("__TOKEN__", token_json)

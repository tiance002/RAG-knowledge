"""One explicit image-description provider path; no failure text as evidence."""
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile

import httpx
from app.core.config import get_settings, provider_tls_context
from app.services.llm import invoke_provider_http_async
from app.services.observability import event


class VLMDescriptionError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__("Image description did not complete: " + status)


class VLMService:
    def __init__(self, base_url=None, api_key=None, model=None, prompt=None):
        settings = get_settings()
        self.base_url = base_url if base_url is not None else settings.VLM_BASE_URL
        self.api_key = api_key if api_key is not None else settings.VLM_API_KEY
        self.model = model if model is not None else settings.VLM_MODEL
        self.prompt = prompt if prompt is not None else settings.VLM_PROMPT

    async def describe_image(self, image_path):
        if not self.base_url or not self.model:
            raise VLMDescriptionError("NOT_CONFIGURED")
        path = Path(image_path)
        ext = path.suffix.lower().lstrip('.')
        mime = {'jpg':'jpeg', 'jpeg':'jpeg', 'png':'png', 'gif':'gif', 'webp':'webp'}.get(ext, 'png')
        encoded = base64.b64encode(path.read_bytes()).decode('ascii')
        async with httpx.AsyncClient(timeout=120.0, verify=provider_tls_context()) as client:
            response = await invoke_provider_http_async(lambda: client.post(
                self.base_url.rstrip('/') + '/chat/completions',
                headers={'Authorization':'Bearer ' + self.api_key, 'Content-Type':'application/json'},
                json={'model':self.model, 'messages':[{'role':'user', 'content':[
                    {'type':'text','text':self.prompt},
                    {'type':'image_url','image_url':{'url':'data:image/' + mime + ';base64,' + encoded}}
                ]}], 'max_tokens':1000}), stage='vlm', model_name=self.model)
            response.raise_for_status()
            result = response.json()
        choice = result['choices'][0]
        reason = choice.get('finish_reason')
        if reason not in ('stop', None):
            raise VLMDescriptionError('TRUNCATED' if reason == 'length' else 'UNKNOWN')
        text = choice.get('message', {}).get('content')
        if not isinstance(text, str) or not text.strip():
            raise VLMDescriptionError('EMPTY')
        return text.strip()


def _write_receipt(path, value):
    # Receipts use the existing private audit storage, not the public image root.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as f:
        json.dump(value, f, ensure_ascii=False); name=f.name
    try:
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def describe_parsed_images(chunks, kb_id, service, run_async):
    """Append derived captions, preserving original image locators and failures.

    A durable RUNNING receipt is conservative UNKNOWN on future calls. This
    suppresses duplicate requests after interruption; it is not auto-recovery.
    """
    settings = get_settings()
    summary = {'COMPLETED':0, 'UNKNOWN':0, 'FAILED':0, 'NOT_CONFIGURED':0, 'cached':0}
    for chunk in chunks:
        if chunk.content_type != 'image' or not chunk.image_path:
            continue
        metadata = dict(chunk.metadata or {})
        if metadata.get('image_description', {}).get('status') == 'COMPLETED':
            continue
        if service is None:
            summary['NOT_CONFIGURED'] += 1
            metadata['image_description'] = {'status':'NOT_CONFIGURED'}
            chunk.metadata = metadata
            continue
        path = (Path(settings.IMAGE_DIR) / chunk.image_path).resolve()
        root = Path(settings.IMAGE_DIR).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            summary['FAILED'] += 1
            metadata['image_description'] = {'status':'SOURCE_UNAVAILABLE'}
            chunk.metadata = metadata
            continue
        try:
            image_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            identity = json.dumps([kb_id,image_hash,service.base_url,service.model,service.prompt,1000],ensure_ascii=False)
            key = hashlib.sha256(identity.encode()).hexdigest()
            directory = Path(settings.PDF_AUDIT_DIR) / 'image-descriptions'
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
            receipt = directory / (key + '.json')
            value = None
            try:
                with receipt.open('x', encoding='utf-8') as handle:
                    os.chmod(receipt, 0o600)
                    json.dump({'status':'RUNNING','image_sha256':image_hash,'model':service.model},handle)
            except FileExistsError:
                # Invalid or partial receipts are UNKNOWN, never an implicit retry.
                try: value = json.loads(receipt.read_text(encoding='utf-8'))
                except (ValueError, OSError): value = {'status':'UNKNOWN'}
                if value.get('status') == 'RUNNING': value = {'status':'UNKNOWN'}
                summary['cached'] += 1
            if value is None:
                try:
                    caption = run_async(service.describe_image(str(path)))
                    value = {'status':'COMPLETED', 'text':caption,'image_sha256':image_hash,'model':service.model}
                except Exception as exc:
                    from app.services.llm import is_unknown_provider_error
                    status = getattr(exc, 'status', None)
                    if is_unknown_provider_error(exc): status='UNKNOWN'
                    value = {'status':status or 'FAILED','error_type':type(exc).__name__, 'image_sha256':image_hash,'model':service.model}
                _write_receipt(receipt, value)
        except OSError as exc:
            # No durable attempt record means no safe provider invocation; if a
            # response write failed the RUNNING marker remains conservative.
            summary['FAILED'] += 1
            metadata['image_description'] = {'status':'RECEIPT_UNAVAILABLE','error_type':type(exc).__name__}
            chunk.metadata = metadata
            continue
        status = value.get('status', 'UNKNOWN')
        metadata['image_description'] = {'status':status,'image_sha256':image_hash,'model':service.model,'receipt_id':key}
        if status == 'COMPLETED' and isinstance(value.get('text'), str):
            chunk.content += '\n[图片描述（模型生成，需核对原图）]\n' + value['text']
            metadata['content_origin'] = 'model_generated_image_description'
            summary['COMPLETED'] += 1
        else:
            summary['UNKNOWN' if status in ('UNKNOWN','RUNNING') else 'FAILED'] += 1
        chunk.metadata = metadata
        event('ingest.image_description', kb_id=kb_id, status=status, model=service.model,
              image_sha256=image_hash, receipt_id=key)
    return summary

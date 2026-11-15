"""
VLM (视觉语言模型) 服务
"""

import base64
import httpx
from typing import Optional
from app.services.llm import invoke_provider_http_async

from app.core.config import get_settings, provider_tls_context

settings = get_settings()


class VLMService:
    """VLM 服务"""

    def __init__(self):
        self.base_url = settings.VLM_BASE_URL
        self.api_key = settings.VLM_API_KEY
        self.model = settings.VLM_MODEL
        self.prompt = settings.VLM_PROMPT

    async def describe_image(self, image_path: str) -> str:
        """获取图片描述"""
        try:
            # 读取图片并转为 base64
            with open(image_path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode("utf-8")

            # 判断图片类型
            ext = image_path.split(".")[-1].lower()
            mime_type = {
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "gif": "image/gif",
                "webp": "image/webp",
            }.get(ext, "image/png")

            # 构建请求
            async with httpx.AsyncClient(timeout=120.0, verify=provider_tls_context()) as client:
                response = await invoke_provider_http_async(lambda: client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": self.prompt,
                                    },
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:{mime_type};base64,{image_data}"
                                        },
                                    },
                                ],
                            }
                        ],
                        "max_tokens": 1000,
                    },
                ), stage="vlm", model_name=self.model)

                if response.status_code != 200:
                    raise Exception(f"VLM API 错误: {response.text}")

                result = response.json()
                return result["choices"][0]["message"]["content"]

        except Exception as e:
            print(f"VLM 调用失败: {type(e).__name__}")
            return "图片解析失败"


from pathlib import Path

class VLMDescriptionError(RuntimeError):
    def __init__(self, status, *, response_evidence=None):
        self.status = status
        self.response_evidence = response_evidence
        super().__init__("Image description did not complete: " + status)


class TaskVLMService:
    def __init__(self, base_url=None, api_key=None, model=None, prompt=None, max_tokens=1000):
        settings = get_settings()
        self.base_url = base_url if base_url is not None else settings.VLM_BASE_URL
        self.api_key = api_key if api_key is not None else settings.VLM_API_KEY
        self.model = model if model is not None else settings.VLM_MODEL
        self.prompt = prompt if prompt is not None else settings.VLM_PROMPT
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 128 <= max_tokens <= 32768:
            raise ValueError('VLM_MAX_TOKENS_INVALID')
        self.max_tokens = max_tokens

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
                ]}], 'max_tokens':self.max_tokens}), stage='vlm', model_name=self.model)
            response.raise_for_status()
            result = response.json()
        choice = result['choices'][0]
        reason = choice.get('finish_reason')
        text = choice.get('message', {}).get('content')
        # Preserve visible output and response accounting, never hidden reasoning
        # or credentials, and never expose rejected output as a successful caption.
        evidence = {'finish_reason': reason, 'response_id': result.get('id'),
                    'usage': result.get('usage'), 'max_tokens': self.max_tokens,
                    'visible_text': text if isinstance(text, str) else None}
        if reason not in ('stop', None):
            raise VLMDescriptionError('TRUNCATED' if reason == 'length' else 'UNKNOWN',
                                      response_evidence=evidence)
        if not isinstance(text, str) or not text.strip():
            raise VLMDescriptionError('EMPTY', response_evidence=evidence)
        return text.strip()

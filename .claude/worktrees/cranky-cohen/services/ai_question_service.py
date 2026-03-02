# -*- coding: utf-8 -*-
"""
AI问答服务（硅基流动）
"""
import json
import logging
import os
import time
from services.external_api import record_external_api_timing as _record_external_api_timing
try:
    import requests
except ImportError:  # pragma: no cover - 运行环境缺依赖时兜底
    requests = None


class AIQuestionService:
    """AI问答服务类（OpenAI兼容接口）"""

    _knowledge_cache = None

    def __init__(self, api_key, api_base, allowed_models, connect_timeout=8, read_timeout=60, retries=1, max_tokens=800):
        self.api_key = api_key
        self.api_base = api_base.rstrip('/')
        self.allowed_models = allowed_models
        self.logger = logging.getLogger(__name__)
        self.connect_timeout = self._safe_number(connect_timeout, 8.0, is_int=False)
        self.read_timeout = self._safe_number(read_timeout, 60.0, is_int=False)
        self.retries = int(self._safe_number(retries, 1, is_int=True))
        self.max_tokens = int(self._safe_number(max_tokens, 800, is_int=True))

    def _safe_number(self, value, default, is_int=False):
        try:
            number = int(value) if is_int else float(value)
        except (TypeError, ValueError):
            return default
        if number <= 0:
            return default
        return number

    def ask(self, question, model):
        """向模型提问"""
        if requests is None:
            raise RuntimeError("缺少requests依赖，请安装requirements.txt")
        if not self.api_key:
            raise RuntimeError("未配置AI服务API Key")
        if model not in self.allowed_models:
            raise ValueError("不支持的模型")
        if not question or not isinstance(question, str):
            raise ValueError("问题不能为空")

        url = f"{self.api_base}/chat/completions"
        headers = {
            'Authorization': f"Bearer {self.api_key}",
            'Content-Type': 'application/json'
        }
        knowledge_context = self._build_knowledge_context(question)
        system_prompt = '你是天气健康风险预测系统的智能助手。请用简洁、专业的中文回答。'
        if knowledge_context:
            system_prompt += f"\n以下是专业医学知识库摘要，仅供参考：\n{knowledge_context}"

        payload = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': question.strip()}
            ],
            'temperature': 0.2,
            'max_tokens': self.max_tokens
        }

        attempts = max(1, self.retries + 1)
        for attempt in range(attempts):
            try:
                start_ts = time.perf_counter()
                response = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=(self.connect_timeout, self.read_timeout)
                )
                elapsed_ms = round((time.perf_counter() - start_ts) * 1000, 2)
                _record_external_api_timing('ai_chat', elapsed_ms, response.status_code)
                request_id = response.headers.get('X-Request-Id') or response.headers.get('x-request-id')
                if response.status_code in (401, 403):
                    self.logger.warning(
                        "AI鉴权失败: status=%s duration_ms=%s request_id=%s",
                        response.status_code,
                        elapsed_ms,
                        request_id
                    )
                    raise RuntimeError("AI鉴权失败，请检查API密钥")
                if response.status_code in (429, 500, 502, 503, 504):
                    self.logger.warning(
                        "AI服务暂不可用: status=%s duration_ms=%s request_id=%s",
                        response.status_code,
                        elapsed_ms,
                        request_id
                    )
                    if attempt < attempts - 1:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    raise RuntimeError("AI服务暂不可用")
                if response.status_code != 200:
                    self.logger.warning(
                        "AI请求失败: status=%s duration_ms=%s request_id=%s",
                        response.status_code,
                        elapsed_ms,
                        request_id
                    )
                    raise RuntimeError("AI服务暂不可用")

                try:
                    data = response.json()
                except Exception as json_error:
                    self.logger.warning(
                        "AI响应解析失败: %s status=%s duration_ms=%s request_id=%s",
                        json_error,
                        response.status_code,
                        elapsed_ms,
                        request_id
                    )
                    raise RuntimeError("AI响应解析失败")

                choices = data.get('choices', [])
                if not choices:
                    raise RuntimeError("AI返回为空")

                message = choices[0].get('message', {})
                content = message.get('content', '').strip()
                if not content:
                    raise RuntimeError("AI返回内容为空")

                return content
            except requests.exceptions.Timeout as exc:
                self.logger.warning("AI请求超时: %s", exc)
                if attempt < attempts - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError("AI请求超时") from exc
            except requests.exceptions.RequestException as exc:
                self.logger.warning("AI请求异常: %s", exc)
                if attempt < attempts - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise RuntimeError("AI请求失败") from exc

    def _build_knowledge_context(self, question):
        knowledge = self._load_knowledge_base()
        if not knowledge:
            return ''
        question_lower = question.lower()
        matches = []
        for item in knowledge:
            tags = item.get('tags', [])
            title = item.get('title', '')
            keywords = item.get('keywords', [])
            if any(tag.lower() in question_lower for tag in tags) or any(k.lower() in question_lower for k in keywords) or (title and title.lower() in question_lower):
                matches.append(item)
            if len(matches) >= 3:
                break
        if not matches:
            return ''
        lines = []
        for item in matches:
            lines.append(f"- {item.get('title')}: {item.get('content')}")
        return '\n'.join(lines)

    def _load_knowledge_base(self):
        if AIQuestionService._knowledge_cache is not None:
            return AIQuestionService._knowledge_cache
        base_dir = os.path.dirname(os.path.dirname(__file__))
        kb_path = os.path.join(base_dir, 'data', 'medical_kb.json')
        if not os.path.exists(kb_path):
            AIQuestionService._knowledge_cache = []
            return AIQuestionService._knowledge_cache
        try:
            with open(kb_path, 'r', encoding='utf-8') as f:
                AIQuestionService._knowledge_cache = json.load(f)
        except FileNotFoundError as exc:
            self.logger.warning("知识库文件不存在: %s", exc)
            AIQuestionService._knowledge_cache = []
        except json.JSONDecodeError as exc:
            self.logger.warning("知识库JSON解析失败: %s", exc)
            AIQuestionService._knowledge_cache = []
        except OSError as exc:
            self.logger.warning("知识库读取失败: %s", exc)
            AIQuestionService._knowledge_cache = []
        return AIQuestionService._knowledge_cache
